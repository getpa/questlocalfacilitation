// StreamTile: one headset's media WebSocket -> WebCodecs -> canvas (+ optional Opus audio).
// Message format: questhub/web/media_ws.py (u8 kind | u8 flags | u64 pts | payload).

const KIND = { SESSION: 1, VIDEO_CONFIG: 2, VIDEO: 3, AUDIO_CONFIG: 4, AUDIO: 5 };

export const webCodecsSupported = () => 'VideoDecoder' in window && window.isSecureContext;

export function avcCodecString(annexb) {
  for (let i = 0; i + 4 < annexb.length; i++) {
    if (annexb[i] !== 0 || annexb[i + 1] !== 0) continue;
    let nal = -1;
    if (annexb[i + 2] === 1) nal = i + 3;
    else if (annexb[i + 2] === 0 && annexb[i + 3] === 1) nal = i + 4;
    if (nal < 0) continue;
    if ((annexb[nal] & 0x1f) === 7) {
      const hex = (b) => b.toString(16).padStart(2, '0');
      return `avc1.${hex(annexb[nal + 1])}${hex(annexb[nal + 2])}${hex(annexb[nal + 3])}`;
    }
  }
  return 'avc1.42e01f';
}

export class AudioPlayer {
  constructor() { this.ctx = null; this.clock = 0; this.decoder = null; this.enabled = false; }

  async enable() {
    this.ctx = this.ctx || new AudioContext({ sampleRate: 48000 });
    await this.ctx.resume();
    this.enabled = true;
  }

  async disable() { this.enabled = false; if (this.ctx) await this.ctx.suspend(); }

  configure(opusHead) {
    if (!('AudioDecoder' in window)) return;
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
    this.decoder = new AudioDecoder({ output: (d) => this.play(d), error: () => {} });
    this.decoder.configure({ codec: 'opus', sampleRate: 48000, numberOfChannels: 2, description: opusHead });
  }

  decode(pts, data) {
    if (!this.enabled || !this.decoder || this.decoder.state !== 'configured') return;
    try { this.decoder.decode(new EncodedAudioChunk({ type: 'key', timestamp: pts, data })); } catch { /* skip */ }
  }

  play(audioData) {
    const ctx = this.ctx;
    if (!ctx || ctx.state !== 'running') { audioData.close(); return; }
    const frames = audioData.numberOfFrames;
    const channels = audioData.numberOfChannels;
    const buffer = ctx.createBuffer(channels, frames, audioData.sampleRate);
    for (let ch = 0; ch < channels; ch++) {
      const plane = new Float32Array(frames);
      audioData.copyTo(plane, { planeIndex: ch, format: 'f32-planar' });
      buffer.copyToChannel(plane, ch);
    }
    audioData.close();
    const now = ctx.currentTime;
    if (this.clock < now + 0.02 || this.clock > now + 0.3) this.clock = now + 0.08;  // ~80 ms jitter buffer
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    source.start(this.clock);
    this.clock += buffer.duration;
  }

  close() { if (this.decoder && this.decoder.state !== 'closed') this.decoder.close(); }
}

export class StreamTile {
  /**
   * @param canvas target canvas
   * @param urlFn () => WebSocket URL (re-evaluated on every (re)connect)
   */
  constructor(canvas, urlFn, { maxQueue = 30, audio = null, onClose = null } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.urlFn = urlFn;
    this.maxQueue = maxQueue;
    this.audio = audio;           // AudioPlayer or null
    this.onClose = onClose;
    this.decoder = null;
    this.config = null;
    this.waitingForKey = true;
    this.closed = true;
    this.width = 0; this.height = 0;
    this.resetStats();
  }

  resetStats() {
    this.stats = { codec: this.stats?.codec ?? null, decoded: 0, dropped: 0, errors: 0, lastError: null,
      bytes: 0, since: performance.now() };
  }

  fps() {
    const seconds = Math.max(0.001, (performance.now() - this.stats.since) / 1000);
    return this.stats.decoded / seconds;
  }

  start() {
    this.closed = false;
    this.connect();
  }

  connect() {
    if (this.closed) return;
    this.ws = new WebSocket(this.urlFn());
    this.ws.binaryType = 'arraybuffer';
    this.ws.onmessage = (e) => this.onMessage(e.data);
    this.ws.onclose = (e) => {
      if (this.onClose) this.onClose(e.code);
      if (!this.closed && e.code !== 4429 && e.code !== 4401 && e.code !== 4403) setTimeout(() => this.connect(), 1500);
    };
  }

  reconnect() {
    if (this.ws) { const old = this.ws; old.onclose = null; old.close(); }
    this.waitingForKey = true;
    this.connect();
  }

  stop() {
    this.closed = true;
    if (this.ws) { this.ws.onclose = null; this.ws.close(); }
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
    this.decoder = null;
  }

  fail(where, error) {
    this.stats.errors++;
    this.stats.lastError = `${where}: ${error?.message ?? error}`;
    this.waitingForKey = true;
  }

  draw(frame) {
    const { canvas } = this;
    if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
      canvas.width = frame.displayWidth;
      canvas.height = frame.displayHeight;
    }
    this.ctx.drawImage(frame, 0, 0);
    frame.close();
    this.stats.decoded++;
  }

  configure(config) {
    this.config = config;
    const codec = avcCodecString(config);
    this.stats.codec = codec;
    if (!('VideoDecoder' in window)) { this.fail('WebCodecs', 'not supported'); return; }
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
    this.decoder = new VideoDecoder({ output: (f) => this.draw(f), error: (e) => this.fail('VideoDecoder', e) });
    this.decoder.configure({ codec, optimizeForLatency: true });
    this.waitingForKey = true;
  }

  decode(pts, key, data) {
    const d = this.decoder;
    if (!d || d.state !== 'configured') return;
    if (this.waitingForKey && !key) { this.stats.dropped++; return; }
    if (d.decodeQueueSize > this.maxQueue) { this.stats.dropped++; this.waitingForKey = true; return; }
    let payload = data;
    if (key) {
      payload = new Uint8Array(this.config.length + data.length);
      payload.set(this.config, 0);
      payload.set(data, this.config.length);
      this.waitingForKey = false;
    }
    try {
      d.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: pts, data: payload }));
    } catch (e) { this.fail('decode', e); }
  }

  onMessage(buffer) {
    this.stats.bytes += buffer.byteLength;
    const view = new DataView(buffer);
    const kind = view.getUint8(0);
    const key = (view.getUint8(1) & 1) === 1;
    const pts = Number(view.getBigUint64(2));
    const payload = new Uint8Array(buffer, 10);
    switch (kind) {
      case KIND.SESSION: this.width = view.getUint32(10); this.height = view.getUint32(14); break;
      case KIND.VIDEO_CONFIG: this.configure(payload.slice()); break;
      case KIND.VIDEO: this.decode(pts, key, payload); break;
      case KIND.AUDIO_CONFIG: if (this.audio) this.audio.configure(payload.slice()); break;
      case KIND.AUDIO: if (this.audio) this.audio.decode(pts, payload); break;
    }
  }
}
