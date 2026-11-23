// Quest Hub PoC player: WebSocket -> WebCodecs (H.264 Annex B + Opus) -> canvas / Web Audio.
// window.qhStats is read by tests and the status panel.

const KIND = { SESSION: 1, VIDEO_CONFIG: 2, VIDEO: 3, AUDIO_CONFIG: 4, AUDIO: 5 };

const stats = (window.qhStats = {
  supported: 'VideoDecoder' in window,
  secureContext: window.isSecureContext,
  codec: null, width: 0, height: 0,
  messages: 0, videoChunks: 0, keyChunks: 0, framesDecoded: 0, framesDropped: 0,
  audioChunks: 0, audioDecoded: 0, audioPlaying: false,
  decoderErrors: 0, lastError: null, fps: 0, mode: 'full',
});

const canvas = document.getElementById('screen');
const ctx = canvas.getContext('2d');
let videoConfig = null;
let decoder = null;
let waitingForKey = true;
let audioDecoder = null;
let audioCtx = null;
let audioClock = 0;
let ws = null;

// avc1.PPCCLL from the SPS NAL unit (Annex B).
export function avcCodecString(annexb) {
  for (let i = 0; i + 4 < annexb.length; i++) {
    const startCode = annexb[i] === 0 && annexb[i + 1] === 0 &&
      (annexb[i + 2] === 1 || (annexb[i + 2] === 0 && annexb[i + 3] === 1));
    if (!startCode) continue;
    const nal = i + (annexb[i + 2] === 1 ? 3 : 4);
    if ((annexb[nal] & 0x1f) === 7) {
      const hex = (b) => b.toString(16).padStart(2, '0');
      return `avc1.${hex(annexb[nal + 1])}${hex(annexb[nal + 2])}${hex(annexb[nal + 3])}`;
    }
  }
  return 'avc1.42e01f';
}

function fail(where, error) {
  stats.decoderErrors++;
  stats.lastError = `${where}: ${error && error.message ? error.message : error}`;
  console.warn(stats.lastError);
}

function draw(frame) {
  if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
    canvas.width = frame.displayWidth;
    canvas.height = frame.displayHeight;
    canvas.style.aspectRatio = `${frame.displayWidth} / ${frame.displayHeight}`;
  }
  ctx.drawImage(frame, 0, 0);
  frame.close();
  stats.framesDecoded++;
}

async function configureVideo(config) {
  videoConfig = config;
  const codec = avcCodecString(config);
  stats.codec = codec;
  if (!stats.supported) return;
  if (decoder && decoder.state !== 'closed') decoder.close();
  const options = { codec, optimizeForLatency: true, hardwareAcceleration: 'no-preference' };
  try {
    const support = await VideoDecoder.isConfigSupported(options);
    if (!support.supported) throw new Error(`${codec} not supported`);
  } catch (e) { fail('isConfigSupported', e); }
  decoder = new VideoDecoder({ output: draw, error: (e) => { fail('VideoDecoder', e); waitingForKey = true; } });
  decoder.configure(options);  // no description => Annex B, SPS/PPS in-band
  waitingForKey = true;
}

function decodeVideo(pts, key, data) {
  stats.videoChunks++;
  if (key) stats.keyChunks++;
  if (!decoder || decoder.state !== 'configured') return;
  if (waitingForKey && !key) { stats.framesDropped++; return; }
  if (decoder.decodeQueueSize > 30) {  // fall behind: skip to the next key frame
    stats.framesDropped++;
    waitingForKey = true;
    return;
  }
  let payload = data;
  if (key) {  // prepend SPS/PPS so every key frame is self-contained
    payload = new Uint8Array(videoConfig.length + data.length);
    payload.set(videoConfig, 0);
    payload.set(data, videoConfig.length);
    waitingForKey = false;
  }
  try {
    decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: pts, data: payload }));
  } catch (e) { fail('decode', e); waitingForKey = true; }
}

function playAudio(audioData) {
  stats.audioDecoded++;
  if (!audioCtx || audioCtx.state !== 'running') { audioData.close(); return; }
  const frames = audioData.numberOfFrames;
  const channels = audioData.numberOfChannels;
  const buffer = audioCtx.createBuffer(channels, frames, audioData.sampleRate);
  for (let ch = 0; ch < channels; ch++) {
    const plane = new Float32Array(frames);
    audioData.copyTo(plane, { planeIndex: ch, format: 'f32-planar' });
    buffer.copyToChannel(plane, ch);
  }
  audioData.close();
  const now = audioCtx.currentTime;
  if (audioClock < now + 0.02 || audioClock > now + 0.3) audioClock = now + 0.08;  // ~80 ms jitter buffer
  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioCtx.destination);
  source.start(audioClock);
  audioClock += buffer.duration;
}

function configureAudio(opusHead) {
  if (!('AudioDecoder' in window)) return;
  if (audioDecoder && audioDecoder.state !== 'closed') audioDecoder.close();
  audioDecoder = new AudioDecoder({ output: playAudio, error: (e) => fail('AudioDecoder', e) });
  audioDecoder.configure({ codec: 'opus', sampleRate: 48000, numberOfChannels: 2, description: opusHead });
}

function decodeAudio(pts, data) {
  stats.audioChunks++;
  if (!audioDecoder || audioDecoder.state !== 'configured') return;
  try {
    audioDecoder.decode(new EncodedAudioChunk({ type: 'key', timestamp: pts, data }));
  } catch (e) { fail('audio decode', e); }
}

function onMessage(buffer) {
  stats.messages++;
  const view = new DataView(buffer);
  const kind = view.getUint8(0);
  const key = (view.getUint8(1) & 1) === 1;
  const pts = Number(view.getBigUint64(2));
  const payload = new Uint8Array(buffer, 10);
  switch (kind) {
    case KIND.SESSION:
      stats.width = view.getUint32(10);
      stats.height = view.getUint32(14);
      break;
    case KIND.VIDEO_CONFIG: configureVideo(payload.slice()); break;
    case KIND.VIDEO: decodeVideo(pts, key, payload); break;
    case KIND.AUDIO_CONFIG: configureAudio(payload.slice()); break;
    case KIND.AUDIO: decodeAudio(pts, payload); break;
  }
}

function connect() {
  const params = new URLSearchParams({ video: stats.mode, every: '1', audio: '1' });
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${scheme}://${location.host}/ws/media?${params}`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => onMessage(e.data);
  ws.onclose = () => { if (!ws.replaced) setTimeout(connect, 1000); };
}

function reconnect() {
  if (ws) { ws.replaced = true; ws.close(); }
  waitingForKey = true;
  connect();
}

// ---------------------------------------------------------------- UI

const $ = (id) => document.getElementById(id);
function fill(id, rows) {
  $(id).innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v ?? '—'}</dd>`).join('');
}

let lastFrames = 0;
setInterval(() => { stats.fps = stats.framesDecoded - lastFrames; lastFrames = stats.framesDecoded; }, 1000);

async function refresh() {
  let s = null;
  try { s = await (await fetch('/api/status')).json(); } catch { /* server restarting */ }
  fill('client', [
    ['WebCodecs', stats.supported ? '対応' : '非対応'],
    ['codec', stats.codec], ['表示fps', stats.fps], ['デコード', stats.framesDecoded],
    ['破棄', stats.framesDropped], ['音声', `${stats.audioDecoded}${stats.audioPlaying ? '（再生中）' : ''}`],
    ['エラー', stats.lastError],
  ]);
  if (!s) return;
  $('title').textContent = `Quest Hub PoC — ${s.device_name || s.serial}`;
  $('state').textContent = `${s.state}${s.error ? ` (${s.error})` : ''} · 再接続 ${s.restarts}`;
  fill('device', [
    ['serial', s.serial], ['映像', `${s.video.codec ?? ''} ${s.video.width ?? ''}×${s.video.height ?? ''}`],
    ['パケット', s.video.packets], ['キーフレーム', s.video.key_frames],
    ['音声', `${s.audio.codec ?? 'なし'} / ${s.audio.packets}`], ['視聴者', s.viewers],
    ['品質', s.quality], ['実測帯域', s.mbps_in != null ? `${s.mbps_in} Mbps` : null],
  ]);
  if ($('quality').value !== s.quality && document.activeElement !== $('quality')) $('quality').value = s.quality;
  const w = s.wifi || {};
  fill('wifi', [['SSID', w.ssid], ['BSSID', w.bssid], ['帯域', w.band], ['RSSI', w.rssi_dbm != null ? `${w.rssi_dbm} dBm` : null], ['リンク', w.link_mbps != null ? `${w.link_mbps} Mbps` : null]]);
  const r = s.recording || s.last_recording;
  fill('recording', r ? [['状態', s.recording ? '録画中' : '停止'], ['ファイル', r.path], ['映像', r.video_packets], ['音声', r.audio_packets], ['サイズ', r.bytes ? `${(r.bytes / 1e6).toFixed(1)} MB` : null], ['エラー', r.error]] : [['状態', '未録画']]);
  $('rec').textContent = s.recording ? '録画停止' : '録画開始';
  $('stage').classList.toggle('recording', !!s.recording);
}

$('rec').onclick = async () => {
  const s = await (await fetch('/api/status')).json();
  await fetch(s.recording ? '/api/recording/stop' : '/api/recording/start', { method: 'POST' });
  refresh();
};
$('key').onclick = () => fetch('/api/keyframe', { method: 'POST' });
$('audio').onclick = async () => {
  audioCtx = audioCtx || new AudioContext({ sampleRate: 48000 });
  if (audioCtx.state === 'running') { await audioCtx.suspend(); stats.audioPlaying = false; $('audio').textContent = '音声を再生'; }
  else { await audioCtx.resume(); stats.audioPlaying = true; $('audio').textContent = '音声を停止'; }
};
$('mode').onchange = (e) => { stats.mode = e.target.value; reconnect(); };

async function loadQuality() {
  const q = await (await fetch('/api/quality')).json();
  $('quality').innerHTML = q.presets.map((p) =>
    `<option value="${p.name}">${p.label}（${p.max_size}px/${p.max_fps}fps, 約${p.mbps} Mbps/台・50台 ${p.mbps_50} Mbps）</option>`).join('')
    + (q.current === 'custom' ? '<option value="custom">カスタム</option>' : '');
  $('quality').value = q.current;
}
$('quality').onchange = async (e) => {
  const res = await fetch('/api/quality', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preset: e.target.value }) });
  if (!res.ok) { alert((await res.json()).detail); loadQuality(); }
};
loadQuality();

connect();
refresh();
setInterval(refresh, 1000);
