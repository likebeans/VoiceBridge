import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const source = await readFile(new URL("../../src/voice_bridge/web/demo.js", import.meta.url), "utf8");

const providers = [
  { id: "funasr", model: "fun-asr-realtime", configured: true,
    auto_detect: true, language_support_known: true,
    languages: [{ code: "zh", name: "中文" }, { code: "fr", name: "法语" },
      { code: "fil", name: "菲律宾语" }] },
  { id: "qwen", model: "qwen3-asr-flash-realtime", configured: true,
    auto_detect: true, language_support_known: true,
    languages: [{ code: "zh", name: "中文" }, { code: "fr", name: "法语" },
      { code: "tr", name: "土耳其语" }, { code: "fil", name: "菲律宾语" }] },
];

async function page(t, configuredProviders = providers) {
  class Element extends EventTarget {
    constructor() {
      super();
      this.value = "";
      this.textContent = "";
      this.selectionStart = this.selectionEnd = 0;
      this.disabled = false;
      this.readOnly = false;
      this.hidden = true;
      this.dataset = {};
    }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    replaceChildren(...children) { this.children = children; }
    focus() {}
    click() { this.dispatchEvent(new Event("click")); }
  }
  class Recorder extends EventTarget {
    static instances = [];
    constructor(options) { super(); this.options = options; this.state = "idle"; Recorder.instances.push(this); }
    emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }
    stateTo(state) { this.state = state; this.emit("statechange", { state }); }
    async start() { this.stateTo("connecting"); this.stateTo("recording"); }
    async stop() { this.stateTo("stopping"); }
    cancel() { this.stateTo("cancelled"); }
    transcript(full_text, confirmed_text = "") {
      this.emit("transcript", { type: "transcript", session_id: "test", full_text, confirmed_text });
    }
    done(full_text) {
      this.stateTo("done");
      this.emit("done", { type: "done", session_id: "test", full_text, audio_seconds: 0.1 });
    }
    fail(confirmed_text = "") {
      this.stateTo("error");
      this.emit("error", { code: "upstream_closed", message: "Connection ended", confirmed_text });
    }
  }
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const timers = new Set();
  const context = createContext({
    RealtimeTranscriber: Recorder, URL, performance,
    document: { getElementById: $, createElement: () => new Element() },
    location: { href: "http://localhost:8000/demo", protocol: "http:" },
    window: { isSecureContext: true, AudioWorkletNode: class {}, addEventListener() {} },
    navigator: { mediaDevices: { getUserMedia() {} } },
    fetch: async () => ({ ok: true, json: async () => ({
      providers: configuredProviders,
      default_provider: "funasr", max_session_seconds: 120, max_audio_seconds: 120,
    }) }),
    setInterval: (callback) => { timers.add(callback); return callback; },
    clearInterval: (callback) => timers.delete(callback),
    setTimeout: (callback) => { timers.add(callback); return callback; },
  });
  // Execute the actual UI module with only its recording transport substituted.
  // No browser build tools, microphone permissions or paid upstream are needed.
  const body = source.replace(/^import .*?;\s*/m, "");
  await runInContext(`(async () => { ${body} })()`, context);
  t.after(() => { for (const recorder of Recorder.instances) if (["connecting", "recording", "stopping"].includes(recorder.state)) recorder.cancel(); });
  return {
    $, timers,
    input: $("transcript"),
    start() { $("record").click(); return Recorder.instances.at(-1); },
  };
}

function selection(input) { return [input.value, input.selectionStart, input.selectionEnd]; }
function change(element, value) { element.value = value; element.dispatchEvent(new Event("change")); }
function choices(view) { return view.$("language").children.map((option) => option.value); }

test("languages come from capabilities and automatic detection is the default", async (t) => {
  const view = await page(t);
  assert.deepEqual(Array.from(choices(view)), ["", "zh", "fr", "fil"]);
  assert.equal(view.$("language").value, "");
  assert.equal(view.$("language").disabled, false);
  assert.match(view.$("model-note").textContent, /3 种语言/);
  assert.equal(view.start().options.language, null);
});

test("switching provider preserves a supported language", async (t) => {
  const view = await page(t);
  change(view.$("language"), "fr");
  change(view.$("provider"), "qwen");
  assert.equal(view.$("language").value, "fr");
  assert.deepEqual(Array.from(choices(view)), ["", "zh", "fr", "tr", "fil"]);
  const recorder = view.start();
  assert.equal(recorder.options.provider, "qwen");
  assert.equal(recorder.options.language, "fr");
  assert.equal(view.$("language").disabled, true);
  recorder.done("Bonjour.");
  assert.equal(view.$("language").disabled, false);
});

test("an incompatible provider requires an explicit language choice before recording", async (t) => {
  const view = await page(t);
  change(view.$("provider"), "qwen");
  change(view.$("language"), "tr");
  change(view.$("provider"), "funasr");
  assert.equal(view.$("language").value, "tr");
  assert.equal(view.$("language").children.at(-1).disabled, true);
  assert.equal(view.$("record").disabled, true);
  assert.equal(view.$("notice").hidden, false);
  assert.match(view.$("notice").textContent, /之前选择的语言/);
  change(view.$("language"), "");
  assert.equal(view.$("record").disabled, false);
  assert.equal(view.$("notice").hidden, true);
  assert.equal(view.start().options.language, null);
});

test("switching back restores the previously selected supported language", async (t) => {
  const view = await page(t);
  change(view.$("provider"), "qwen");
  change(view.$("language"), "tr");
  change(view.$("provider"), "funasr");
  change(view.$("provider"), "qwen");
  assert.equal(view.$("language").value, "tr");
  assert.equal(view.$("record").disabled, false);
  assert.equal(view.$("notice").hidden, true);
});

test("an unknown model does not advertise a confirmed language list", async (t) => {
  const view = await page(t, [{ id: "funasr", model: "custom-model", configured: true,
    languages: [], auto_detect: null, language_support_known: false }]);
  assert.deepEqual(Array.from(choices(view)), [""]);
  assert.match(view.$("language").children[0].textContent, /能力未确认/);
  assert.match(view.$("model-note").textContent, /语言范围尚未确认/);
  assert.equal(view.start().options.language, null);
});

test("Filipino uses the public code and unsupported-language errors have a Chinese message", async (t) => {
  const view = await page(t);
  change(view.$("language"), "fil");
  const recorder = view.start();
  assert.equal(recorder.options.language, "fil");
  recorder.stateTo("error");
  recorder.emit("error", { code: "invalid_language", confirmed_text: "", message: "private" });
  assert.match(view.$("notice").textContent, /不支持这种语言/);
  assert.equal(view.$("record").disabled, false);
});

test("empty recording preserves the original selected text and selection", async (t) => {
  const view = await page(t);
  view.input.value = "原有重要文字";
  view.input.setSelectionRange(2, 4);
  const recorder = view.start();
  recorder.transcript("");
  assert.deepEqual(selection(view.input), ["原有重要文字", 2, 4]);
  recorder.transcript("暂定");
  recorder.transcript("");
  assert.deepEqual(selection(view.input), ["原有重要文字", 2, 4]);
  recorder.done("");
  assert.deepEqual(selection(view.input), ["原有重要文字", 2, 4]);
  assert.equal(view.input.readOnly, false);
  assert.equal(view.$("record").disabled, false);
  assert.equal(view.timers.size, 0);
});

test("successive recordings insert at the speech cursor without moving the trailing text", async (t) => {
  const view = await page(t);
  view.input.value = "原有重要文字";
  view.input.setSelectionRange(2, 4);
  view.start().done("新的");
  assert.deepEqual(selection(view.input), ["原有新的文字", 4, 4]);
  view.start().done("内容");
  assert.deepEqual(selection(view.input), ["原有新的内容文字", 6, 6]);
});

for (const terminal of ["cancel", "fail"]) {
  test(`${terminal} restores the original selection after provisional text`, async (t) => {
    const view = await page(t);
    view.input.value = "原有重要文字";
    view.input.setSelectionRange(2, 4);
    const recorder = view.start();
    recorder.transcript("暂定");
    recorder[terminal]();
    assert.deepEqual(selection(view.input), ["原有重要文字", 2, 4]);
    assert.equal(view.input.readOnly, false);
    assert.equal(view.timers.size, 0);
  });
}

test("failure keeps confirmed text, while callbacks from an older session cannot alter the new draft", async (t) => {
  const view = await page(t);
  view.input.value = "原有重要文字";
  view.input.setSelectionRange(2, 4);
  const previous = view.start();
  previous.transcript("已确认临时", "已确认");
  previous.fail("已确认");
  assert.deepEqual(selection(view.input), ["原有已确认文字", 5, 5]);
  const current = view.start();
  current.transcript("新的");
  previous.done("旧结果");
  assert.deepEqual(selection(view.input), ["原有已确认新的文字", 7, 7]);
  current.cancel();
  assert.deepEqual(selection(view.input), ["原有已确认文字", 5, 5]);
});
