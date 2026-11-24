"""Device registry: the only writer of persistent device data (docs/GUI_DESIGN.md §3.3).

Key: the headset serial (ro.serialno). Rows imported from the CLI TSV, which
has no serial, use a placeholder key "tsv:<ip:port>" until the headset is
seen and `observe()` merges them.

SSID policy (§4.9): any SSID is accepted. The first SSID observed in this
run becomes the baseline; a different SSID later raises a warning that stays
until acknowledged (which adopts the new SSID as the baseline).
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time

from questhub.core import events as ev
from questhub.core.events import EventBus
from questhub.devices.wifi_parse import WifiInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    serial TEXT PRIMARY KEY,
    alias TEXT,
    ip TEXT,
    port INTEGER DEFAULT 5555,
    ip_locked INTEGER DEFAULT 0,
    grid_col INTEGER,
    grid_row INTEGER,
    model TEXT,
    quality TEXT,
    notes TEXT,
    ssid TEXT,
    bssid TEXT,
    band TEXT,
    rssi INTEGER,
    freq INTEGER,
    link_mbps INTEGER,
    wifi_seen_at REAL,
    ssid_baseline TEXT,
    battery INTEGER,
    charging INTEGER,
    last_seen REAL,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT, source TEXT, ip TEXT, port INTEGER, ssid TEXT, bssid TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT, kind TEXT, message TEXT, data TEXT, created_at REAL, acked_at REAL
);
"""

EDITABLE = {"alias", "ip", "port", "grid_col", "grid_row", "quality", "notes"}
MAX_OBSERVATIONS_PER_DEVICE = 200
TRUSTED_SOURCES = {"usb", "manual"}  # may overwrite a manually locked IP


class Registry:
    def __init__(self, path: str | Path, bus: EventBus | None = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.bus = bus or EventBus()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------ queries

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM devices ORDER BY COALESCE(grid_row, 999), COALESCE(grid_col, 999), alias, serial"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, serial: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM devices WHERE serial = ?", (serial,)).fetchone()
        return dict(row) if row else None

    def find_by_endpoint(self, ip: str, port: int = 5555) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM devices WHERE ip = ? AND port = ?", (ip, port)).fetchone()
        return dict(row) if row else None

    def warnings(self, include_acked: bool = False) -> list[dict]:
        sql = "SELECT * FROM warnings" + ("" if include_acked else " WHERE acked_at IS NULL") + " ORDER BY id DESC"
        with self._lock:
            rows = self._db.execute(sql).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def observations(self, serial: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM observations WHERE serial = ? ORDER BY id DESC LIMIT ?",
                                    (serial, limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ writes

    def _ensure(self, serial: str) -> dict:
        row = self.get(serial)
        if row is None:
            now = time.time()
            with self._lock:
                self._db.execute("INSERT INTO devices (serial, created_at) VALUES (?, ?)", (serial, now))
                self._db.commit()
            row = self.get(serial)
        return row

    def _set(self, serial: str, **values) -> None:
        if not values:
            return
        cols = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            self._db.execute(f"UPDATE devices SET {cols} WHERE serial = ?", (*values.values(), serial))
            self._db.commit()

    def start_run(self) -> None:
        """A new run of the app: forget SSID baselines (they are per run)."""
        with self._lock:
            self._db.execute("UPDATE devices SET ssid_baseline = NULL")
            self._db.commit()

    def observe(self, serial: str, source: str, ip: str | None = None, port: int | None = None,
                wifi: WifiInfo | None = None, model: str | None = None,
                battery: int | None = None, charging: bool | None = None) -> dict:
        """Record what we just saw about a headset; returns the updated row."""
        now = time.time()
        created = self.get(serial) is None
        if ip is not None:
            self._merge_placeholder(serial, ip, port or 5555)
        row = self._ensure(serial)
        updates: dict = {"last_seen": now}
        if model:
            updates["model"] = model
        if battery is not None:
            updates["battery"] = battery
        if charging is not None:
            updates["charging"] = int(charging)
        if not row["alias"]:
            updates["alias"] = self._default_alias()

        if ip is not None:
            port = port or row["port"] or 5555
            locked = bool(row["ip_locked"]) and source not in TRUSTED_SOURCES
            if not locked and (ip != row["ip"] or port != row["port"]):
                self._release_endpoint(ip, port, keep=serial)
                updates.update(ip=ip, port=port)
                if source == "usb":
                    updates["ip_locked"] = 0
                if row["ip"]:
                    self.bus.publish(ev.IP_CHANGED, serial, old=row["ip"], new=ip, source=source)

        if wifi is not None:
            updates.update(ssid=wifi.ssid, bssid=wifi.bssid, band=wifi.band, rssi=wifi.rssi_dbm,
                           freq=wifi.freq_mhz, link_mbps=wifi.link_mbps, wifi_seen_at=now)
            self._check_ssid(serial, row, wifi)

        self._set(serial, **updates)
        if ip is not None or wifi is not None:
            with self._lock:
                self._db.execute(
                    "INSERT INTO observations (serial, source, ip, port, ssid, bssid, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (serial, source, ip, port, wifi.ssid if wifi else None, wifi.bssid if wifi else None, now))
                self._db.execute(
                    "DELETE FROM observations WHERE serial = ? AND id NOT IN "
                    "(SELECT id FROM observations WHERE serial = ? ORDER BY id DESC LIMIT ?)",
                    (serial, serial, MAX_OBSERVATIONS_PER_DEVICE))
                self._db.commit()
        result = self.get(serial)
        self.bus.publish(ev.DEVICE_OBSERVED, serial, source=source, created=created)
        return result

    def _check_ssid(self, serial: str, row: dict, wifi: WifiInfo) -> None:
        baseline = row["ssid_baseline"]
        if wifi.ssid is None:
            if row["ssid"] and not self._open_warning(serial, "wifi_unavailable"):
                self._warn(serial, "wifi_unavailable", "Wi-Fi情報を取得できません", previous=row["ssid"])
            return
        self._ack_kind(serial, "wifi_unavailable")
        if baseline is None:
            self._set(serial, ssid_baseline=wifi.ssid)
            return
        if wifi.ssid != baseline:
            existing = self._open_warning(serial, "ssid_changed")
            if existing and existing["data"].get("new") == wifi.ssid:
                return
            if existing:
                self.ack_warning(existing["id"], adopt=False)
            self._warn(serial, "ssid_changed", f"SSID が {baseline} から {wifi.ssid} に変わりました",
                       old=baseline, new=wifi.ssid)
            self.bus.publish(ev.SSID_CHANGED, serial, old=baseline, new=wifi.ssid)
        else:
            # Back on the baseline network: the earlier warning no longer applies.
            self._ack_kind(serial, "ssid_changed")

    def expect_ssid(self, serial: str, ssid: str) -> None:
        """The operator moves this headset to `ssid` on purpose: make it the baseline, no warning."""
        self._set(serial, ssid_baseline=ssid)
        self._ack_kind(serial, "ssid_changed")

    def restore_ssid_baseline(self, serial: str, ssid: str | None) -> None:
        self._set(serial, ssid_baseline=ssid)

    def _warn(self, serial: str, kind: str, message: str, **data) -> None:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO warnings (serial, kind, message, data, created_at) VALUES (?, ?, ?, ?, ?)",
                (serial, kind, message, json.dumps(data, ensure_ascii=False), now))
            self._db.commit()
        self.bus.publish(ev.NETWORK_WARNING, serial, id=cur.lastrowid, kind=kind, message=message, **data)

    def _open_warning(self, serial: str, kind: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM warnings WHERE serial = ? AND kind = ? AND acked_at IS NULL ORDER BY id DESC",
                (serial, kind)).fetchone()
        return {**dict(row), "data": json.loads(row["data"] or "{}")} if row else None

    def _ack_kind(self, serial: str, kind: str) -> None:
        with self._lock:
            self._db.execute("UPDATE warnings SET acked_at = ? WHERE serial = ? AND kind = ? AND acked_at IS NULL",
                             (time.time(), serial, kind))
            self._db.commit()

    def ack_warning(self, warning_id: int, adopt: bool = True) -> dict | None:
        """Acknowledge a warning; for an SSID change the new SSID becomes the baseline."""
        with self._lock:
            row = self._db.execute("SELECT * FROM warnings WHERE id = ?", (warning_id,)).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE warnings SET acked_at = ? WHERE id = ?", (time.time(), warning_id))
            self._db.commit()
        data = json.loads(row["data"] or "{}")
        if adopt and row["kind"] == "ssid_changed" and data.get("new"):
            self._set(row["serial"], ssid_baseline=data["new"])
        self.bus.publish(ev.WARNING_ACKED, row["serial"], id=warning_id, kind=row["kind"])
        return dict(row)

    def update(self, serial: str, **values) -> dict:
        """Manual edits from the GUI. A manual IP is locked against scans/telemetry."""
        self._ensure(serial)
        values = {k: v for k, v in values.items() if k in EDITABLE}
        if "ip" in values:
            if values["ip"]:
                self._release_endpoint(values["ip"], int(values.get("port") or 5555), keep=serial)
            values["ip_locked"] = 1 if values["ip"] else 0
        self._set(serial, **values)
        self.bus.publish(ev.REGISTRY_CHANGED, serial, fields=sorted(values))
        return self.get(serial)

    def delete(self, serial: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM devices WHERE serial = ?", (serial,))
            self._db.execute("DELETE FROM observations WHERE serial = ?", (serial,))
            self._db.commit()
        self.bus.publish(ev.REGISTRY_CHANGED, serial, deleted=True)

    def _release_endpoint(self, ip: str, port: int, keep: str) -> None:
        """Another headset now owns ip:port; the old owner's address is stale."""
        with self._lock:
            rows = self._db.execute("SELECT serial FROM devices WHERE ip = ? AND port = ? AND serial != ?",
                                    (ip, port, keep)).fetchall()
            for r in rows:
                if not r["serial"].startswith("tsv:"):
                    self._db.execute("UPDATE devices SET ip = NULL, ip_locked = 0 WHERE serial = ?", (r["serial"],))
            self._db.commit()

    def _merge_placeholder(self, serial: str, ip: str, port: int) -> None:
        """A TSV row for ip:port turns out to be `serial`: keep its alias/layout."""
        placeholder = self.get(f"tsv:{ip}:{port}")
        if not placeholder or placeholder["serial"] == serial:
            return
        existing = self._ensure(serial)
        keep = {k: placeholder[k] for k in ("alias", "grid_col", "grid_row", "quality", "notes")
                if placeholder[k] is not None and existing[k] is None}
        if placeholder["alias"] and existing["alias"] and existing["alias"].startswith("Quest-"):
            keep["alias"] = placeholder["alias"]
        self._set(serial, **keep)
        self.delete(placeholder["serial"])

    def _default_alias(self) -> str:
        with self._lock:
            used = {r["alias"] for r in self._db.execute("SELECT alias FROM devices WHERE alias IS NOT NULL")}
        n = 1
        while f"Quest-{n:02d}" in used:
            n += 1
        return f"Quest-{n:02d}"

    # ------------------------------------------------------------ TSV (CLI compatibility)

    def import_tsv(self, text: str) -> int:
        count = 0
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            alias, endpoint = cols[0].strip(), cols[1].strip()
            ip, _, port = endpoint.partition(":")
            port = int(port or 5555)
            col = int(cols[2]) if len(cols) > 2 and cols[2].strip().isdigit() else None
            row = int(cols[3].split("#")[0].strip()) if len(cols) > 3 and cols[3].split("#")[0].strip().isdigit() else None
            existing = self.find_by_endpoint(ip, port)
            serial = existing["serial"] if existing else f"tsv:{ip}:{port}"
            self._ensure(serial)
            self._set(serial, alias=alias, ip=ip, port=port, grid_col=col, grid_row=row)
            count += 1
        self.bus.publish(ev.REGISTRY_CHANGED, None, imported=count)
        return count

    def export_tsv(self) -> str:
        lines = ["# alias\tendpoint(=IP[:PORT])\t[col]\t[row]", "# Exported from Quest Hub"]
        for d in self.list():
            if not d["ip"]:
                continue
            cols = [d["alias"] or d["serial"], f"{d['ip']}:{d['port'] or 5555}"]
            if d["grid_col"] is not None and d["grid_row"] is not None:
                cols += [str(d["grid_col"]), str(d["grid_row"])]
            lines.append("\t".join(cols))
        return "\n".join(lines) + "\n"

    def export_network_csv(self) -> str:
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["alias", "serial", "ip", "ssid", "bssid", "band", "rssi_dbm", "freq_mhz", "link_mbps", "seen"])
        for d in self.list():
            seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["wifi_seen_at"])) if d["wifi_seen_at"] else ""
            writer.writerow([d["alias"], d["serial"], d["ip"], d["ssid"], d["bssid"], d["band"], d["rssi"],
                             d["freq"], d["link_mbps"], seen])
        return buf.getvalue()

    def close(self) -> None:
        with self._lock:
            self._db.close()
