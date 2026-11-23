"""Settings persisted as JSON in the data directory (~/.questhub/settings.json)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path

from questhub import paths


@dataclass
class Settings:
    admin_host: str = "127.0.0.1"
    admin_port: int = 8765
    viewer_enabled: bool = False
    viewer_port: int = 8443
    ca_port: int = 8766
    viewer_insecure_http: bool = False     # development only: plain HTTP viewer (localhost testing)
    default_quality: str = "standard"
    audio: bool = True
    audio_dup: bool = True
    auto_stream: bool = True               # start streaming when a headset connects
    record_dir: str = str(paths.PROJECT_ROOT / "recordings")
    disk_min_free_gb: float = 5.0
    telemetry_interval: float = 60.0
    adb_concurrency: int = 8
    reconnect_max_backoff: float = 30.0
    quest_tweaks: bool = False
    wake_on_start: bool = True
    tile_fallback_fps: float = 20.0

    @classmethod
    def load(cls, path: Path) -> "Settings":
        settings = cls()
        if path.exists():
            data = json.loads(path.read_text())
            known = {f.name for f in fields(cls)}
            for key, value in data.items():
                if key in known:
                    setattr(settings, key, value)
        return settings

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
        tmp.replace(path)

    def update(self, values: dict) -> list[str]:
        changed = []
        for f in fields(self):
            if f.name in values and values[f.name] != getattr(self, f.name):
                value = values[f.name]
                if f.type in ("int", int):
                    value = int(value)
                elif f.type in ("float", float):
                    value = float(value)
                elif f.type in ("bool", bool):
                    value = bool(value)
                setattr(self, f.name, value)
                changed.append(f.name)
        return changed

    def to_dict(self) -> dict:
        return asdict(self)
