"""Black-capture detection, diagnostics and recovery for Meta Quest.

Background (docs/GUI_DESIGN.md §4.10): on Horizon OS the scrcpy server does
not capture SurfaceFlinger. `DisplayManager.createVirtualDisplay(display 0)`
is routed to Meta's Strata compositor recording (SurfaceForger
.nativeRecordDisplay). When the compositor does not render into that
recording (headset paused / proximity "removed", some UI transitions) or the
content is withheld (passthrough camera, protected layers), the stream keeps
running but every frame is black. Nothing errors, so it must be detected
from the pixels.

Only key frames are decoded (every ~2 s on Quest, plus one right after
RESET_VIDEO), so the cost is small even for 50 headsets.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

DEFAULT_THRESHOLD = 16.0        # 99th-percentile luma below this = black frame


@dataclass
class FrameLuma:
    mean: float
    p99: float

    @property
    def black(self) -> bool:
        return self.p99 < DEFAULT_THRESHOLD


class KeyFrameProbe:
    """Decode a key frame (Annex B config + IDR) and measure its luma (blocking)."""

    def __init__(self):
        import av  # imported lazily: tests without PyAV can still import this module
        self._av = av
        self._decoder = None

    def measure(self, config: bytes, key_frame: bytes) -> FrameLuma | None:
        import numpy as np
        if self._decoder is None:
            self._decoder = self._av.CodecContext.create("h264", "r")
        try:
            frames = self._decoder.decode(self._av.Packet(config + key_frame))
        except Exception:
            self._decoder = None   # reset after a bitstream error
            return None
        if not frames:
            return None
        y = frames[-1].to_ndarray(format="gray")
        return FrameLuma(mean=float(y.mean()), p99=float(np.percentile(y, 99)))


@dataclass
class BlackState:
    """Hysteresis: black only after `confirm_s` of consecutive black key frames."""
    confirm_s: float = 3.0
    first_black: float | None = None
    black: bool = False
    since: float | None = None
    last_luma: FrameLuma | None = None

    def update(self, luma: FrameLuma, now: float | None = None) -> str | None:
        """Returns "black" or "visible" when the confirmed state changes."""
        now = time.monotonic() if now is None else now
        self.last_luma = luma
        if luma.black:
            if self.first_black is None:
                self.first_black = now
            if not self.black and now - self.first_black >= self.confirm_s:
                self.black, self.since = True, self.first_black
                return "black"
            return None
        self.first_black = None
        if self.black:
            self.black, self.since = False, None
            return "visible"
        return None


# Escalating recovery while the capture stays black (seconds after confirmation).
RECOVERY_STEPS = (
    (0.0, "reset_video"),     # re-create the Strata recording (RESET_VIDEO)
    (6.0, "wake"),            # KEYCODE_WAKEUP + proximity "worn" (prox_close)
    (12.0, "restart"),        # restart the scrcpy server on the headset
)
RECOVERY_REPEAT = 60.0        # start over at most once a minute while still black

DIAG_COMMANDS = (
    ("compositor", "dumpsys SurfaceFlinger | grep -A22 -- '--------Strata composer--------'"),
    ("resumed", "dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity' | head -20"),
    ("power", "dumpsys power | grep -E 'mWakefulness=|Display Power|mHoldingDisplaySuspendBlocker'"),
    ("vrpower", "dumpsys vrpowermanager 2>/dev/null | head -40"),
    ("displays", "dumpsys display | grep -E 'mBaseDisplayInfo=DisplayInfo' | cut -c1-200"),
)


def summarize_diagnostics(sections: dict[str, str]) -> dict:
    """Pull the fields that tell the causes apart out of the raw dumps."""
    comp = sections.get("compositor", "")
    summary: dict = {}
    for line in comp.splitlines():
        line = line.strip()
        if line.startswith("Current strata layer:"):
            summary["strata_layer"] = line.split(":", 1)[1].strip()
        elif line.startswith("ovrState:"):
            summary["ovr_state"] = line.split(":", 1)[1].strip()
            summary["passthrough"] = "passthrough: 1" in line
        elif line.startswith("-> S"):
            summary["active_layer"] = line[3:].strip()
            summary["protected"] = "protected" in line
    resumed = [l.split("u0 ", 1)[-1].split(" ")[0] for l in sections.get("resumed", "").splitlines() if "u0 " in l]
    summary["resumed"] = sorted(set(resumed))[:8]
    for line in sections.get("vrpower", "").splitlines():
        if "proximity state:" in line.lower():
            summary["proximity"] = line.split(":", 1)[1].strip()
            break
    power = sections.get("power", "")
    if "mWakefulness=" in power:
        summary["wakefulness"] = power.split("mWakefulness=", 1)[1].split()[0]
    return summary


def likely_cause(summary: dict) -> str:
    if summary.get("wakefulness") and summary["wakefulness"] != "Awake":
        return "ヘッドセットがスリープ中（画面オフ）"
    if summary.get("proximity", "").upper() == "OPEN":
        return "ヘッドセットを装着していない（近接センサー）"
    if summary.get("passthrough"):
        return "パススルー表示中（Meta はカメラ映像を録画させない）"
    if summary.get("protected"):
        return "保護されたレイヤーを表示中（Meta が録画を禁止）"
    return "コンポジターが録画先に描画していない（未装着・遷移中など）"
