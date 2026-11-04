import { RealtimeTranscriber } from './recorder.js';

const $ = (id) => document.getElementById(id);
const endpoint = new URL('/v1/asr', location.href);
endpoint.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
$('endpoint').textContent = endpoint.href;
let capabilities;
let recorder;
let before = '';
let after = '';
let original = '';
let originalSelectionStart = 0;
let originalSelectionEnd = 0;
let audioStarted = null;
let stopRequested = null;
let firstTextAt = null;
let partialCount = 0;
let timer;
const log = [];
const activeStates = new Set(['connecting', 'recording', 'stopping']);
const labels = {
  idle: '准备就绪', connecting: '准备麦克风与连接…', recording: '正在录音',
  stopping: '正在收尾，请稍候…', done: '转写完成', error: '转写中断', cancelled: '已取消本次录音',
};
const errors = {
  server_busy: '当前转写服务繁忙，请稍后再试。',
  shutting_down: '服务正在重启，请稍后重新开始录音。',
  microphone_denied: '麦克风权限被拒绝。请在浏览器地址栏的权限设置中允许麦克风，再重试。',
  microphone_not_found: '没有找到麦克风。请连接输入设备后重试。',
  microphone_unavailable: '无法读取麦克风。请检查系统权限，或关闭正在占用麦克风的应用。',
  microphone_ended: '麦克风已断开或权限已被撤销，请检查设备后重新录音。',
  audio_suspended: '浏览器暂停了音频采集，请返回本页重新开始录音。',
  audio_error: '浏览器无法继续处理音频，请重新开始录音。',
  audio_overflow: '页面未能及时处理音频，请关闭繁忙的标签页后重新录音。',
  network_backpressure: '网络发送音频过慢，本次录音已中断，请检查连接后重试。',
  connection_error: '无法连接转写服务，请确认后端正在运行。',
  connection_closed: '转写连接提前断开，已保留收到的确认文字。',
  initialization_timeout: '麦克风准备超时，请确认权限后重新开始录音。',
  ready_timeout: '识别服务准备超时，请稍后重试。',
  flush_timeout: '浏览器未能完成音频收尾，已保留收到的确认文字。',
  done_timeout: '等待最终文字超时，已保留收到的确认文字。',
  insecure_context: '录音需要 HTTPS 或本机 localhost 地址。',
  unsupported_browser: '当前浏览器不支持此录音方式，请使用新版 Chrome 或 Edge。',
  provider_unavailable: '所选识别服务还没有配置密钥，请选择另一个已配置的服务。',
  invalid_language: '所选模型不支持这种语言，请根据语言菜单重新选择。',
  upstream_connection_error: '无法连接识别服务，请检查服务端网络和模型配置。',
  upstream_auth_error: '识别服务拒绝了认证，请在服务端检查模型密钥与权限。',
  upstream_rate_limited: '识别服务当前限流，请稍后重新开始录音。',
  upstream_unavailable: '识别服务暂时不可用，请稍后重试。',
  upstream_rejected: '识别服务拒绝连接，请在服务端检查模型与接入配置。',
  upstream_error: '识别服务返回错误，请检查服务端的模型权限和额度。',
  upstream_timeout: '连接识别服务超时，请稍后重试。',
  finish_timeout: '最后一句等待超时，已保留收到的确认文字。',
  idle_timeout: '服务没有持续收到音频，请检查麦克风后重试。',
  session_limit: '已达到单次录音时长上限，请开始新的一次录音。',
  audio_limit: '已达到音频时长上限，请开始新的一次录音。',
  incomplete_transcript: '识别服务未完成全部文字，已保留已确认的部分。',
};

function report(message) {
  $('notice').textContent = message;
  $('notice').hidden = false;
}
function appendLog(event) {
  log.push(JSON.stringify(event));
  if (log.length > 30) log.shift();
  $('event-log').textContent = log.join('\n');
  $('event-log').scrollTop = $('event-log').scrollHeight;
}
function restoreOriginal() {
  $('transcript').value = original;
  $('transcript').setSelectionRange(originalSelectionStart, originalSelectionEnd);
}
function replaceVoice(text) {
  if (!text) {
    restoreOriginal();
    return;
  }
  $('transcript').value = before + text + after;
  const caret = before.length + text.length;
  $('transcript').setSelectionRange(caret, caret);
}
function elapsed() {
  const seconds = audioStarted === null ? 0 : Math.floor((performance.now() - audioStarted) / 1000);
  $('elapsed').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}
function renderState(state) {
  const active = activeStates.has(state);
  $('status').textContent = labels[state] || state;
  $('status-dot').dataset.state = state;
  $('provider').disabled = active || !capabilities?.providers.some((item) => item.configured);
  $('language').disabled = active || !selectedProvider()?.configured;
  $('transcript').readOnly = active;
  $('clear').disabled = active;
  $('copy').disabled = active;
  $('record').disabled = state === 'connecting' || state === 'stopping'
    || !selectedProvider()?.configured || !supportsSelectedLanguage();
  $('record').dataset.recording = String(state === 'recording');
  $('record-label').textContent = state === 'recording' ? '停止录音' : state === 'stopping' ? '等待定稿…' : state === 'connecting' ? '正在准备…' : '开始录音';
  $('cancel').hidden = !active;
  if (state === 'recording') {
    audioStarted = performance.now();
    timer = setInterval(elapsed, 250);
    $('hint').textContent = '可以开始说话。文字会持续更新，点击停止后等待最后一句完成。';
    $('result-kind').textContent = '识别中 · 暂时锁定编辑';
  } else if (state === 'stopping') {
    stopRequested = performance.now();
    clearInterval(timer);
    elapsed();
    $('hint').textContent = '麦克风正在停止，最后一段文字仍可能更新。';
    $('result-kind').textContent = '等待最终结果';
  } else if (!active) {
    clearInterval(timer);
    $('hint').textContent = state === 'done' ? '转写已完成。你可以编辑或复制，文字不会自动发送给 agent。' : '准备好后再开始说话；停止录音不会自动发送给 agent。';
    $('result-kind').textContent = state === 'done' ? '已定稿 · 可以编辑' : '可以编辑';
  }
}

async function startRecording() {
  original = $('transcript').value;
  originalSelectionStart = $('transcript').selectionStart;
  originalSelectionEnd = $('transcript').selectionEnd;
  before = original.slice(0, originalSelectionStart);
  after = original.slice(originalSelectionEnd);
  $('notice').hidden = true;
  $('timing').textContent = '完成一次录音后显示首次出字和收尾耗时。';
  $('session-id').textContent = '正在建立';
  audioStarted = null;
  firstTextAt = null;
  stopRequested = null;
  partialCount = 0;
  log.length = 0;
  $('event-log').textContent = '准备建立连接。';
  elapsed();
  const current = new RealtimeTranscriber({
    url: endpoint.href, provider: $('provider').value, language: $('language').value || null,
  });
  recorder = current;
  current.addEventListener('statechange', ({ detail }) => {
    if (recorder !== current) return;
    renderState(detail.state);
    appendLog({ type: 'client_state', state: detail.state });
    if (detail.state === 'cancelled') restoreOriginal();
  });
  current.addEventListener('transcript', ({ detail }) => {
    if (recorder !== current) return;
    if (firstTextAt === null && detail.full_text) firstTextAt = performance.now();
    if (!detail.is_final) partialCount += 1;
    replaceVoice(detail.full_text);
    $('session-id').textContent = detail.session_id;
    appendLog(detail);
  });
  current.addEventListener('done', ({ detail }) => {
    if (recorder !== current) return;
    replaceVoice(detail.full_text);
    $('session-id').textContent = detail.session_id;
    const now = performance.now();
    const first = firstTextAt !== null && audioStarted !== null ? `${Math.round(firstTextAt - audioStarted)} ms` : '无有效语音';
    const drain = stopRequested === null ? '—' : `${Math.round(now - stopRequested)} ms`;
    $('timing').textContent = `首次出字 ${first} · 收尾 ${drain} · 中间结果 ${partialCount} 次 · 已上传音频 ${detail.audio_seconds.toFixed(2)} 秒（本次测量）`;
    appendLog(detail);
    if (!detail.full_text) $('hint').textContent = '本次未识别到文字。请确认麦克风输入正常，再说一句完整的话。';
  });
  current.addEventListener('error', ({ detail }) => {
    if (recorder !== current) return;
    replaceVoice(detail.confirmed_text);
    report(errors[detail.code] || detail.message || '转写失败，请检查麦克风和连接后重试。');
    appendLog(detail);
  });
  try {
    await current.start();
  } catch (error) {
    if (recorder === current && current.state !== 'cancelled' && $('notice').hidden) {
      renderState('error');
      report(errors[error.name] || errors[error.code] || error.message || '无法开始录音。');
    }
  }
}

$('record').addEventListener('click', async () => {
  if (recorder?.state === 'recording') {
    try { await recorder.stop(); } catch { /* error event displays details */ }
  } else {
    await startRecording();
  }
});
$('cancel').addEventListener('click', () => {
  Promise.resolve(recorder?.cancel()).catch(() => {});
});
$('clear').addEventListener('click', () => {
  $('transcript').value = '';
  $('transcript').focus();
});
$('copy').addEventListener('click', async () => {
  const text = $('transcript').value;
  if (!text) { $('transcript').focus(); return; }
  try {
    await navigator.clipboard.writeText(text);
    $('copy').textContent = '已复制';
    setTimeout(() => { $('copy').textContent = '复制文字'; }, 1600);
  } catch {
    $('transcript').focus();
    $('transcript').select();
    report('已选中文字，请按 Ctrl+C 或 ⌘C 复制。');
  }
});
window.addEventListener('pagehide', () => { recorder?.cancel(); });

function selectedProvider() {
  return capabilities?.providers.find((item) => item.id === $('provider').value);
}
function supportsSelectedLanguage() {
  return !$('language').value
    || selectedProvider()?.languages?.some((item) => item.code === $('language').value);
}
function updateModelNote() {
  const selected = selectedProvider();
  if (!selected) {
    $('model-note').textContent = '请在服务端配置至少一个识别服务。';
    return;
  }
  const coverage = selected.language_support_known
    ? `${selected.languages.length} 种语言${selected.auto_detect ? ' · 支持自动识别' : ''}`
    : '此模型的语言范围尚未确认';
  $('model-note').textContent = `${selected.model} · ${coverage} · 单次最长 ${Math.min(capabilities.max_session_seconds, capabilities.max_audio_seconds)} 秒`;
}
function updateLanguages() {
  const previous = $('language').value;
  const selected = selectedProvider();
  const languages = selected?.languages || [];
  const choices = [{ code: '', name: selected?.auto_detect
    ? '自动识别' : '不指定语言（模型能力未确认）' }, ...languages];
  const unsupported = previous && !languages.some((item) => item.code === previous);
  if (unsupported) {
    const name = capabilities.providers.flatMap((item) => item.languages || [])
      .find((item) => item.code === previous)?.name || previous;
    choices.push({ code: previous, name: `${name}（当前服务不支持）`, disabled: true });
    report('当前服务不支持之前选择的语言，请选择自动识别或菜单中的其他语言。');
  }
  $('language').replaceChildren(...choices.map(({ code, name, disabled }) => {
    const option = document.createElement('option');
    option.value = code;
    option.textContent = name;
    option.disabled = Boolean(disabled);
    return option;
  }));
  $('language').value = previous;
  updateModelNote();
}
$('provider').addEventListener('change', () => {
  $('notice').hidden = true;
  updateLanguages();
  renderState(recorder?.state || 'idle');
});
$('language').addEventListener('change', () => {
  $('notice').hidden = true;
  renderState(recorder?.state || 'idle');
});

try {
  const response = await fetch('/v1/asr/capabilities', { cache: 'no-store' });
  if (response.status === 401) {
    throw new Error('服务已启用 API Key 鉴权，请通过会注入 X-API-Key 的业务后端或网关打开试录页。');
  }
  if (!response.ok) throw new Error('无法读取服务配置。');
  capabilities = await response.json();
  $('provider').replaceChildren(...capabilities.providers.map((provider) => {
    const option = document.createElement('option');
    option.value = provider.id;
    option.textContent = `${provider.id === 'funasr' ? 'FunASR' : 'Qwen ASR'}${provider.configured ? '' : '（未配置）'}`;
    option.disabled = !provider.configured;
    return option;
  }));
  if (capabilities.default_provider) $('provider').value = capabilities.default_provider;
  updateLanguages();
  renderState('idle');
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    $('record').disabled = true;
    report('当前浏览器环境无法录音。请用 Chrome 或 Edge 打开本机 localhost 地址，或使用 HTTPS。');
  } else if (!capabilities.default_provider) {
    report('服务端还没有配置模型密钥，配置后刷新本页。');
  }
} catch (error) {
  renderState('error');
  report(`${error.message} 请确认后端正在运行后刷新。`);
}
