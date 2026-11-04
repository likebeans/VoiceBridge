// Streaming, windowed-sinc FIR resampling. The ring buffer retains only the
// filter's history; chunk boundaries do not reset the filter or sample clock.
const PHASES = 512;

export class PCMResampler {
  constructor(inputRate, outputRate = 16000) {
    if (![inputRate, outputRate].every((rate) => Number.isFinite(rate) && rate > 0)) {
      throw new RangeError("Sample rates must be positive finite numbers");
    }
    this.inputRate = inputRate;
    this.outputRate = outputRate;
    this.inputCount = 0;
    this.outputCount = 0;
    this.finished = false;
    this.halfWidth = Math.ceil(16 * Math.max(1, inputRate / outputRate));
    this.history = new Float32Array(2 * this.halfWidth + Math.ceil(inputRate / outputRate) + 2);

    // Leave a transition band below the destination Nyquist frequency. Scale
    // the filter width with decimation, so 96 kHz input has the same rejection
    // in the output band as 48 kHz input. Normalize each phase for unity DC gain.
    const cutoff = 0.45 * Math.min(1, outputRate / inputRate);
    this.kernels = Array.from({ length: PHASES + 1 }, (_, phase) => {
      const weights = new Float64Array(2 * this.halfWidth + 1);
      let sum = 0;
      for (let tap = -this.halfWidth; tap <= this.halfWidth; tap += 1) {
        const distance = tap - phase / PHASES;
        if (Math.abs(distance) > this.halfWidth) continue;
        const angle = 2 * Math.PI * cutoff * distance;
        const sinc = angle === 0 ? 1 : Math.sin(angle) / angle;
        const window = 0.42 + 0.5 * Math.cos(Math.PI * distance / this.halfWidth)
          + 0.08 * Math.cos(2 * Math.PI * distance / this.halfWidth);
        const weight = 2 * cutoff * sinc * window;
        weights[tap + this.halfWidth] = weight;
        sum += weight;
      }
      for (let index = 0; index < weights.length; index += 1) weights[index] /= sum;
      return weights;
    });
  }

  push(samples) {
    if (this.finished) throw new Error("Cannot append samples after flush");
    const capacity = Math.floor((this.inputCount + samples.length) * this.outputRate / this.inputRate)
      - this.outputCount;
    const output = new Float32Array(capacity);
    let count = 0;
    for (let index = 0; index < samples.length; index += 1) {
      this.history[this.inputCount % this.history.length] = samples[index];
      this.inputCount += 1;
      // Wait for the right half of the symmetric filter, preserving phase
      // across render quanta. flush() supplies zero padding at the final edge.
      while (Math.floor(this.outputCount * this.inputRate / this.outputRate)
        + this.halfWidth < this.inputCount) {
        output[count++] = this._nextSample();
      }
    }
    return output.subarray(0, count);
  }

  flush() {
    if (this.finished) return new Float32Array(0);
    this.finished = true;
    // Keep exactly the duration captured, without appending an artificial tail.
    const length = Math.floor(this.inputCount * this.outputRate / this.inputRate) - this.outputCount;
    const output = new Float32Array(length);
    for (let index = 0; index < length; index += 1) output[index] = this._nextSample();
    return output;
  }

  _nextSample() {
    const position = this.outputCount * this.inputRate / this.outputRate;
    const center = Math.floor(position);
    const weights = this.kernels[Math.round((position - center) * PHASES)];
    let value = 0;
    for (let tap = 0; tap < weights.length; tap += 1) {
      const index = center + tap - this.halfWidth;
      if (index >= 0 && index < this.inputCount) {
        value += this.history[index % this.history.length] * weights[tap];
      }
    }
    this.outputCount += 1;
    return value;
  }
}

export function encodePCM16(samples) {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Number.isFinite(samples[index]) ? Math.max(-1, Math.min(1, samples[index])) : 0;
    view.setInt16(index * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
  }
  return buffer;
}
