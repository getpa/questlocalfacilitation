"""FrameHub: cache stream state for one device and fan packets out.

The hub keeps the latest session (resolution), codec config and key frame
so a late subscriber can start at the next key frame. Viewer-specific
policies (key-frames-only, every N) live in the web layer.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time

from questhub.stream.protocol import MediaPacket, SessionPacket

# Event kinds delivered to subscribers.
SESSION = "session"
VIDEO_CONFIG = "video_config"
VIDEO = "video"
AUDIO_CONFIG = "audio_config"
AUDIO = "audio"


@dataclass
class Event:
    kind: str
    packet: MediaPacket | None = None
    session: SessionPacket | None = None


@dataclass
class TrackStats:
    packets: int = 0
    key_frames: int = 0
    bytes: int = 0
    last_at: float | None = None


class Subscription:
    """Bounded queue. A slow video subscriber is resynchronised at the next
    key frame instead of buffering without limit."""

    def __init__(self, hub: "FrameHub", video: bool, audio: bool, maxsize: int):
        self._hub = hub
        self.video = video
        self.audio = audio
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize)
        self.need_key_frame = True
        self.dropped = 0
        self.closed = False

    def _offer(self, event: Event) -> None:
        if self.closed:
            return
        if event.kind == VIDEO:
            if self.need_key_frame:
                if not event.packet.key_frame:
                    self.dropped += 1
                    return
                self.need_key_frame = False
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            if event.kind in (VIDEO, VIDEO_CONFIG, SESSION):
                # Drop everything queued and restart from the next key frame,
                # keeping the stream decodable.
                while not self.queue.empty():
                    self.queue.get_nowait()
                self.need_key_frame = True
                self._hub._replay_state(self, include_audio=self.audio)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._hub._subscriptions.discard(self)
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                self.queue.get_nowait()
                self.queue.put_nowait(None)


@dataclass
class FrameHub:
    video_codec: str | None = None
    audio_codec: str | None = None
    session: SessionPacket | None = None
    video_config: MediaPacket | None = None
    audio_config: MediaPacket | None = None
    last_key_frame: MediaPacket | None = None
    video_stats: TrackStats = field(default_factory=TrackStats)
    audio_stats: TrackStats = field(default_factory=TrackStats)
    _subscriptions: set = field(default_factory=set)

    def subscribe(self, video: bool = True, audio: bool = True, maxsize: int = 256) -> Subscription:
        sub = Subscription(self, video, audio, maxsize)
        self._subscriptions.add(sub)
        self._replay_state(sub, include_audio=audio)
        return sub

    def _replay_state(self, sub: Subscription, include_audio: bool) -> None:
        if sub.video:
            if self.session:
                sub.queue.put_nowait(Event(SESSION, session=self.session))
            if self.video_config:
                sub.queue.put_nowait(Event(VIDEO_CONFIG, packet=self.video_config))
        if include_audio and self.audio_config:
            sub.queue.put_nowait(Event(AUDIO_CONFIG, packet=self.audio_config))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def publish_video(self, item: SessionPacket | MediaPacket) -> None:
        if isinstance(item, SessionPacket):
            self.session = item
            event = Event(SESSION, session=item)
        elif item.config:
            self.video_config = item
            event = Event(VIDEO_CONFIG, packet=item)
        else:
            stats = self.video_stats
            stats.packets += 1
            stats.bytes += len(item.data)
            stats.last_at = time.monotonic()
            if item.key_frame:
                stats.key_frames += 1
                self.last_key_frame = item
            event = Event(VIDEO, packet=item)
        for sub in list(self._subscriptions):
            if sub.video:
                sub._offer(event)

    def publish_audio(self, item: MediaPacket) -> None:
        if item.config:
            self.audio_config = item
            event = Event(AUDIO_CONFIG, packet=item)
        else:
            self.audio_stats.packets += 1
            self.audio_stats.bytes += len(item.data)
            self.audio_stats.last_at = time.monotonic()
            event = Event(AUDIO, packet=item)
        for sub in list(self._subscriptions):
            if sub.audio:
                sub._offer(event)

    def close(self) -> None:
        for sub in list(self._subscriptions):
            sub.close()
