"""Capture quality presets and their bandwidth estimates.

The bit rate is the encoder's target ceiling: busy scenes approach it, while
near-static VR menus use much less (a real Quest 3 at "standard" measured
~0.5-0.7 Mbps on a quiet screen). Plan networks with the ceiling.

The CLI (scripts/quest_multi_scrcpy.sh --quality) uses the same values.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

AUDIO_MBPS = 0.128          # Opus default bit rate in scrcpy
OVERHEAD = 1.03             # TCP/IP + scrcpy packet headers (~3 %)


@dataclass(frozen=True)
class QualityPreset:
    name: str
    label: str
    max_size: int        # longest side in px (Quest 3: 1280 -> 1280x684)
    max_fps: int
    video_bit_rate: int  # bits per second
    description: str

    def mbps(self, audio: bool = True) -> float:
        """Estimated peak bandwidth for one headset (Mbps)."""
        return round((self.video_bit_rate / 1e6 + (AUDIO_MBPS if audio else 0)) * OVERHEAD, 2)

    def total_mbps(self, devices: int, audio: bool = True) -> float:
        return round(self.mbps(audio) * devices, 1)

    def to_dict(self) -> dict:
        return {**asdict(self), "mbps": self.mbps(), "mbps_50": self.total_mbps(50)}


PRESETS: dict[str, QualityPreset] = {
    "low": QualityPreset("low", "低（省帯域）", 960, 15, 1_500_000,
                         "50台の常時監視向け。文字は読みにくいが動きは追える"),
    "standard": QualityPreset("standard", "標準", 1280, 30, 4_000_000,
                              "既定。授業中の一覧表示と録画の両立"),
    "high": QualityPreset("high", "高（詳細）", 1920, 60, 10_000_000,
                          "数台の詳細確認・高品質な録画向け。多台数では帯域不足に注意"),
}
DEFAULT_PRESET = "standard"


def get_preset(name: str) -> QualityPreset:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(f"unknown quality preset {name!r} (choose from {', '.join(PRESETS)})") from None


def bandwidth_table(device_counts=(1, 10, 30, 50)) -> str:
    """Markdown table used in the docs (kept in sync by tests)."""
    head = "| プリセット | 長辺 | fps | 映像ビットレート | 1台あたり（目安） | " + \
        " | ".join(f"{n}台" for n in device_counts[1:]) + " |"
    sep = "|" + "---|" * (5 + len(device_counts) - 1)
    rows = []
    for p in PRESETS.values():
        totals = " | ".join(f"{p.total_mbps(n):g} Mbps" for n in device_counts[1:])
        rows.append(f"| `{p.name}` {p.label} | {p.max_size}px | {p.max_fps} | {p.video_bit_rate / 1e6:g} Mbps | "
                    f"約{p.mbps():g} Mbps | {totals} |")
    return "\n".join([head, sep, *rows])
