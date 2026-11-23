// StreamTile: one device's media WebSocket -> WebCodecs -> canvas.
// Reusable building block for grid views (M1 load test, M3 grid).
// Message format: see questhub/poc/app.py (u8 kind | u8 flags | u64 pts | payload).

const KIND = { SESSION: 1, VIDEO_CONFIG: 2, VIDEO: 3, AUDIO_CONFIG: 4, AUDIO: 5 };

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

export class StreamTile {
  constructor(canvas, url, { maxQueue = 30 } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.url = url;
    this.maxQueue = maxQueue;
    this.decoder = null;
    this.config = null;
    this.waitingForKey = true;
    this.submitted = new Map();  // timestamp -> performance.now() at decode()
    this.closed = false;
    this.resetStats();
  }

  resetStats() {
    this.stats = {
      codec: this.stats?.codec ?? null, messages: 0, bytes: 0, chunks: 0, keyChunks: 0,
      decoded: 0, dropped: 0, errors: 0, lastError: null, maxQueue: 0,
      latencySum: 0, latencyMax: 0, since: performance.now(),
    };
  }

  summary() {
    const s = this.stats;
    const seconds = Math.max(0.001, (performance.now() - s.since) / 1000);
    return {
      codec: s.codec, fps: s.decoded / seconds, kbps: (s.bytes * 8) / 1000 / seconds,
      decoded: s.decoded, dropped: s.dropped, errors: s.errors, lastError: s.lastError,
      maxQueue: s.maxQueue, latencyAvgMs: s.decoded ? s.latencySum / s.decoded : null,
      latencyMaxMs: s.latencyMax,
    };
  }

  start() {
    this.ws = new WebSocket(this.url);
    this.ws.binaryType = 'arraybuffer';
    this.ws.onmessage = (e) => this.onMessage(e.data);
    this.ws.onclose = () => { if (!this.closed) setTimeout(() => this.start(), 1000); };
  }

  stop() {
    this.closed = true;
    if (this.ws) this.ws.close();
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
  }

  fail(where, error) {
    this.stats.errors++;
    this.stats.lastError = `${where}: ${error?.message ?? error}`;
    this.waitingForKey = true;
  }

  draw(frame) {
    const started = this.submitted.get(frame.timestamp);
    if (started !== undefined) {
      const latency = performance.now() - started;
      this.stats.latencySum += latency;
      this.stats.latencyMax = Math.max(this.stats.latencyMax, latency);
      this.submitted.delete(frame.timestamp);
    }
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
    if (this.decoder && this.decoder.state !== 'closed') this.decoder.close();
    this.decoder = new VideoDecoder({ output: (f) => this.draw(f), error: (e) => this.fail('VideoDecoder', e) });
    this.decoder.configure({ codec, optimizeForLatency: true });
    this.waitingForKey = true;
    this.submitted.clear();
  }

  decode(pts, key, data) {
    const s = this.stats;
    s.chunks++;
    if (key) s.keyChunks++;
    const d = this.decoder;
    if (!d || d.state !== 'configured') return;
    s.maxQueue = Math.max(s.maxQueue, d.decodeQueueSize);
    if (this.waitingForKey && !key) { s.dropped++; return; }
    if (d.decodeQueueSize > this.maxQueue) { s.dropped++; this.waitingForKey = true; return; }
    let payload = data;
    if (key) {
      payload = new Uint8Array(this.config.length + data.length);
      payload.set(this.config, 0);
      payload.set(data, this.config.length);
      this.waitingForKey = false;
    }
    try {
      this.submitted.set(pts, performance.now());
      if (this.submitted.size > 300) this.submitted.clear();
      d.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: pts, data: payload }));
    } catch (e) { this.fail('decode', e); }
  }

  onMessage(buffer) {
    this.stats.messages++;
    this.stats.bytes += buffer.byteLength;
    const view = new DataView(buffer);
    const kind = view.getUint8(0);
    const key = (view.getUint8(1) & 1) === 1;
    const pts = Number(view.getBigUint64(2));
    const payload = new Uint8Array(buffer, 10);
    if (kind === KIND.VIDEO_CONFIG) this.configure(payload.slice());
    else if (kind === KIND.VIDEO) this.decode(pts, key, payload);
  }
}
