// Quest Hub admin GUI. State comes from /api/state (refreshed on events), media from /ws/media.
import { AudioPlayer, StreamTile } from '/static/common/tile.js';

const TOKEN = window.QH_TOKEN;
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const CONN = { wifi: ['Wi-Fi', 'ok'], usb: ['USB', 'ok'], unauthorized: ['許可待ち', 'warn'], connecting: ['接続中', ''],
  offline: ['オフライン', 'bad'], needs_usb: ['USB で再有効化が必要', 'bad'] };
const STREAM = { streaming: ['表示中', 'ok'], starting: ['起動中', ''], waiting: ['接続待ち', 'warn'], backoff: ['再接続待ち', 'warn'],
  reconfiguring: ['品質変更中', ''], paused: ['ネイティブ表示中', ''], stopped: ['停止', ''], idle: ['未開始', ''] };
const chip = (map, key) => { const [t, c] = map[key] || [key || '—', '']; return `<span class="chip ${c}">${esc(t)}</span>`; };
const fmtBytes = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`);
const fmtTime = (s) => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };

let state = { devices: [], usb: [], warnings: [], recording: { active: [] }, settings: {}, presets: [], viewer: {} };

// ---------------------------------------------------------------- API

async function api(method, path, body, raw = false) {
  const opts = { method, headers: { 'X-QH-Token': TOKEN } };
  if (body !== undefined) {
    if (raw) opts.body = body;
    else { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  }
  const res = await fetch(path, opts);
  const data = res.headers.get('content-type')?.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error(data?.detail || res.statusText);
  return data;
}

async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok, 'ok'); refreshSoon(); return r; } catch (e) { toast(e.message, 'bad'); return null; }
}

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 9000 : 5000);
}

// ---------------------------------------------------------------- refresh

let refreshTimer = null;
function refreshSoon(delay = 150) { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, delay); }

async function refresh() {
  try { state = await api('GET', '/api/state'); } catch { return; }
  renderSummary(); renderMonitor(); renderDevices(); renderRecording(); renderNetworkWarnings(); renderSettings();
}

function connectEvents() {
  const ws = new WebSocket(`ws://${location.host}/ws/events?token=${encodeURIComponent(TOKEN)}`);
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    const alias = (state.devices.find((d) => d.serial === ev.serial) || {}).alias || ev.serial || '';
    if (ev.type === 'network_warning') toast(`${alias}: ${ev.data.message}`, 'warn');
    if (ev.type === 'notice') toast(`${alias ? alias + ': ' : ''}${ev.data.message}`, ev.data.level === 'error' ? 'bad' : ev.data.level === 'warning' ? 'warn' : '');
    if (ev.type === 'provisioning_progress') {
      if (ev.data.step === 'done') toast(`${alias}: Wi-Fi 化しました（${ev.data.ip}${ev.data.ssid ? ' / ' + ev.data.ssid : ''}）`, 'ok');
      if (ev.data.step === 'error') toast(`${ev.data.usb_id}: ${ev.data.error}`, 'bad');
    }
    if (ev.type === 'scan_progress') {
      $('#scan-progress').textContent = ev.data.step === 'done' ? `完了: ポート開放 ${ev.data.found} 台、登録 ${ev.data.identified} 台`
        : ev.data.step === 'start' ? `${ev.data.hosts} ホストを確認中…（${ev.data.ranges.join(', ')}）` : `${ev.data.done}/${ev.data.total}（発見 ${ev.data.found}）`;
    }
    if (ev.type === 'recording_stopped' && ev.data.reason === 'disk_full') toast('ディスク不足のため録画を停止しました', 'bad');
    if (ev.type === 'media_black') {
      if (ev.data.cause && !ev.data.meta_paused) {
        const paused = ev.data.diagnostics && ev.data.diagnostics.recording_paused;
        toast(`${alias}: 映像が黒くなっています — ${ev.data.cause}${paused ? '' : '（自動で復旧を試みます）'}`, 'warn');
      }
      else if (ev.data.black === false) toast(`${alias}: 映像が戻りました${ev.data.recovered_by ? `（${({ reset_video: 'キャプチャ再作成', wake: '画面の再起動', restart: 'サーバー再起動' })[ev.data.recovered_by] || ev.data.recovered_by}）` : ''}`, 'ok');
    }
    if (ev.type !== 'recording_progress' || $('#tab-recording').classList.contains('active')) refreshSoon(300);
  };
  ws.onclose = () => setTimeout(connectEvents, 2000);
}

function renderSummary() {
  const d = state.devices.filter((x) => !x.placeholder);
  const conn = d.filter((x) => ['wifi', 'usb'].includes(x.connection.state)).length;
  const streaming = d.filter((x) => x.stream.state === 'streaming').length;
  const rec = state.recording.active.length;
  $('#summary').textContent = `接続 ${conn}/${d.length} · 表示 ${streaming}${rec ? ` · 録画 ${rec}` : ''}${state.viewer.running ? ` · ビューワー公開中（${state.viewer.sessions}人）` : ''}`
    + (state.auto_suspended ? ' · 授業終了済み（「全台表示」で再開）' : '');
  const w = state.warnings.length;
  $('#warn-count').hidden = !w;
  $('#warn-count').textContent = w;
}

// ---------------------------------------------------------------- tabs

$$('#tabs button').forEach((b) => b.onclick = () => {
  $$('#tabs button').forEach((x) => x.classList.toggle('active', x === b));
  $$('.tab').forEach((t) => t.classList.toggle('active', t.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === 'network') loadNetwork();
  if (b.dataset.tab === 'recording') loadSessions();
  if (b.dataset.tab === 'settings') loadViewer();
  updateTileVisibility();
});

function qualityOptions(select, current) {
  select.innerHTML = state.presets.map((p) => `<option value="${p.name}">${esc(p.label)}（約${p.mbps}Mbps/台）</option>`).join('');
  if (current) select.value = current;
}

// ---------------------------------------------------------------- monitor grid

const tiles = new Map();   // serial -> { el, tile, mode, slow }
let gridMode = 'full';
const visible = new Set();
const observer = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const serial = e.target.dataset.serial;
    if (e.isIntersecting) visible.add(serial); else visible.delete(serial);
  }
  updateTileVisibility();
}, { threshold: 0.01 });

function mediaUrl(serial, mode, audio = 0) {
  return `ws://${location.host}/ws/media/${encodeURIComponent(serial)}?video=${mode}&audio=${audio}&token=${encodeURIComponent(TOKEN)}`;
}

function sortedDevices() {
  return [...state.devices].filter((d) => !d.placeholder).sort((a, b) =>
    (a.grid_row ?? 999) - (b.grid_row ?? 999) || (a.grid_col ?? 999) - (b.grid_col ?? 999) || String(a.alias).localeCompare(String(b.alias)));
}

function gridColumns(n) {
  const v = $('#grid-cols').value;
  if (v !== 'auto') return Number(v);
  return Math.max(1, Math.min(10, Math.ceil(Math.sqrt(n * 1.6))));
}

function renderMonitor() {
  const shown = sortedDevices().filter((d) => d.stream.state && !['idle', 'stopped'].includes(d.stream.state));
  $('#monitor-empty').hidden = shown.length > 0;
  const grid = $('#grid');
  grid.style.setProperty('--cols', gridColumns(shown.length));
  const keep = new Set(shown.map((d) => d.serial));
  for (const [serial, t] of tiles) if (!keep.has(serial)) { t.tile.stop(); observer.unobserve(t.el); t.el.remove(); tiles.delete(serial); }
  shown.forEach((d) => {
    let t = tiles.get(d.serial);
    if (!t) t = createTile(d);
    grid.append(t.el); // keeps order
    const warn = state.warnings.some((w) => w.serial === d.serial);
    t.el.classList.toggle('rec', d.recording);
    t.el.querySelector('.name').textContent = d.alias || d.serial;
    t.el.querySelector('.state').innerHTML = d.stream.state === 'streaming' ? (t.slow ? '低fps' : '') : esc((STREAM[d.stream.state] || [d.stream.state])[0]);
    t.el.querySelector('.state').hidden = d.stream.state === 'streaming' && !t.slow;
    t.el.querySelector('.flags').innerHTML = (d.recording ? '<span class="rec">REC</span>' : '') + (warn ? '<span class="warn">!</span>' : '')
      + (d.stream.black ? `<span class="warn" title="${esc(d.stream.black_cause || 'Quest 側で映像が黒くなっています')}">黒画面</span>` : '')
      + (d.battery != null ? `<span>${d.battery}%</span>` : '');
    t.el.querySelector('.meta').textContent = `${d.stream.quality || ''}`;
  });
  updateTileVisibility();
}

function createTile(d) {
  const el = document.createElement('div');
  el.className = 'tile';
  el.dataset.serial = d.serial;
  el.draggable = true;
  el.innerHTML = '<canvas width="1280" height="684"></canvas><span class="state"></span><span class="flags"></span><div class="label"><span class="name"></span><span class="meta muted"></span></div>';
  const t = { el, mode: gridMode, slow: false, lowChecks: 0 };
  t.tile = new StreamTile(el.querySelector('canvas'), () => mediaUrl(d.serial, t.mode));
  el.onclick = () => openFocus(d.serial);
  el.ondragstart = (e) => e.dataTransfer.setData('text/plain', d.serial);
  el.ondragover = (e) => { e.preventDefault(); el.classList.add('dragover'); };
  el.ondragleave = () => el.classList.remove('dragover');
  el.ondrop = (e) => { e.preventDefault(); el.classList.remove('dragover'); swapSeats(e.dataTransfer.getData('text/plain'), d.serial); };
  tiles.set(d.serial, t);
  observer.observe(el);
  return t;
}

function updateTileVisibility() {
  const onMonitor = $('#tab-monitor').classList.contains('active') && document.visibilityState === 'visible' && $('#focus').hidden;
  for (const [serial, t] of tiles) {
    const want = onMonitor && visible.has(serial);
    if (want && t.tile.closed) { t.tile.resetStats(); t.tile.start(); }
    if (!want && !t.tile.closed) t.tile.stop();
  }
}
document.addEventListener('visibilitychange', updateTileVisibility);

// Automatic fall-back to key frames for tiles that cannot keep up (docs §7.2).
setInterval(() => {
  const threshold = state.settings.tile_fallback_fps || 20;
  for (const [serial, t] of tiles) {
    const d = state.devices.find((x) => x.serial === serial);
    if (t.tile.closed || t.mode !== 'full' || !d || d.stream.state !== 'streaming') { t.tile.resetStats(); continue; }
    const expected = Math.min(d.stream.quality === 'low' ? 15 : 30, 30);
    const fps = t.tile.fps();
    t.lowChecks = fps < Math.min(threshold, expected * 0.66) ? t.lowChecks + 1 : 0;
    t.tile.resetStats();
    if (t.lowChecks >= 3) { t.slow = true; t.mode = 'keyframes'; t.tile.reconnect(); toast(`${d.alias}: 表示が追いつかないため低fpsに切り替えました`, 'warn'); }
  }
}, 2000);

async function swapSeats(from, to) {
  if (!from || from === to) return;
  const order = sortedDevices().filter((d) => tiles.has(d.serial)).map((d) => d.serial);
  const i = order.indexOf(from), j = order.indexOf(to);
  if (i < 0 || j < 0) return;
  [order[i], order[j]] = [order[j], order[i]];
  const cols = gridColumns(order.length);
  await act(() => Promise.all(order.map((serial, k) => api('PATCH', `/api/devices/${encodeURIComponent(serial)}`, { grid_col: k % cols, grid_row: Math.floor(k / cols) }))));
}

$('#grid-mode').onchange = (e) => { gridMode = e.target.value; for (const t of tiles.values()) { t.mode = gridMode; t.slow = false; t.tile.reconnect(); } };
$('#grid-cols').innerHTML += Array.from({ length: 10 }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join('');
$('#grid-cols').onchange = () => renderMonitor();
$('#start-all').onclick = () => act(() => api('POST', '/api/streams/start-all'), '全台の表示を開始しました');
$('#stop-all').onclick = () => act(() => api('POST', '/api/streams/stop-all'));
$('#quality-all').onchange = (e) => act(() => api('POST', '/api/streams/quality-all', { preset: e.target.value }), '品質を変更しました（録画中の端末は除く）');
$$('[data-broadcast]').forEach((b) => b.onclick = () => act(() => api('POST', '/api/input/broadcast', { key: b.dataset.broadcast }), `${b.dataset.broadcast} を送信しました`));

// ---------------------------------------------------------------- focus view

let focus = null;  // { serial, tile, audio, control }

function openFocus(serial) {
  closeFocus();
  const d = state.devices.find((x) => x.serial === serial);
  if (!d) return;
  const audio = new AudioPlayer();
  const tile = new StreamTile($('#focus-canvas'), () => mediaUrl(serial, 'full', 1), { audio });
  focus = { serial, tile, audio, control: null };
  $('#focus-title').textContent = d.alias || serial;
  qualityOptions($('#focus-quality'), d.stream.quality);
  $('#focus-audio').textContent = '音声を再生';
  $('#focus-input').checked = false;
  $('#focus-canvas').classList.remove('input');
  $('#focus').hidden = false;
  updateTileVisibility();
  tile.start();
  updateFocusInfo();
}

function closeFocus() {
  if (!focus) return;
  focus.tile.stop(); focus.audio.disable(); focus.audio.close();
  if (focus.control) focus.control.close();
  focus = null;
  $('#focus').hidden = true;
  updateTileVisibility();
}

function updateFocusInfo() {
  if (!focus) return;
  const d = state.devices.find((x) => x.serial === focus.serial) || {};
  const s = d.stream || {};
  $('#focus-info').textContent = `${(STREAM[s.state] || [s.state])[0] || ''} · ${s.width || '?'}×${s.height || '?'} · ${focus.tile.fps().toFixed(0)}fps · ${d.ssid || ''}`;
  focus.tile.resetStats();
}
setInterval(updateFocusInfo, 1000);

$('#focus-close').onclick = closeFocus;
$('#focus').onclick = (e) => { if (e.target.id === 'focus') closeFocus(); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeFocus(); });
$('#focus-audio').onclick = async () => {
  if (!focus) return;
  if (focus.audio.enabled) { await focus.audio.disable(); $('#focus-audio').textContent = '音声を再生'; }
  else { await focus.audio.enable(); $('#focus-audio').textContent = '音声を停止'; }
};
$('#focus-quality').onchange = (e) => focus && act(() => api('POST', `/api/streams/${encodeURIComponent(focus.serial)}/quality`, { preset: e.target.value }), '品質を変更しました');
$('#focus-key').onclick = () => focus && act(() => api('POST', `/api/streams/${encodeURIComponent(focus.serial)}/keyframe`));
$('#focus-native').onclick = () => focus && act(() => api('POST', `/api/streams/${encodeURIComponent(focus.serial)}/native`), 'ネイティブ表示を開きました（閉じると自動で再開）');
$('#focus-diag').onclick = async () => {
  if (!focus) return;
  const r = await act(() => api('POST', `/api/streams/${encodeURIComponent(focus.serial)}/diagnose`));
  if (r) toast(`診断: ${r.cause}（記録: ${r.file || 'なし'}）`, 'warn');
};
$$('[data-key]').forEach((b) => b.onclick = () => focus && act(() => api('POST', `/api/input/${encodeURIComponent(focus.serial)}/key`, { key: b.dataset.key })));
$('#focus-text-form').onsubmit = (e) => {
  e.preventDefault();
  const text = $('#focus-text').value;
  if (focus && text) act(() => api('POST', `/api/input/${encodeURIComponent(focus.serial)}/text`, { text })).then(() => { $('#focus-text').value = ''; });
};
$('#focus-input').onchange = (e) => {
  if (!focus) return;
  $('#focus-canvas').classList.toggle('input', e.target.checked);
  if (e.target.checked) {
    focus.control = new WebSocket(`ws://${location.host}/ws/control/${encodeURIComponent(focus.serial)}?token=${encodeURIComponent(TOKEN)}`);
    focus.control.onmessage = (m) => { const r = JSON.parse(m.data); if (r.error) toast(r.error, 'bad'); };
  } else if (focus.control) { focus.control.close(); focus.control = null; }
};
let pointerDown = false;
function sendPointer(action, e) {
  if (!focus?.control || focus.control.readyState !== 1) return;
  const r = e.target.getBoundingClientRect();
  const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  focus.control.send(JSON.stringify({ action, x, y }));
}
$('#focus-canvas').onpointerdown = (e) => { if (!$('#focus-input').checked) return; pointerDown = true; e.target.setPointerCapture(e.pointerId); sendPointer('down', e); };
$('#focus-canvas').onpointermove = (e) => { if (pointerDown) sendPointer('move', e); };
$('#focus-canvas').onpointerup = (e) => { if (pointerDown) { pointerDown = false; sendPointer('up', e); } };

// ---------------------------------------------------------------- devices tab

function renderDevices() {
  const usb = state.usb;
  $('#usb-list').innerHTML = usb.length ? usb.map((u) => `<label class="row"><input type="checkbox" class="usb-pick" value="${esc(u.usb_id)}" ${u.state === 'device' ? '' : 'disabled'}>
    <span>${esc(u.model || '')} ${esc(u.serial)}</span> ${u.state === 'device' ? (u.wifi ? '<span class="chip ok">Wi-Fi 済み</span>' : '<span class="chip">USB</span>')
    : '<span class="chip warn">ヘッドセット内で USB デバッグを許可してください</span>'}</label>`).join('') : '<span class="muted">USB 接続の端末はありません</span>';

  const tbody = $('#device-table tbody');
  const active = document.activeElement;
  if (active && tbody.contains(active) && active.tagName === 'INPUT') return; // do not clobber typing
  tbody.innerHTML = sortedDevicesAll().map((d) => {
    const c = d.connection || {};
    const s = d.stream || {};
    const wifi = d.ssid ? `${esc(d.ssid)} <span class="muted">${esc(d.band || '')} ${d.rssi ?? ''}dBm</span>` : '<span class="muted">—</span>';
    const streaming = s.state && !['idle', 'stopped'].includes(s.state);
    return `<tr data-serial="${esc(d.serial)}">
      <td><input class="inline" data-field="alias" value="${esc(d.alias || '')}"><div class="muted hint">${esc(d.placeholder ? 'TSV（未接続）' : d.serial)} ${esc(d.model || '')}</div></td>
      <td>${chip(CONN, c.state)}${c.last_error && c.state !== 'wifi' ? `<div class="hint muted" title="${esc(c.last_error)}">${esc(c.last_error.slice(0, 40))}</div>` : ''}</td>
      <td><input class="inline" data-field="ip" value="${esc(d.ip || '')}" placeholder="—">${d.ip_locked ? ' <span class="chip" title="手入力（スキャンで上書きしない）">固定</span>' : ''}</td>
      <td>${wifi}</td>
      <td>${d.battery != null ? `${d.battery}%${d.charging ? ' ⚡' : ''}` : '—'}</td>
      <td><input class="num" data-field="grid_col" type="number" min="0" value="${d.grid_col ?? ''}"> <input class="num" data-field="grid_row" type="number" min="0" value="${d.grid_row ?? ''}"></td>
      <td><select data-field="quality">${state.presets.map((p) => `<option value="${p.name}" ${(s.quality || d.quality || state.settings.default_quality) === p.name ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}</select></td>
      <td>${chip(STREAM, s.state || 'idle')}${d.recording ? ' <span class="chip bad">REC</span>' : ''}${s.black ? ` <span class="chip warn" title="${esc(s.black_cause || '')}">黒画面</span>` : ''}</td>
      <td class="row">
        ${['wifi', 'usb'].includes(c.state) ? '<button data-act="disconnect">切断</button>' : '<button data-act="connect">接続</button>'}
        ${streaming ? '<button data-act="stop">表示停止</button>' : '<button data-act="start">表示</button>'}
        <button data-act="delete" class="danger">削除</button>
      </td></tr>`;
  }).join('') || '<tr><td colspan="9" class="empty">端末がありません。USB で接続するか、スキャン / TSV 読み込みで登録してください。</td></tr>';
}

function sortedDevicesAll() {
  return [...state.devices].sort((a, b) => (a.grid_row ?? 999) - (b.grid_row ?? 999) || (a.grid_col ?? 999) - (b.grid_col ?? 999)
    || String(a.alias).localeCompare(String(b.alias)));
}

$('#device-table').addEventListener('change', (e) => {
  const tr = e.target.closest('tr[data-serial]');
  const field = e.target.dataset.field;
  if (!tr || !field) return;
  let value = e.target.value;
  if (field === 'grid_col' || field === 'grid_row') value = value === '' ? null : Number(value);
  act(() => api('PATCH', `/api/devices/${encodeURIComponent(tr.dataset.serial)}`, { [field]: value }));
});
$('#device-table').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-act]');
  const tr = e.target.closest('tr[data-serial]');
  if (!b || !tr) return;
  const serial = encodeURIComponent(tr.dataset.serial);
  const a = b.dataset.act;
  if (a === 'connect') act(() => api('POST', `/api/devices/${serial}/connect`));
  if (a === 'disconnect') act(() => api('POST', `/api/devices/${serial}/disconnect`));
  if (a === 'start') act(() => api('POST', `/api/streams/${serial}/start`));
  if (a === 'stop') act(() => api('POST', `/api/streams/${serial}/stop`));
  if (a === 'delete' && confirm('この端末を台帳から削除しますか？')) act(() => api('DELETE', `/api/devices/${serial}`));
});
$('#enable-wifi-selected').onclick = () => {
  const ids = $$('.usb-pick:checked').map((x) => x.value);
  if (!ids.length) { toast('端末を選んでください', 'warn'); return; }
  toast('Wi-Fi 化しています…');
  act(() => api('POST', '/api/usb/enable-wifi', { usb_ids: ids }));
};
$('#enable-wifi-all').onclick = () => { toast('Wi-Fi 化しています…'); act(() => api('POST', '/api/usb/enable-wifi', {})); };
$('#scan').onclick = () => { $('#scan-progress').textContent = '開始…'; act(() => api('POST', '/api/scan', { cidr: $('#scan-cidr').value.trim() || null })); };
$('#tsv-file').onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const text = await file.text();
  const r = await act(() => api('POST', '/api/registry/import', text, true));
  if (r) toast(`${r.imported} 台を読み込みました`, 'ok');
  e.target.value = '';
};

// ---------------------------------------------------------------- recording tab

function renderRecording() {
  const rec = state.recording;
  const streaming = state.devices.filter((d) => d.stream.state === 'streaming');
  const checked = new Set($$('.rec-pick:checked').map((x) => x.value));
  $('#rec-select').innerHTML = streaming.map((d) => `<label><input type="checkbox" class="rec-pick" value="${esc(d.serial)}" ${checked.has(d.serial) ? 'checked' : ''} ${d.recording ? 'disabled' : ''}> ${esc(d.alias)}</label>`).join('') || '<span class="muted">表示中の端末がありません</span>';
  $('#rec-disk').textContent = `保存先 ${rec.dir || ''} · 空き ${rec.free_gb ?? '?'} GB${rec.session ? ` · セッション ${rec.session}` : ''}`;
  $('#rec-active tbody').innerHTML = rec.active.map((r) => {
    const d = state.devices.find((x) => x.serial === r.serial) || {};
    return `<tr><td>${esc(d.alias || r.serial)}</td><td>${fmtTime(r.elapsed_s)}</td><td>${fmtBytes(r.bytes)}</td><td>${r.parts}</td>
      <td class="muted">${esc(r.file.split('/').slice(-2).join('/'))}</td><td><button data-stop="${esc(r.serial)}">停止</button></td></tr>`;
  }).join('') || '<tr><td colspan="6" class="empty">録画していません</td></tr>';
}
$('#rec-active').onclick = (e) => { const s = e.target.dataset.stop; if (s) act(() => api('POST', '/api/recordings/stop', { serials: [s] })); };
$('#rec-start-all').onclick = () => act(() => api('POST', '/api/recordings/start', { name: $('#rec-name').value.trim() || null }), '録画を開始しました');
$('#rec-start-selected').onclick = () => {
  const serials = $$('.rec-pick:checked').map((x) => x.value);
  if (!serials.length) { toast('端末を選んでください', 'warn'); return; }
  act(() => api('POST', '/api/recordings/start', { serials, name: $('#rec-name').value.trim() || null }), '録画を開始しました');
};
$('#rec-stop-all').onclick = () => act(() => api('POST', '/api/recordings/stop', {}), '録画を停止しました');

async function loadSessions() {
  const r = await api('GET', '/api/recordings').catch(() => null);
  if (!r) return;
  $('#rec-sessions tbody').innerHTML = r.sessions.map((s) => `<tr><td>${esc(s.name)}</td><td>${s.started_at ? new Date(s.started_at * 1000).toLocaleString() : ''}</td>
    <td>${s.devices}</td><td>${fmtBytes(s.bytes || 0)}</td><td class="muted">${esc(s.dir)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">まだありません</td></tr>';
}

// ---------------------------------------------------------------- network tab

function countTable(obj) {
  const rows = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
  return rows.length ? `<table>${rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v} 台</td></tr>`).join('')}</table>` : '<span class="muted">データなし</span>';
}

function renderNetworkWarnings() {
  const name = (s) => (state.devices.find((d) => d.serial === s) || {}).alias || s;
  $('#warnings').innerHTML = state.warnings.length ? state.warnings.map((w) => `<div class="row"><span class="chip warn">${esc(name(w.serial))}</span>
    <span>${esc(w.message)}</span><span class="muted hint">${new Date(w.created_at * 1000).toLocaleTimeString()}</span>
    <button data-ack="${w.id}">確認</button></div>`).join('') : '<span class="muted">なし</span>';
}
$('#warnings').onclick = (e) => { const id = e.target.dataset.ack; if (id) act(() => api('POST', `/api/warnings/${id}/ack`)); };

async function loadNetwork() {
  const n = await api('GET', '/api/network').catch(() => null);
  if (!n) return;
  $('#net-ssid').innerHTML = countTable(n.ssids);
  $('#net-bssid').innerHTML = countTable(n.bssids);
  $('#net-band').innerHTML = countTable(n.bands);
  const name = (s) => (state.devices.find((d) => d.serial === s) || {}).alias || s;
  $('#net-notes').innerHTML = [
    n.weak.length ? `電波が弱い（-70dBm 未満）: ${n.weak.map(name).map(esc).join(', ')}` : '',
    n.slow_band.length ? `2.4GHz 接続: ${n.slow_band.map(name).map(esc).join(', ')}` : '',
  ].filter(Boolean).join('<br>');
}

// ---------------------------------------------------------------- settings / viewer

function renderSettings() {
  qualityOptions($('#quality-all'), state.settings.default_quality);
  const v = state.viewer;
  $('#viewer-enabled').checked = v.enabled;
  $('#viewer-status').textContent = v.running ? `公開中 · 視聴 ${v.sessions} 人${v.insecure ? ' · HTTP（開発用）' : ''}` : v.error ? `起動できません: ${v.error}` : '停止中';
  $('#viewer-detail').hidden = !v.enabled;
  $('#viewer-code').textContent = v.code;
  const form = $('#settings-form');
  if (!form.contains(document.activeElement)) {
    qualityOptions(form.default_quality, state.settings.default_quality);
    for (const el of form.elements) {
      if (!el.name || !(el.name in state.settings)) continue;
      if (el.type === 'checkbox') el.checked = !!state.settings[el.name]; else el.value = state.settings[el.name];
    }
  }
}

async function loadViewer() {
  const v = await api('GET', '/api/viewer').catch(() => null);
  if (!v) return;
  $('#viewer-urls').innerHTML = v.urls.map((u) => `<code>${esc(u)}</code>`).join(' / ');
  $('#ca-url').innerHTML = `<code>${esc(v.ca_url)}</code>`;
  $('#ca-fp').textContent = v.fingerprint || '（ビューワー初回起動時に作成）';
  $('#qr-viewer').innerHTML = v.qr_viewer;
  $('#qr-ca').innerHTML = v.qr_ca;
}

$('#viewer-enabled').onchange = (e) => act(() => api('POST', `/api/viewer/${e.target.checked ? 'enable' : 'disable'}`)).then(loadViewer);
$('#viewer-rotate').onclick = () => act(() => api('POST', '/api/viewer/rotate-code'), 'アクセスコードを変更しました');
$('#settings-form').onsubmit = (e) => {
  e.preventDefault();
  const body = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value;
  }
  act(() => api('PUT', '/api/settings', body), '設定を保存しました');
};
$('#end-session').onclick = () => {
  const usb = $('#end-usb').checked;
  if (!confirm(`授業を終了します。録画停止・ビューワー停止・表示停止${usb ? '・全端末の Wi-Fi ADB 無効化' : ''}を行います。よろしいですか？`)) return;
  act(() => api('POST', '/api/session/end', { back_to_usb: usb }), '授業を終了しました');
};

connectEvents();
refresh();
setInterval(refresh, 3000);
