import assert from "node:assert/strict";
import test from "node:test";
import { PCMResampler, encodePCM16 } from "../../src/voice_bridge/web/pcm-resampler.js";

function resample(input, rate, chunkSize = 128) {
  const resampler = new PCMResampler(rate);
  const chunks = [];
  for (let offset = 0; offset < input.length; offset += chunkSize) {
    chunks.push(resampler.push(input.subarray(offset, offset + chunkSize)));
  }
  chunks.push(resampler.flush());
  assert.equal(resampler.flush().length, 0);
  assert.throws(() => resampler.push(new Float32Array(1)), /after flush/);
  const result = new Float32Array(chunks.reduce((sum, chunk) => sum + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}

function tone(rate, frequency, count = rate) {
  return Float32Array.from({ length: count }, (_, index) => Math.sin(2 * Math.PI * frequency * index / rate));
}

function rms(samples) {
  const interior = samples.subarray(200, samples.length - 200);
  return Math.sqrt(interior.reduce((sum, sample) => sum + sample ** 2, 0) / interior.length);
}

for (const rate of [8000, 16000, 32000, 44100, 48000, 96000]) {
  test(`${rate} Hz preserves exact captured duration and chunk continuity`, () => {
    const input = tone(rate, 1000, rate + 37);
    const smallChunks = resample(input, rate, 128);
    const largeChunks = resample(input, rate, input.length);
    assert.equal(smallChunks.length, Math.floor(input.length * 16000 / rate));
    assert.deepEqual(smallChunks, largeChunks);
    assert.ok(Math.abs(rms(smallChunks) - Math.SQRT1_2) < 0.01, "Speech-band tone keeps its gain");
  });
}

for (const rate of [44100, 48000, 96000]) {
  test(`${rate} Hz suppresses frequencies above output Nyquist`, () => {
    const passband = rms(resample(tone(rate, 1000), rate));
    for (const frequency of [9000, 12000, 15000]) {
      const rejected = rms(resample(tone(rate, frequency), rate));
      assert.ok(rejected / passband < 0.01, `${frequency} Hz must be attenuated at least 40 dB`);
    }
  });
}

test("short captures flush the delayed samples and constant signals retain unity gain", () => {
  assert.equal(resample(new Float32Array(0), 48000).length, 0);
  for (const count of [1, 2, 3, 17, 128, 130]) {
    assert.equal(resample(new Float32Array(count).fill(0.5), 48000).length, Math.floor(count / 3));
  }
  const result = resample(new Float32Array(48000).fill(0.25), 48000);
  for (const sample of result.subarray(100, result.length - 100)) assert.ok(Math.abs(sample - 0.25) < 1e-6);
});

test("PCM encoding is signed, clamped and explicitly little endian", () => {
  const result = new Uint8Array(encodePCM16([-2, -1, -0.5, 0, 0.5, 1, 2, NaN]));
  assert.deepEqual([...result], [0, 128, 0, 128, 0, 192, 0, 0, 0, 64, 255, 127, 255, 127, 0, 0]);
});

test("sample rates must be finite and positive", () => {
  for (const rate of [0, -1, NaN, Infinity]) assert.throws(() => new PCMResampler(rate), RangeError);
});
