import { PCMResampler, encodePCM16 } from "./pcm-resampler.js";

class PCMRecorderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.resampler = new PCMResampler(sampleRate, 16000);
    this.active = false;
    this.finished = false;
    this.packet = new Float32Array(800); // 50 ms at 16 kHz
    this.packetLength = 0;
    this.pendingBytes = 0;
    this.pendingPackets = 0;
    this.inputSamples = 0;
    this.inputLimit = Infinity;
    this.port.onmessage = ({ data }) => {
      if (data.type === "start" && !this.active && !this.finished) {
        this.packet = new Float32Array(Math.max(1, Math.min(800, data.packetSamples)));
        this.inputLimit = Math.floor(data.durationSeconds * sampleRate);
        this.active = true;
      } else if (data.type === "ack") {
        this.pendingBytes = Math.max(0, this.pendingBytes - data.bytes);
        this.pendingPackets = Math.max(0, this.pendingPackets - 1);
      } else if (data.type === "flush") {
        this._flush(false);
      }
    };
  }

  process(inputs, outputs) {
    // The graph also has a zero-gain node; write silence here as a second guard
    // against microphone playback, and keep an output connected for scheduling.
    for (const channels of outputs) for (const channel of channels) channel.fill(0);
    if (this.finished) return false;
    const channels = inputs[0];
    if (!this.active || !channels?.length || !channels[0].length) return true;
    const count = Math.min(channels[0].length, this.inputLimit - this.inputSamples);
    const mono = new Float32Array(count);
    for (const channel of channels) {
      for (let index = 0; index < count; index += 1) mono[index] += channel[index] / channels.length;
    }
    this.inputSamples += count;
    this._append(this.resampler.push(mono));
    // An audio-clock limit also works when a background tab throttles timers.
    if (!this.finished && this.inputSamples >= this.inputLimit) this._flush(true);
    return !this.finished;
  }

  _append(samples) {
    for (let index = 0; index < samples.length && !this.finished; index += 1) {
      this.packet[this.packetLength++] = samples[index];
      if (this.packetLength === this.packet.length) this._sendPacket();
    }
  }

  _sendPacket() {
    if (!this.packetLength || this.finished) return;
    const bytes = this.packetLength * 2;
    // Bound the MessagePort queue even if the UI thread stops receiving. The
    // main thread acknowledges only after it has sent the packet to WebSocket.
    if (this.pendingBytes + bytes > 64000 || this.pendingPackets >= 4096) {
      this.active = false;
      this.finished = true;
      this.port.postMessage({
        type: "error",
        code: "audio_overflow",
        message: "The browser could not send microphone audio fast enough",
      });
      return;
    }
    const buffer = encodePCM16(this.packet.subarray(0, this.packetLength));
    this.pendingBytes += bytes;
    this.pendingPackets += 1;
    this.packetLength = 0;
    this.port.postMessage({ type: "pcm", buffer }, [buffer]);
  }

  _flush(automatic) {
    if (this.finished) return;
    this.active = false;
    this._append(this.resampler.flush());
    this._sendPacket();
    if (this.finished) return; // Overflow already reported an error.
    this.finished = true;
    // A MessagePort is FIFO: every PCM message precedes this acknowledgement.
    this.port.postMessage({ type: "flushed", automatic });
  }
}

registerProcessor("pcm-recorder", PCMRecorderProcessor);
