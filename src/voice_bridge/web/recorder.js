const BUFFER_LIMIT = 64000;
const DEFAULT_TIMEOUTS = Object.freeze({ init: 30000, ready: 20000, flush: 3000, done: 25000 });

function timeoutOptions(overrides) {
  if (!overrides || typeof overrides !== "object" || Array.isArray(overrides)) {
    throw new TypeError("timeouts must be an object of millisecond budgets");
  }
  const timeouts = { ...DEFAULT_TIMEOUTS };
  for (const [stage, value] of Object.entries(overrides)) {
    if (!Object.hasOwn(DEFAULT_TIMEOUTS, stage)) throw new TypeError(`Unknown timeout stage: ${stage}`);
    // Browsers overflow setTimeout above the signed 32-bit range into an
    // immediate timer, so reject those values along with non-finite budgets.
    if (!Number.isFinite(value) || value < 1 || value > 2147483647) {
      throw new RangeError(`timeouts.${stage} must be between 1 and 2147483647 milliseconds`);
    }
    timeouts[stage] = value;
  }
  return Object.freeze(timeouts);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  // Auto-stop and cancellation can settle a promise before a caller awaits it.
  // Callers still receive its original rejection when they do await it.
  promise.catch(() => {});
  return { promise, resolve, reject };
}

function failure(code, message, confirmedText = "") {
  return Object.assign(new Error(message), { code, confirmed_text: confirmedText });
}

function stopTracks(stream) {
  for (const track of stream?.getTracks() || []) {
    try { track.stop(); } catch { /* Already released by the browser. */ }
  }
}

function invokeAsync(callback) {
  // Invoke immediately within the user gesture, but turn synchronous exceptions
  // into rejections so Promise.all observes every initialization operation.
  return new Promise((resolve) => { resolve(callback()); });
}

/**
 * Browser PCM client for /v1/asr. Call start() from a user gesture.
 * start() resolves when recording begins; stop() resolves with the raw done
 * message after the final transcript. Both reject on failure. cancel() is
 * immediate and rejects pending operations with code "cancelled".
 *
 * Events: statechange {state}, transcript (raw server message), done (raw
 * server message), error {code, message, confirmed_text}. A new start clears
 * fullText, confirmedText and result; stop preserves them while waiting.
 * When provider is omitted, start() reads /v1/asr/capabilities from the page
 * origin inside the existing init budget and sends its default_provider
 * explicitly. An explicit provider never fetches capabilities.
 * timeouts accepts init/ready/flush/done in milliseconds. Increase ready and
 * done with the corresponding backend budgets, leaving time for transport.
 */
export class RealtimeTranscriber extends EventTarget {
  constructor({
    url,
    provider = null,
    language = null,
    workletUrl = new URL("./pcm-worklet.js", import.meta.url),
    timeouts = {},
  } = {}) {
    super();
    this.url = url;
    this.provider = provider;
    this.language = language;
    this.workletUrl = workletUrl;
    this._timeouts = timeoutOptions(timeouts);
    this._stateEvents = [];
    this._dispatchingState = false;
    this.fullText = "";
    this.confirmedText = "";
    this.result = null;
    this._state = "idle";
    this._session = null;
  }

  get state() { return this._state; }

  async start() {
    if (this._session && !this._session.ended) {
      throw failure("invalid_state", "A recording session is already active", this.confirmedText);
    }
    const session = {
      started: deferred(),
      completed: deferred(),
      timers: new Map(),
      ended: false,
      ready: false,
      stopRequested: false,
      stopSent: false,
      flushed: false,
      capturing: false,
      stream: null,
      context: null,
      source: null,
      worklet: null,
      gain: null,
      ws: null,
      capabilitiesAbort: null,
    };
    this._session = session;
    this.fullText = "";
    this.confirmedText = "";
    this.result = null;
    session.onPageHide = () => { if (this._current(session)) this.cancel(); };
    globalThis.addEventListener?.("pagehide", session.onPageHide);
    this._timer(session, "init", this._timeouts.init, () => {
      this._fail(session, "initialization_timeout", "Microphone or connection setup timed out");
    });
    this._changeState("connecting");
    // A state listener can cancel synchronously, even before permission opens.
    if (this._current(session)) void this._initialize(session);
    return session.started.promise;
  }

  async stop() {
    const session = this._session;
    if (!session) return;
    if (this.state === "connecting") {
      this.cancel(); // Recording has not begun; discard this connection.
      return;
    }
    if (session.ended) return session.completed.promise;
    if (!session.stopRequested) {
      session.stopRequested = true;
      this._clearTimer(session, "auto");
      this._changeState("stopping");
      if (this._current(session)) {
        this._timer(session, "flush", this._timeouts.flush, () => {
          this._fail(session, "flush_timeout", "Microphone audio could not be finalized");
        });
        try {
          session.worklet.port.postMessage({ type: "flush" });
        } catch {
          this._fail(session, "audio_error", "Microphone audio could not be finalized");
        }
      }
    }
    return session.completed.promise;
  }

  cancel() {
    const session = this._session;
    if (!session || session.ended) return;
    const error = failure("cancelled", "Recording cancelled", this.confirmedText);
    session.ended = true;
    session.started.reject(error);
    session.completed.reject(error);
    this._release(session);
    // Discard provisional text, retaining only the last confirmed transcript.
    this.fullText = this.confirmedText;
    this._changeState("cancelled");
  }

  async _initialize(session) {
    try {
      if (globalThis.isSecureContext === false) {
        throw failure("insecure_context", "Microphone access requires HTTPS or localhost");
      }
      const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
      if (!globalThis.navigator?.mediaDevices?.getUserMedia || !AudioContextClass
        || !globalThis.AudioWorkletNode || !globalThis.WebSocket) {
        throw failure("unsupported_browser", "This browser does not support microphone AudioWorklets");
      }
      const context = new AudioContextClass({ latencyHint: "interactive" });
      session.context = context;
      if (!context.audioWorklet) {
        throw failure("unsupported_browser", "AudioWorklet is unavailable; use HTTPS or localhost");
      }
      context.onstatechange = () => {
        if (this._current(session) && session.capturing && context.state !== "running") {
          this._fail(session, "audio_suspended", "Microphone audio was suspended; start a new recording");
        }
      };
      // Resume inside the original user gesture, before awaiting permission.
      // Prepare everything before WebSocket connects: the server's start
      // deadline must never include a permission dialog or module download.
      const resumed = invokeAsync(() => context.resume());
      const streamReady = invokeAsync(() => navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      })).then((stream) => {
        if (!this._current(session)) {
          stopTracks(stream); // Permission can resolve long after cancellation.
          return;
        }
        session.stream = stream;
        const tracks = stream.getAudioTracks();
        if (!tracks.length || tracks.some((track) => track.readyState === "ended")) {
          throw failure("microphone_ended", "No active microphone track is available");
        }
        for (const track of tracks) {
          track.onended = () => {
            if (this._current(session) && !session.stopRequested) {
              this._fail(session, "microphone_ended", "The microphone disconnected or permission was revoked");
            }
          };
        }
      });
      // Network setup starts only after resume() and getUserMedia() were invoked in
      // the user gesture. It runs alongside microphone/module setup and stays inside
      // the existing init budget; cancellation aborts it.
      const providerReady = this.provider === null || this.provider === undefined
        ? this._defaultProvider(session)
        : Promise.resolve(this.provider);
      const [, , , provider] = await Promise.all([
        resumed, streamReady, invokeAsync(() => context.audioWorklet.addModule(this.workletUrl)),
        providerReady,
      ]);
      if (!this._current(session)) return;
      if (context.state !== "running") {
        throw failure("audio_suspended", "Audio could not start; start recording from a button click");
      }
      session.worklet = new AudioWorkletNode(context, "pcm-recorder", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: "explicit",
      });
      session.worklet.port.onmessage = ({ data }) => this._onAudio(session, data);
      session.worklet.onprocessorerror = () => {
        this._fail(session, "audio_error", "Microphone audio processing failed");
      };
      session.source = context.createMediaStreamSource(session.stream);
      session.gain = context.createGain();
      session.gain.gain.value = 0;
      // Do not connect the microphone graph until ready arrives.
      const url = new URL(this.url || "/v1/asr", globalThis.location?.href);
      if (url.protocol === "http:") url.protocol = "ws:";
      if (url.protocol === "https:") url.protocol = "wss:";
      if (!["ws:", "wss:"].includes(url.protocol)) {
        throw failure("invalid_url", "Use a ws:// or wss:// transcription URL");
      }
      const ws = new WebSocket(url.href);
      session.ws = ws;
      ws.binaryType = "arraybuffer";
      ws.onopen = () => {
        if (!this._current(session)) return;
        this._clearTimer(session, "init");
        this._timer(session, "ready", this._timeouts.ready, () => {
          this._fail(session, "ready_timeout", "Speech recognition did not become ready in time");
        });
        this._send(session, JSON.stringify({
          type: "start",
          provider,
          language: this.language,
          audio: { encoding: "pcm_s16le", sample_rate: 16000, channels: 1 },
        }));
      };
      ws.onmessage = ({ data }) => this._onServer(session, data);
      ws.onerror = () => this._fail(session, "connection_error", "Could not connect to speech recognition");
      ws.onclose = () => {
        this._fail(session, "connection_closed", "Speech recognition disconnected before completion");
      };
    } catch (error) {
      const code = (typeof error.code === "string" && error.code) || ({
        NotAllowedError: "microphone_denied",
        NotFoundError: "microphone_not_found",
        NotReadableError: "microphone_unavailable",
      }[error.name]) || "initialization_error";
      this._fail(session, code, error.message || "Could not initialize microphone recording");
    }
  }

  async _defaultProvider(session) {
    if (typeof globalThis.fetch !== "function") {
      throw failure("unsupported_browser", "This browser cannot read the speech provider configuration");
    }
    const page = globalThis.location?.href;
    if (!page) throw failure("unsupported_browser", "The page address is unavailable");
    const controller = new AbortController();
    session.capabilitiesAbort = controller;
    try {
      let response;
      try {
        response = await globalThis.fetch(new URL("/v1/asr/capabilities", page), {
          cache: "no-store",
          credentials: "same-origin",
          signal: controller.signal,
        });
      } catch (error) {
        if (error?.name === "AbortError") throw error;
        throw failure("capabilities_error", "Could not read the configured speech provider");
      }
      if (!response?.ok) {
        throw failure("capabilities_error", "Could not read the configured speech provider");
      }
      let payload;
      try {
        payload = await response.json();
      } catch {
        throw failure("capabilities_error", "Speech provider capabilities are invalid");
      }
      const provider = payload?.default_provider;
      if (provider === null || provider === undefined) {
        throw failure("provider_unavailable", "The service has no configured speech provider");
      }
      if (provider !== "qwen" && provider !== "funasr") {
        throw failure("capabilities_error", "Speech provider capabilities are invalid");
      }
      return provider;
    } finally {
      if (session.capabilitiesAbort === controller) session.capabilitiesAbort = null;
    }
  }

  _onServer(session, data) {
    if (!this._current(session)) return;
    try {
      if (typeof data !== "string") throw new Error("Expected a JSON message from speech recognition");
      const message = JSON.parse(data);
      if (!message || typeof message !== "object") throw new Error("Invalid speech recognition message");
      if (message.type === "ready") {
        this._onReady(session, message);
      } else if (message.type === "error") {
        this._fail(session, message.code || "server_error", message.message || "Speech recognition failed",
          typeof message.confirmed_text === "string" ? message.confirmed_text : this.confirmedText);
      } else if (message.type === "transcript") {
        if (!session.ready) throw new Error("Transcript received before ready");
        if (typeof message.confirmed_text === "string") this.confirmedText = message.confirmed_text;
        if (typeof message.full_text === "string") this.fullText = message.full_text;
        this.dispatchEvent(new CustomEvent("transcript", { detail: message }));
      } else if (message.type === "done") {
        if (!session.stopSent) throw new Error("Completion received before stop");
        this._finish(session, message);
      } else {
        throw new Error("Unknown speech recognition message");
      }
    } catch (error) {
      this._fail(session, "protocol_error", error.message);
    }
  }

  _onReady(session, message) {
    if (session.ready) throw new Error("Duplicate ready message");
    const limit = Math.min(message.max_session_seconds, message.max_audio_seconds);
    const chunkBytes = message.max_audio_chunk_bytes;
    if (!Number.isFinite(limit) || limit <= 0 || !Number.isInteger(chunkBytes) || chunkBytes < 2
      || message.audio?.encoding !== "pcm_s16le" || message.audio?.sample_rate !== 16000
      || message.audio?.channels !== 1) {
      throw new Error("Server advertised unsupported audio limits or format");
    }
    if (session.context.state !== "running") {
      this._fail(session, "audio_suspended", "Microphone audio was suspended before recording could begin");
      return;
    }
    session.ready = true;
    session.packetBytes = Math.min(1600, chunkBytes - chunkBytes % 2);
    const durationSeconds = limit - Math.min(2, limit * 0.1);
    this._clearTimer(session, "ready");
    this._timer(session, "auto", durationSeconds * 1000, () => { this.stop().catch(() => {}); });
    session.capturing = true;
    session.worklet.port.postMessage({
      type: "start",
      packetSamples: session.packetBytes / 2,
      durationSeconds,
    });
    session.source.connect(session.worklet);
    session.worklet.connect(session.gain);
    session.gain.connect(session.context.destination);
    session.started.resolve();
    this._changeState("recording");
  }

  _onAudio(session, message) {
    if (!this._current(session)) return;
    try {
      if (message.type === "pcm") {
        if (!session.ready || session.stopSent || session.flushed
          || !(message.buffer instanceof ArrayBuffer) || !message.buffer.byteLength
          || message.buffer.byteLength % 2 || message.buffer.byteLength > session.packetBytes) {
          throw new Error("Invalid microphone audio packet");
        }
        if (this._send(session, message.buffer)) {
          session.worklet.port.postMessage({ type: "ack", bytes: message.buffer.byteLength });
        }
      } else if (message.type === "flushed") {
        if (session.flushed) return;
        if (!session.ready || (!session.stopRequested && !message.automatic)) {
          throw new Error("Unexpected microphone flush");
        }
        session.flushed = true;
        if (!session.stopRequested) {
          session.stopRequested = true;
          this._clearTimer(session, "auto");
          this._changeState("stopping");
          if (!this._current(session)) return;
        }
        this._clearTimer(session, "flush");
        this._releaseAudio(session);
        // This handler runs after every earlier PCM message on the same port;
        // WebSocket.send is synchronous and preserves that exact ordering.
        session.stopSent = true;
        if (this._send(session, JSON.stringify({ type: "stop" }))) {
          this._timer(session, "done", this._timeouts.done, () => {
            this._fail(session, "done_timeout", "Timed out waiting for the final transcript");
          });
        }
      } else if (message.type === "error") {
        this._fail(session, message.code || "audio_error", message.message || "Microphone audio failed");
      } else {
        throw new Error("Unknown microphone audio message");
      }
    } catch (error) {
      this._fail(session, "audio_error", error.message);
    }
  }

  _send(session, data) {
    if (!this._current(session)) return false;
    const ws = session.ws;
    if (ws?.readyState !== 1) {
      this._fail(session, "connection_closed", "Speech recognition is not connected");
      return false;
    }
    const bytes = typeof data === "string" ? new TextEncoder().encode(data).length : data.byteLength;
    if (ws.bufferedAmount + bytes > BUFFER_LIMIT) {
      this._fail(session, "network_backpressure", "The network is too slow to send microphone audio");
      return false;
    }
    try {
      ws.send(data);
      if (ws.bufferedAmount > BUFFER_LIMIT) {
        this._fail(session, "network_backpressure", "The network is too slow to send microphone audio");
        return false;
      }
      return true;
    } catch {
      this._fail(session, "connection_error", "Could not send audio to speech recognition");
      return false;
    }
  }

  _finish(session, message) {
    if (!this._current(session)) return;
    session.ended = true;
    this.result = message;
    this.fullText = this.confirmedText = message.full_text || "";
    session.completed.resolve(message);
    this._release(session);
    this._changeState("done");
    if (this._session === session) this.dispatchEvent(new CustomEvent("done", { detail: message }));
  }

  _fail(session, code, message, confirmedText = this.confirmedText) {
    if (!this._current(session)) return;
    session.ended = true;
    this.fullText = this.confirmedText = confirmedText;
    const error = failure(code, message, confirmedText);
    session.started.reject(error);
    session.completed.reject(error);
    this._release(session);
    this._changeState("error");
    if (this._session === session) {
      this.dispatchEvent(new CustomEvent("error", {
        detail: { code, message, confirmed_text: confirmedText },
      }));
    }
  }

  _current(session) { return this._session === session && !session.ended; }

  _changeState(state) {
    this._state = state;
    this._stateEvents.push(state);
    if (this._dispatchingState) return;
    this._dispatchingState = true;
    try {
      // A listener may cancel or restart synchronously. Finish notifying every
      // listener of this transition before delivering the resulting transition.
      while (this._stateEvents.length) {
        const next = this._stateEvents.shift();
        this.dispatchEvent(new CustomEvent("statechange", { detail: { state: next } }));
      }
    } finally {
      this._dispatchingState = false;
    }
  }

  _timer(session, name, delay, callback) {
    this._clearTimer(session, name);
    session.timers.set(name, setTimeout(() => {
      session.timers.delete(name);
      if (this._current(session)) callback();
    }, delay));
  }

  _clearTimer(session, name) {
    clearTimeout(session.timers.get(name));
    session.timers.delete(name);
  }

  _releaseAudio(session) {
    session.capturing = false;
    if (session.stream) for (const track of session.stream.getAudioTracks()) track.onended = null;
    stopTracks(session.stream);
    for (const node of [session.source, session.worklet, session.gain]) {
      try { node?.disconnect(); } catch { /* Already disconnected. */ }
    }
    if (session.worklet) {
      session.worklet.onprocessorerror = null;
      session.worklet.port.onmessage = null;
      session.worklet.port.close();
    }
    if (session.context) {
      session.context.onstatechange = null;
      try { Promise.resolve(session.context.close()).catch(() => {}); } catch { /* Already closed. */ }
    }
    session.stream = session.source = session.worklet = session.gain = session.context = null;
  }

  _release(session) {
    globalThis.removeEventListener?.("pagehide", session.onPageHide);
    session.capabilitiesAbort?.abort();
    session.capabilitiesAbort = null;
    for (const timer of session.timers.values()) clearTimeout(timer);
    session.timers.clear();
    this._releaseAudio(session);
    if (session.ws) {
      session.ws.onopen = session.ws.onmessage = session.ws.onerror = session.ws.onclose = null;
      try { session.ws.close(); } catch { /* Already disconnected. */ }
      session.ws = null;
    }
  }
}
