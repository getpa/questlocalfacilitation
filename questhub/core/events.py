"""In-process event bus (docs/GUI_DESIGN.md §3.2 rule 3).

Modules publish state changes here; upper layers subscribe. Publishing never
blocks: a slow subscriber loses its oldest events.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field
import itertools
import time

# Event types
USB_DEVICE_ATTACHED = "usb_device_attached"
CONNECTION_CHANGED = "connection_changed"
MEDIA_STATE_CHANGED = "media_state_changed"
DEVICE_OBSERVED = "device_observed"
IP_CHANGED = "ip_changed"
SSID_CHANGED = "ssid_changed"
TELEMETRY_UPDATED = "telemetry_updated"
NETWORK_WARNING = "network_warning"
WARNING_ACKED = "warning_acked"
PROVISIONING_PROGRESS = "provisioning_progress"
SCAN_PROGRESS = "scan_progress"
RECORDING_PROGRESS = "recording_progress"
RECORDING_STOPPED = "recording_stopped"
QUALITY_CHANGED = "quality_changed"
REGISTRY_CHANGED = "registry_changed"
NOTICE = "notice"

_ids = itertools.count(1)


@dataclass
class Event:
    type: str
    serial: str | None = None
    data: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    id: int = field(default_factory=lambda: next(_ids))

    def to_dict(self) -> dict:
        return asdict(self)


class Subscription:
    def __init__(self, bus: "EventBus", types: set[str] | None, maxsize: int):
        self._bus = bus
        self.types = types
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize)

    def offer(self, event: Event) -> None:
        if self.types and event.type not in self.types:
            return
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(event)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def get(self, timeout: float | None = None) -> Event:
        return await asyncio.wait_for(self.queue.get(), timeout)

    def close(self) -> None:
        self._bus._subs.discard(self)
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


class EventBus:
    def __init__(self, history: int = 300):
        self._subs: set[Subscription] = set()
        self.history: deque[Event] = deque(maxlen=history)

    def subscribe(self, *types: str, maxsize: int = 1000) -> Subscription:
        sub = Subscription(self, set(types) or None, maxsize)
        self._subs.add(sub)
        return sub

    def publish(self, type: str, serial: str | None = None, **data) -> Event:
        event = Event(type, serial, data)
        self.history.append(event)
        for sub in list(self._subs):
            sub.offer(event)
        return event

    def recent(self, limit: int = 100, types: set[str] | None = None) -> list[dict]:
        events = [e for e in self.history if not types or e.type in types]
        return [e.to_dict() for e in events[-limit:]]
