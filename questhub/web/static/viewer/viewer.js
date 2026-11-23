// Read-only viewer: key-frame grid, one full-rate focus view (docs §4.6).
import { AudioPlayer, StreamTile, webCodecsSupported } from '/static/common/tile.js';

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
const url = (serial, video, audio = 0) => `${scheme}://${location.host}/ws/media/${encodeURIComponent(serial)}?video=${video}&audio=${audio}`;
const tiles = new Map();
let devices = [];
let focus = null;

$('#unsupported').hidden = webCodecsSupported();
$('#cols').innerHTML += Array.from({ length: 10 }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join('');
$('#cols').onchange = render;

function toast(message) {
  const el = document.createElement('div');
  el.className = 'toast warn';
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), 6000);
}

async function load() {
  const res = await fetch('/api/devices');
  if (res.status === 401) { location.reload(); return; }
  devices = (await res.json()).filter((d) => d.state && !['idle', 'stopped'].includes(d.state));
  devices.sort((a, b) => (a.grid_row ?? 999) - (b.grid_row ?? 999) || (a.grid_col ?? 999) - (b.grid_col ?? 999) || a.alias.localeCompare(b.alias));
  render();
}

function render() {
  const grid = $('#grid');
  const n = devices.length;
  const v = $('#cols').value;
  grid.style.setProperty('--cols', v === 'auto' ? Math.max(1, Math.min(8, Math.ceil(Math.sqrt(n * 1.6)))) : v);
  $('#empty').hidden = n > 0;
  $('#summary').textContent = `${n} 台`;
  const keep = new Set(devices.map((d) => d.serial));
  for (const [serial, t] of tiles) if (!keep.has(serial)) { t.tile.stop(); t.el.remove(); tiles.delete(serial); }
  for (const d of devices) {
    let t = tiles.get(d.serial);
    if (!t) {
      const el = document.createElement('div');
      el.className = 'tile';
      el.innerHTML = '<canvas width="1280" height="684"></canvas><span class="state" hidden></span><span class="flags"></span><div class="label"><span class="name"></span></div>';
      const tile = new StreamTile(el.querySelector('canvas'), () => url(d.serial, 'keyframes'));
      el.onclick = () => openFocus(d.serial);
      t = { el, tile };
      tiles.set(d.serial, t);
      if (webCodecsSupported()) tile.start();
    }
    grid.append(t.el);
    t.el.querySelector('.name').textContent = d.alias;
    t.el.querySelector('.flags').innerHTML = d.recording ? '<span class="rec">REC</span>' : '';
    const st = t.el.querySelector('.state');
    st.hidden = d.state === 'streaming';
    st.textContent = d.state === 'streaming' ? '' : '再接続中';
  }
}

function openFocus(serial) {
  closeFocus();
  const d = devices.find((x) => x.serial === serial);
  const audio = new AudioPlayer();
  const tile = new StreamTile($('#focus-canvas'), () => url(serial, 'full', 1), {
    audio, onClose: (code) => { if (code === 4429) { toast('全フレーム表示の上限に達しています。しばらくしてから開いてください。'); closeFocus(); } },
  });
  focus = { serial, tile, audio };
  $('#focus-title').textContent = d ? d.alias : serial;
  $('#focus-audio').textContent = '音声を再生';
  $('#focus').hidden = false;
  for (const t of tiles.values()) t.tile.stop();   // save bandwidth while focused
  tile.start();
}

function closeFocus() {
  if (!focus) return;
  focus.tile.stop(); focus.audio.disable(); focus.audio.close();
  focus = null;
  $('#focus').hidden = true;
  if (webCodecsSupported()) for (const t of tiles.values()) if (t.tile.closed) t.tile.start();
}

setInterval(() => {
  if (!focus) return;
  const d = devices.find((x) => x.serial === focus.serial) || {};
  $('#focus-info').textContent = `${d.width || '?'}×${d.height || '?'} · ${focus.tile.fps().toFixed(0)}fps · ${d.quality || ''}`;
  focus.tile.resetStats();
}, 1000);

$('#focus-close').onclick = closeFocus;
$('#focus').onclick = (e) => { if (e.target.id === 'focus') closeFocus(); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeFocus(); });
$('#focus-audio').onclick = async () => {
  if (!focus) return;
  if (focus.audio.enabled) { await focus.audio.disable(); $('#focus-audio').textContent = '音声を再生'; }
  else { await focus.audio.enable(); $('#focus-audio').textContent = '音声を停止'; }
};

load();
setInterval(load, 5000);
window.qhViewer = { tiles, devices: () => devices, focus: () => focus };
