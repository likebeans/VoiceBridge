import assert from "node:assert/strict";
import test from "node:test";
import { RealtimeTranscriber } from "../../src/voice_bridge/web/recorder.js";

const ready = {
  type: "ready",
  session_id: "test-session",
  provider: "funasr",
  audio: { encoding: "pcm_s16le", sample_rate: 16000, channels: 1 },
  max_session_seconds: 120,
  max_audio_seconds: 120,
  max_audio_chunk_bytes: 16384,
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function turn() { for (let step = 0; step < 8; step += 1) await Promise.resolve(); }

function mockBrowser(t, { permission, moduleReady, resumeReady, clientOptions } = {}) {
  const events = [];
  const contexts = [];
  const worklets = [];
  const sockets = [];
  const streams = [];
  const tracks = [];
  const timers = new Map();
  let timerID = 0;
  let now = 0;
  const globals = new Map();
  const page = new EventTarget();
  function replace(name, value) {
    globals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  }
  function stream() {
    const track = { readyState: "live", stopCalls: 0, onended: null,
      stop() { this.readyState = "ended"; this.stopCalls += 1; },
      end() { this.readyState = "ended"; this.onended?.(); },
    };
    const result = { getTracks: () => [track], getAudioTracks: () => [track] };
    tracks.push(track);
    streams.push(result);
    return result;
  }
  function node() {
    return { connections: [], disconnectCalls: 0,
      connect(destination) { this.connections.push(destination); return destination; },
      disconnect() { this.connections = []; this.disconnectCalls += 1; },
    };
  }
  class MockContext {
    constructor() {
      this.state = "suspended";
      this.sampleRate = 44100;
      this.destination = {};
      this.nodes = [];
      this.closeCalls = 0;
      this.audioWorklet = { addModule: (url) => {
        events.push(["module", String(url)]);
        return moduleReady?.promise || Promise.resolve();
      } };
      contexts.push(this);
    }
    resume() {
      events.push(["resume"]);
      if (resumeReady) return resumeReady.promise;
      this.state = "running";
      return Promise.resolve();
    }
    close() { this.closeCalls += 1; this.state = "closed"; return Promise.resolve(); }
    createMediaStreamSource() { const result = node(); this.nodes.push(result); return result; }
    createGain() { const result = { ...node(), gain: { value: 1 } }; this.nodes.push(result); return result; }
    suspend() { this.state = "suspended"; this.onstatechange?.(); }
  }
  class MockWorklet {
    constructor(context, name) {
      Object.assign(this, node());
      this.context = context;
      this.name = name;
      this.port = { messages: [], onmessage: null, closed: false,
        postMessage(message) { this.messages.push(message); },
        close() { this.closed = true; },
      };
      worklets.push(this);
    }
    emit(data) { this.port.onmessage?.({ data }); }
  }
  class MockSocket {
    constructor(url) {
      events.push(["socket", url]);
      this.url = url;
      this.readyState = 0;
      this.bufferedAmount = 0;
      this.sent = [];
      this.closeCalls = 0;
      sockets.push(this);
    }
    open() { this.readyState = 1; this.onopen?.(); }
    send(message) { this.sent.push(message); }
    receive(message) { this.onmessage?.({ data: JSON.stringify(message) }); }
    close() { this.readyState = 3; this.closeCalls += 1; }
    disconnect() { this.readyState = 3; this.onclose?.({ code: 1006 }); }
  }
  replace("isSecureContext", true);
  replace("addEventListener", page.addEventListener.bind(page));
  replace("removeEventListener", page.removeEventListener.bind(page));
  replace("location", { href: "https://voice.example/demo" });
  replace("navigator", { mediaDevices: { getUserMedia: () => {
    events.push(["permission"]);
    return permission ? permission.promise : Promise.resolve(stream());
  } } });
  replace("AudioContext", MockContext);
  replace("AudioWorkletNode", MockWorklet);
  replace("WebSocket", MockSocket);
  replace("setTimeout", (callback, delay) => {
    timers.set(++timerID, { callback, delay, due: now + delay });
    return timerID;
  });
  replace("clearTimeout", (id) => timers.delete(id));
  t.after(() => {
    for (const [name, descriptor] of globals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  const client = new RealtimeTranscriber(clientOptions);
  t.after(() => client.cancel());
  const states = [];
  const errors = [];
  client.addEventListener("statechange", ({ detail }) => states.push(detail.state));
  client.addEventListener("error", ({ detail }) => errors.push(detail));
  return { client, states, errors, events, contexts, worklets, sockets, streams, tracks, stream, timers, page,
    async connect(message = ready) {
      const started = client.start();
      await turn();
      sockets.at(-1).open();
      sockets.at(-1).receive(message);
      await started;
      return { socket: sockets.at(-1), worklet: worklets.at(-1), context: contexts.at(-1) };
    },
    advance(milliseconds) {
      const target = now + milliseconds;
      while (true) {
        const next = [...timers.entries()].filter(([, timer]) => timer.due <= target)
          .sort((a, b) => a[1].due - b[1].due)[0];
        if (!next) break;
        const [id, timer] = next;
        now = timer.due;
        timers.delete(id);
        timer.callback();
      }
      now = target;
    },
    fire(delay) {
      const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `Expected a ${delay} ms timer`);
      const [id, timer] = entry;
      timers.delete(id); // Real setTimeout removes itself before invoking its callback.
      timer.callback();
    },
  };
}

test("prepares microphone before connecting; starts PCM only after ready", async (t) => {
  const permission = deferred();
  const moduleReady = deferred();
  const env = mockBrowser(t, { permission, moduleReady });
  const { client } = env;
  const started = client.start();
  assert.equal(client.state, "connecting");
  assert.deepEqual(env.events.map(([event]) => event), ["resume", "permission", "module"]);
  await turn();
  assert.equal(env.sockets.length, 0);
  permission.resolve(env.stream());
  await turn();
  assert.equal(env.sockets.length, 0);
  moduleReady.resolve();
  await turn();
  const socket = env.sockets[0];
  const worklet = env.worklets[0];
  assert.equal(socket.url, "wss://voice.example/v1/asr");
  assert.equal(worklet.context.nodes[0].connections.length, 0);
  socket.open();
  assert.deepEqual(socket.sent.map(JSON.parse), [{ type: "start", provider: "funasr", language: null,
    audio: { encoding: "pcm_s16le", sample_rate: 16000, channels: 1 } }]);
  assert.equal(worklet.port.messages.length, 0);
  socket.receive(ready);
  await started;
  assert.equal(client.state, "recording");
  assert.equal(worklet.port.messages[0].packetSamples, 800);
  assert.equal(worklet.port.messages[0].durationSeconds, 118);
  assert.equal(worklet.context.nodes[1].gain.value, 0);
  assert.equal(worklet.context.nodes[1].connections[0], worklet.context.destination);
});

test("stop drains queued PCM before stop and retains results until done", async (t) => {
  const env = mockBrowser(t);
  const { client } = env;
  const { socket, worklet, context } = await env.connect();
  const partial = { type: "transcript", confirmed_text: "第一句。", full_text: "第一句。第二", sequence: 1 };
  const transcripts = [];
  const results = [];
  client.addEventListener("transcript", ({ detail }) => transcripts.push(detail));
  client.addEventListener("done", ({ detail }) => results.push(detail));
  socket.receive(partial);
  const stopped = client.stop();
  const duplicate = client.stop();
  let resolved = false;
  stopped.then(() => { resolved = true; });
  assert.equal(client.state, "stopping");
  assert.equal(client.fullText, partial.full_text);
  assert.equal(env.tracks[0].stopCalls, 0);
  assert.equal(socket.sent.length, 1);
  assert.equal(worklet.port.messages.filter((message) => message.type === "flush").length, 1);
  const first = new ArrayBuffer(1600);
  const tail = new ArrayBuffer(78);
  worklet.emit({ type: "pcm", buffer: first });
  worklet.emit({ type: "pcm", buffer: tail });
  worklet.emit({ type: "flushed", automatic: false });
  assert.deepEqual(socket.sent.slice(1), [first, tail, JSON.stringify({ type: "stop" })]);
  assert.equal(env.tracks[0].stopCalls, 1);
  assert.equal(context.closeCalls, 1);
  assert.equal(worklet.port.closed, true);
  assert.equal(socket.closeCalls, 0);
  await turn();
  assert.equal(resolved, false);
  const final = { ...partial, full_text: "第一句。第二句。", confirmed_text: "第一句。第二句。", is_final: true };
  socket.receive(final);
  const done = { type: "done", full_text: final.full_text, session_id: "test-session", audio_seconds: 0.052 };
  socket.receive(done);
  assert.deepEqual(await stopped, done);
  assert.deepEqual(await duplicate, done);
  assert.deepEqual(transcripts, [partial, final]);
  assert.deepEqual(results, [done]);
  assert.equal(client.result.full_text, done.full_text);
  assert.equal(client.confirmedText, done.full_text);
  assert.equal(socket.closeCalls, 1);
  assert.deepEqual(env.states, ["connecting", "recording", "stopping", "done"]);
  assert.equal(env.timers.size, 0);
  assert.deepEqual(await client.stop(), done);
});

test("cancelling pending permission returns immediately and isolates late streams from a new session", async (t) => {
  const permission = deferred();
  const env = mockBrowser(t, { permission });
  const first = env.client.start();
  const rejected = assert.rejects(first, { code: "cancelled" });
  env.client.cancel();
  assert.equal(env.client.state, "cancelled");
  assert.equal(env.contexts[0].closeCalls, 1);
  await rejected;
  const lateStream = env.stream();
  const newStream = env.stream();
  navigator.mediaDevices.getUserMedia = () => Promise.resolve(newStream);
  const second = env.client.start();
  await turn();
  permission.resolve(lateStream);
  await turn();
  assert.equal(lateStream.getTracks()[0].stopCalls, 1);
  assert.equal(newStream.getTracks()[0].stopCalls, 0);
  assert.equal(env.contexts[1].closeCalls, 0);
  assert.equal(env.sockets.length, 1);
  env.sockets[0].open();
  env.sockets[0].receive(ready);
  await second;
  assert.equal(env.client.state, "recording");
  assert.deepEqual(env.errors, []);
});

test("old socket and worklet callbacks cannot change a new recording", async (t) => {
  const env = mockBrowser(t);
  const old = await env.connect();
  const oldMessage = old.socket.onmessage;
  const oldError = old.socket.onerror;
  const oldClose = old.socket.onclose;
  const oldAudio = old.worklet.port.onmessage;
  env.client.cancel();
  const active = await env.connect();
  oldMessage({ data: JSON.stringify({ type: "error", code: "old_error", message: "old" }) });
  oldError();
  oldClose();
  oldAudio({ data: { type: "flushed", automatic: true } });
  assert.equal(env.client.state, "recording");
  assert.equal(active.socket.closeCalls, 0);
  assert.equal(env.errors.length, 0);
  assert.equal(old.socket.closeCalls, 1);
});

test("stop during connection cancels without sending the forbidden stop message", async (t) => {
  const env = mockBrowser(t);
  const starting = env.client.start();
  const rejected = assert.rejects(starting, { code: "cancelled" });
  await turn();
  env.sockets[0].open();
  await env.client.stop();
  await rejected;
  assert.equal(env.client.state, "cancelled");
  assert.equal(env.sockets[0].sent.length, 1);
  assert.equal(env.tracks[0].stopCalls, 1);
});

test("cancel during stopping releases all resources and rejects the waiting stop", async (t) => {
  const env = mockBrowser(t);
  const { socket, worklet, context } = await env.connect();
  const stopping = env.client.stop();
  const rejected = assert.rejects(stopping, { code: "cancelled" });
  env.client.cancel();
  await rejected;
  assert.equal(env.client.state, "cancelled");
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.closeCalls, 1);
  assert.equal(context.closeCalls, 1);
  assert.equal(worklet.port.closed, true);
  assert.equal(env.tracks[0].stopCalls, 1);
  assert.equal(env.errors.length, 0);
});

test("a new recording clears the previous transcript and done result", async (t) => {
  const env = mockBrowser(t);
  const { socket, worklet } = await env.connect();
  const stopping = env.client.stop();
  worklet.emit({ type: "flushed", automatic: false });
  socket.receive({ type: "done", full_text: "完成。" });
  await stopping;
  assert.equal(env.client.fullText, "完成。");
  const again = env.client.start();
  assert.equal(env.client.fullText, "");
  assert.equal(env.client.confirmedText, "");
  assert.equal(env.client.result, null);
  await turn();
  env.sockets[1].open();
  env.sockets[1].receive(ready);
  await again;
});

test("server error preserves confirmed text and closes audio and connection", async (t) => {
  const env = mockBrowser(t);
  const { socket } = await env.connect();
  socket.receive({ type: "transcript", confirmed_text: "原文。", full_text: "原文。待确认" });
  socket.receive({ type: "error", code: "upstream_closed", message: "Upstream closed", confirmed_text: "最终原文。" });
  assert.equal(env.client.state, "error");
  assert.equal(env.client.fullText, "最终原文。");
  assert.deepEqual(env.errors, [{ code: "upstream_closed", message: "Upstream closed", confirmed_text: "最终原文。" }]);
  assert.equal(socket.closeCalls, 1);
  assert.equal(env.tracks[0].stopCalls, 1);
  assert.equal(env.contexts[0].closeCalls, 1);
  assert.equal(env.timers.size, 0);
});

for (const reason of ["track", "suspended", "socket", "processor", "overflow"]) {
  test(`${reason} failure terminates recording explicitly`, async (t) => {
    const env = mockBrowser(t);
    const { socket, worklet, context } = await env.connect();
    socket.receive({ type: "transcript", confirmed_text: "保留", full_text: "保留临时" });
    if (reason === "track") env.tracks[0].end();
    if (reason === "suspended") context.suspend();
    if (reason === "socket") socket.disconnect();
    if (reason === "processor") worklet.onprocessorerror();
    if (reason === "overflow") worklet.emit({ type: "error", code: "audio_overflow", message: "Slow UI" });
    assert.equal(env.client.state, "error");
    assert.equal(env.errors.length, 1);
    assert.equal(env.errors[0].confirmed_text, "保留");
    assert.equal(env.client.fullText, "保留");
    assert.equal(env.tracks[0].stopCalls, 1);
    assert.equal(context.closeCalls, 1);
  });
}

test("network buffering is bounded and no packet is sent over the limit", async (t) => {
  const env = mockBrowser(t);
  const { socket, worklet } = await env.connect();
  socket.bufferedAmount = 63000;
  worklet.emit({ type: "pcm", buffer: new ArrayBuffer(1600) });
  assert.equal(env.client.state, "error");
  assert.equal(env.errors[0].code, "network_backpressure");
  assert.equal(socket.sent.length, 1);
});

test("server chunk limit shrinks packets to an even PCM length", async (t) => {
  const env = mockBrowser(t);
  const { worklet } = await env.connect({ ...ready, max_audio_chunk_bytes: 999 });
  assert.equal(worklet.port.messages[0].packetSamples, 499);
  worklet.emit({ type: "pcm", buffer: new ArrayBuffer(998) });
  assert.equal(env.client.state, "recording");
});

test("wall-clock and audio-clock automatic stop both retain the final response", async (t) => {
  const env = mockBrowser(t);
  let { socket, worklet } = await env.connect({ ...ready, max_audio_seconds: 5 });
  assert.equal(worklet.port.messages[0].durationSeconds, 4.5);
  env.fire(4500);
  assert.equal(env.client.state, "stopping");
  worklet.emit({ type: "flushed", automatic: false });
  socket.receive({ type: "done", full_text: "计时停止" });
  ({ socket, worklet } = await env.connect());
  worklet.emit({ type: "flushed", automatic: true });
  assert.equal(env.client.state, "stopping");
  assert.deepEqual(JSON.parse(socket.sent.at(-1)), { type: "stop" });
  socket.receive({ type: "done", full_text: "采样停止" });
  assert.equal(env.client.fullText, "采样停止");
  assert.equal(env.client.state, "done");
});

for (const stage of ["init", "ready", "flush", "done"]) {
  test(`${stage} has a finite timeout and releases resources`, async (t) => {
    const env = mockBrowser(t);
    const starting = env.client.start();
    const startedRejected = ["init", "ready"].includes(stage) ? assert.rejects(starting) : null;
    await turn();
    const socket = env.sockets[0];
    if (stage === "init") {
      env.fire(30000);
      await startedRejected;
    } else {
      socket.open();
      if (stage === "ready") {
        env.fire(20000);
        await startedRejected;
      } else {
        socket.receive(ready);
        await starting;
        const stopped = env.client.stop();
        const stoppedRejected = assert.rejects(stopped);
        if (stage === "flush") env.fire(3000);
        else {
          env.worklets[0].emit({ type: "flushed", automatic: false });
          env.fire(25000);
        }
        await stoppedRejected;
      }
    }
    assert.equal(env.client.state, "error");
    assert.match(env.errors[0].code, /timeout$/);
    assert.equal(env.tracks[0].stopCalls, 1);
    assert.equal(socket.closeCalls, 1);
    assert.equal(env.timers.size, 0);
  });
}

test("state listeners may cancel synchronously during connecting or stopping", async (t) => {
  const env = mockBrowser(t);
  const cancelConnecting = ({ detail }) => { if (detail.state === "connecting") env.client.cancel(); };
  env.client.addEventListener("statechange", cancelConnecting);
  await assert.rejects(env.client.start(), { code: "cancelled" });
  assert.equal(env.events.length, 0);
  env.client.removeEventListener("statechange", cancelConnecting);
  await env.connect();
  env.client.addEventListener("statechange", ({ detail }) => {
    if (detail.state === "stopping") env.client.cancel();
  });
  await assert.rejects(env.client.stop(), { code: "cancelled" });
  assert.equal(env.client.state, "cancelled");
  assert.equal(env.errors.length, 0);
});

for (const stage of ["connecting", "stopping"]) {
  test(`later listeners see cancellation after ${stage}, without a stale final state`, async (t) => {
    const env = mockBrowser(t);
    if (stage === "stopping") await env.connect();
    env.client.addEventListener("statechange", ({ detail }) => {
      if (detail.state === stage) env.client.cancel();
    });
    const laterStates = [];
    env.client.addEventListener("statechange", ({ detail }) => laterStates.push(detail.state));
    await assert.rejects(stage === "connecting" ? env.client.start() : env.client.stop(), { code: "cancelled" });
    assert.deepEqual(laterStates, [stage, "cancelled"]);
    assert.equal(laterStates.at(-1), env.client.state);
    assert.equal(env.timers.size, 0);
  });
}

test("longer client budgets permit late ready and final text without truncating the session", async (t) => {
  const env = mockBrowser(t, { clientOptions: { timeouts: { ready: 40000, done: 40000 } } });
  const starting = env.client.start();
  starting.catch(() => {});
  await turn();
  const socket = env.sockets[0];
  socket.open();
  env.advance(21000);
  assert.equal(env.client.state, "connecting");
  socket.receive(ready);
  await starting;
  socket.receive({ type: "transcript", full_text: "最后一句", confirmed_text: "" });
  const stopping = env.client.stop();
  stopping.catch(() => {});
  env.worklets[0].emit({ type: "flushed", automatic: false });
  env.advance(26000);
  assert.equal(env.client.state, "stopping");
  assert.equal(socket.closeCalls, 0);
  const done = { type: "done", session_id: "test-session", full_text: "最后一句。", audio_seconds: 0.1 };
  socket.receive(done);
  assert.deepEqual(await stopping, done);
  assert.equal(env.client.fullText, "最后一句。");
  assert.equal(env.timers.size, 0);
});

for (const stage of ["init", "ready", "flush", "done"]) {
  test(`custom ${stage} timeout still bounds failures and releases resources`, async (t) => {
    const env = mockBrowser(t, { clientOptions: { timeouts: { [stage]: 6000 } } });
    const starting = env.client.start();
    starting.catch(() => {});
    await turn();
    const socket = env.sockets[0];
    let operation = starting;
    if (stage !== "init") socket.open();
    if (["flush", "done"].includes(stage)) {
      socket.receive(ready);
      await starting;
      operation = env.client.stop();
      operation.catch(() => {});
      if (stage === "done") env.worklets[0].emit({ type: "flushed", automatic: false });
    }
    const rejected = assert.rejects(operation, { code: stage === "init" ? "initialization_timeout" : `${stage}_timeout` });
    env.advance(5999);
    assert.notEqual(env.client.state, "error");
    env.advance(1);
    await rejected;
    assert.equal(env.client.state, "error");
    assert.equal(env.tracks[0].stopCalls, 1);
    assert.equal(socket.closeCalls, 1);
    assert.equal(env.timers.size, 0);
  });
}

test("timeout settings reject invalid values and unknown stage names", () => {
  for (const value of [0, -1, NaN, Infinity, "30000", 2147483648]) {
    assert.throws(() => new RealtimeTranscriber({ timeouts: { ready: value } }), RangeError);
  }
  for (const timeouts of [null, [], 1000, { reday: 30000 }]) {
    assert.throws(() => new RealtimeTranscriber({ timeouts }), TypeError);
  }
});

test("permission failure before WebSocket reports an actionable error", async (t) => {
  const permission = deferred();
  const env = mockBrowser(t, { permission });
  const started = env.client.start();
  const rejected = assert.rejects(started, { code: "microphone_denied" });
  permission.reject(new DOMException("Permission denied", "NotAllowedError"));
  await rejected;
  assert.equal(env.sockets.length, 0);
  assert.equal(env.errors[0].code, "microphone_denied");
  assert.equal(env.contexts[0].closeCalls, 1);
});

test("numeric DOMException codes map to a stable microphone error", async (t) => {
  const env = mockBrowser(t);
  navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException("No device", "NotFoundError"));
  await assert.rejects(env.client.start(), { code: "microphone_not_found" });
  assert.equal(env.errors[0].code, "microphone_not_found");
  assert.equal(env.sockets.length, 0);
  assert.equal(env.contexts[0].closeCalls, 1);
});

test("synchronous initialization failure still observes other rejected promises", async (t) => {
  const env = mockBrowser(t);
  navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException("No device", "NotFoundError"));
  const ContextClass = globalThis.AudioContext;
  globalThis.AudioContext = class extends ContextClass {
    constructor() {
      super();
      this.audioWorklet.addModule = () => { throw new Error("Module cannot load"); };
    }
    resume() { return Promise.reject(new Error("Audio cannot resume")); }
  };
  await assert.rejects(env.client.start());
  await turn();
  assert.equal(env.errors.length, 1);
  assert.equal(env.client.state, "error");
  assert.equal(env.contexts[0].closeCalls, 1);
  assert.equal(env.sockets.length, 0);
});

test("leaving the page cancels recording and releases the microphone", async (t) => {
  const env = mockBrowser(t);
  const { socket, worklet, context } = await env.connect();
  env.page.dispatchEvent(new Event("pagehide"));
  assert.equal(env.client.state, "cancelled");
  assert.equal(env.tracks[0].stopCalls, 1);
  assert.equal(context.closeCalls, 1);
  assert.equal(socket.closeCalls, 1);
  assert.equal(worklet.port.closed, true);
});

test("module loading failure still stops a microphone granted later", async (t) => {
  const permission = deferred();
  const moduleReady = deferred();
  const env = mockBrowser(t, { permission, moduleReady });
  const started = env.client.start();
  const rejected = assert.rejects(started);
  moduleReady.reject(new Error("Module failed to load"));
  await rejected;
  const late = env.stream();
  permission.resolve(late);
  await turn();
  assert.equal(late.getTracks()[0].stopCalls, 1);
  assert.equal(env.client.state, "error");
  assert.equal(env.sockets.length, 0);
});

test("initialization timeout does not wait for an unanswered permission dialog", async (t) => {
  const permission = deferred();
  const env = mockBrowser(t, { permission });
  const started = env.client.start();
  const rejected = assert.rejects(started, { code: "initialization_timeout" });
  env.fire(30000);
  await rejected;
  assert.equal(env.client.state, "error");
  assert.equal(env.contexts[0].closeCalls, 1);
  const late = env.stream();
  permission.resolve(late);
  await turn();
  assert.equal(late.getTracks()[0].stopCalls, 1);
  assert.equal(env.sockets.length, 0);
});

// Run the actual AudioWorklet processor with fake browser primitives. This
// exercises packet sizes and flush acknowledgement, not just the main-thread
// client's assumptions about the worker.
let Processor;
globalThis.AudioWorkletProcessor = class {
  constructor() { this.port = { messages: [], postMessage(message) { this.messages.push(message); } }; }
};
globalThis.sampleRate = 48000;
globalThis.registerProcessor = (name, implementation) => {
  assert.equal(name, "pcm-recorder");
  Processor = implementation;
};
await import("../../src/voice_bridge/web/pcm-worklet.js");
delete globalThis.AudioWorkletProcessor;
delete globalThis.registerProcessor;

function processor(packetSamples = 800, durationSeconds = 118) {
  globalThis.sampleRate = 48000;
  const result = new Processor();
  result.port.onmessage({ data: { type: "start", packetSamples, durationSeconds } });
  return result;
}

test("worklet emits 50 ms packets, averages channels, writes silence and flushes its tail", () => {
  const worker = processor();
  const output = new Float32Array(128).fill(1);
  for (let block = 0; block < 40; block += 1) {
    worker.process([[new Float32Array(128).fill(0.75), new Float32Array(128).fill(-0.25)]], [[output]]);
  }
  assert.ok(output.every((sample) => sample === 0));
  const beforeFlush = worker.port.messages.filter((message) => message.type === "pcm");
  assert.deepEqual(beforeFlush.map((message) => message.buffer.byteLength), [1600, 1600]);
  worker.port.onmessage({ data: { type: "flush" } });
  const pcm = worker.port.messages.filter((message) => message.type === "pcm");
  assert.equal(pcm.reduce((sum, message) => sum + message.buffer.byteLength / 2, 0), Math.floor(5120 / 3));
  assert.equal(new DataView(pcm[0].buffer).getInt16(200, true), 8192);
  assert.deepEqual(worker.port.messages.at(-1), { type: "flushed", automatic: false });
  assert.equal(worker.process([[]], [[output]]), false);
  const count = worker.port.messages.length;
  worker.port.onmessage({ data: { type: "flush" } });
  assert.equal(worker.port.messages.length, count);
});

test("worklet queue stays bounded if the main thread stops acknowledging", () => {
  const worker = processor();
  for (let block = 0; block < 1200; block += 1) {
    worker.process([[new Float32Array(128)]], [[]]);
  }
  const messages = worker.port.messages;
  assert.equal(messages.at(-1).type, "error");
  assert.equal(messages.at(-1).code, "audio_overflow");
  assert.ok(messages.reduce((sum, message) => sum + (message.buffer?.byteLength || 0), 0) <= 64000);
  assert.equal(messages.filter((message) => message.type === "error").length, 1);
});

test("worklet acknowledgements permit continuous streaming and smaller packets", () => {
  const worker = processor(100);
  let handled = 0;
  for (let block = 0; block < 1200; block += 1) {
    worker.process([[new Float32Array(128)]], [[]]);
    for (; handled < worker.port.messages.length; handled += 1) {
      const message = worker.port.messages[handled];
      assert.equal(message.type, "pcm");
      assert.equal(message.buffer.byteLength, 200);
      worker.port.onmessage({ data: { type: "ack", bytes: message.buffer.byteLength } });
    }
  }
  assert.equal(worker.finished, false);
  assert.equal(worker.pendingBytes, 0);
  assert.equal(worker.pendingPackets, 0);
});

test("worklet stops on the audio clock and flushes the exact allowed duration", () => {
  const worker = processor(100, 0.1);
  for (let block = 0; block < 100; block += 1) worker.process([[new Float32Array(128)]], [[]]);
  const messages = worker.port.messages;
  assert.deepEqual(messages.at(-1), { type: "flushed", automatic: true });
  assert.equal(messages.reduce((sum, message) => sum + (message.buffer?.byteLength || 0), 0), 3200);
  assert.equal(worker.inputSamples, 4800);
});
