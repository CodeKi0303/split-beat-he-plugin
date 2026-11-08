"""Guided whole-keyboard calibration. Host samples continue between MCP calls."""
from __future__ import annotations

import copy
import math
import statistics
import threading
import time
from typing import Any

from pydantic import StrictInt

from .client import DeviceError
from .protocol import integer, key_record


def key_selection(keys):
    if not isinstance(keys, list) or not keys or len(keys) > 14:
        raise ValueError("keys must be a nonempty list of Hall indices 0..13")
    for key in keys:
        integer(key, 0, 13, "key")
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return sorted(keys)


class CaptureModel:
    """Pure measurement state; values are ADC counts, never physical mm."""
    def __init__(self, profile, now, press_seconds=120):
        self.original = copy.deepcopy(profile)
        self.started = now
        self.press_seconds = press_seconds
        self.state, self.error = "baseline", None
        self.rows = [[] for _ in range(14)]
        self.stats = [None] * 14
        self.recent = [[] for _ in range(14)]
        self.extremes = [[0, 0] for _ in range(14)]
        self.direction = [0] * 14
        self.sequences, self.last = [None, None], [now, now]
        self.last_tick = now

    def tick(self, now):
        if self.state not in ("baseline", "press"):
            return
        if now < self.last_tick:
            raise ValueError("Clock moved backwards")
        self.last_tick = now
        if any(now - stamp > .5 for stamp in self.last):
            raise ValueError("Fresh sensor stream stopped for over 500ms; restart calibration")
        if self.state == "baseline" and now - self.started >= 3:
            for key, row in enumerate(self.rows):
                if (len(row) < 30 or row[0][0] - self.started > .25
                        or self.started + 3 - row[-1][0] > .25
                        or row[-1][0] - row[0][0] < 2.5):
                    raise ValueError(f"Key {key}: incomplete 3-second released baseline")
                values = [v for _, v in row]
                if max(values) - min(values) > 64:
                    raise ValueError(f"Key {key}: unstable baseline; release all keys and restart")
                mean, variance = statistics.fmean(values), statistics.pvariance(values)
                self.stats[key] = dict(released=math.floor(mean + .5), mean=mean,
                                       variance=variance, stdev=math.sqrt(variance),
                                       samples=len(values), threshold=max(6, math.ceil(4*math.sqrt(variance)+2)))
            self.state = "press"
        if self.state == "press" and now >= self.started + 3 + self.press_seconds:
            self.state = "ready"

    def add(self, side, status, now):
        self.tick(now)
        if self.state not in ("baseline", "press"):
            return
        if not status.get("fresh") or status.get("calibration_state") != 0 or status.get("output_enabled") is not False:
            raise ValueError("Calibration requires fresh data, no device-side capture and HID output disabled")
        sequence = integer(status.get("sample_sequence"), 0, 0xffffffff, "sample_sequence")
        raw = status.get("raw")
        if not isinstance(raw, list) or len(raw) != 7:
            raise ValueError("Expected 7 ADC samples per side")
        for value in raw:
            integer(value, 0, 4095, "raw ADC")
        previous = self.sequences[side]
        if previous is not None:
            delta = (sequence - previous) & 0xffffffff
            if delta == 0:
                return
            if delta >= 0x80000000:
                raise ValueError("ADC sample sequence moved backwards; restart calibration")
        self.sequences[side], self.last[side] = sequence, now
        for offset, value in enumerate(raw):
            key = 7 * side + offset
            if self.state == "baseline":
                self.rows[key].append((now, value))
                continue
            delta = value - self.stats[key]["released"]
            recent = self.recent[key]
            recent.append(delta)
            del recent[:-2]
            if len(recent) < 2 or recent[0]*recent[1] <= 0:
                continue
            span = min(abs(v) for v in recent)
            if span < self.stats[key]["threshold"]:
                continue
            sign = 1 if delta > 0 else -1
            idx = 0 if sign == 1 else 1
            self.extremes[key][idx] = max(self.extremes[key][idx], span)
            if not self.direction[key] and span >= 128:
                self.direction[key] = sign

    def candidates(self):
        result = []
        for key, sign in enumerate(self.direction):
            if not sign:
                continue
            span = self.extremes[key][0 if sign == 1 else 1]
            original = self.original["keys"][key]
            result.append(key_record({**original, "flags": original["flags"] | 4,
                                      "released": self.stats[key]["released"],
                                      "pressed": self.stats[key]["released"] + sign*span}))
        return result

    def snapshot(self, now):
        eligible = {k["key"]: k for k in self.candidates()}
        return dict(state=self.state, error=self.error,
                    elapsed_seconds=max(0, now-self.started), baseline_seconds=3,
                    capture_seconds=self.press_seconds, eligible_keys=sorted(eligible),
                    keys=[dict(key=i, detected=max(self.extremes[i]) > 0,
                               eligible=i in eligible, span_raw=max(self.extremes[i]),
                               baseline=self.stats[i], candidate=eligible.get(i)) for i in range(14)],
                    note="Press each desired Hall key fully. Slight movement is detected, but applying needs at least 128 ADC counts. Full travel is not automatically proven.")


class BatchCalibration:
    def __init__(self, client, original, press_seconds=120):
        self.client = client
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.model = CaptureModel(original, time.monotonic(), press_seconds)
        self.thread = threading.Thread(target=self._run, daemon=True, name="keyboard-calibration")
        self.thread.start()

    @property
    def active(self):
        with self.lock:
            return self.model.state in ("baseline", "press")

    def _run(self):
        try:
            while not self.stop.is_set():
                for side in (0, 1):
                    with self.client.lock:
                        status = self.client.status(side)
                    with self.lock:
                        self.model.add(side, status, time.monotonic())
                if not self.active:
                    break
                self.stop.wait(.02)
        except Exception as exc:
            with self.lock:
                self.model.state, self.model.error = "error", str(exc)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=4)
        if self.thread.is_alive():
            raise DeviceError("Sensor transaction has not stopped; retry disconnect after it returns")
        with self.lock:
            if self.model.state == "baseline":
                self.model.state = "cancelled"
            elif self.model.state == "press":
                self.model.state = "ready"

    def snapshot(self):
        with self.lock:
            return {**self.model.snapshot(time.monotonic()), "demo": bool(self.client.transport.demo)}


def apply_capture(client, model, keys=None):
    if model.state != "ready":
        raise ValueError("A valid released baseline and press capture are required")
    candidates = {r["key"]: r for r in model.candidates()}
    selected = sorted(candidates) if keys is None else key_selection(keys)
    if not selected or any(key not in candidates for key in selected):
        raise ValueError("Selected keys must have eligible captured movement (span >=128)")
    expected = copy.deepcopy(model.original)
    for key in selected:
        expected["keys"][key] = candidates[key]
    with client.lock:
        if client.get_config() != model.original:
            raise DeviceError("Settings changed since capture; nothing applied. Restart calibration")
        if client.output(False) != {"output_enabled": False}:
            raise DeviceError("Output-off acknowledgement mismatch; nothing applied")
        if client.get_config() != model.original:
            raise DeviceError("Settings changed after output release; no calibration records written")
        error = None
        for key in selected:
            try:
                if client.set_key(candidates[key]) != candidates[key]:
                    raise DeviceError(f"Key {key} acknowledgement differs")
            except (ValueError, DeviceError, OSError, TimeoutError, PermissionError) as exc:
                error = str(exc)
                break
        try:
            actual = client.get_config()
        except (ValueError, DeviceError, OSError, TimeoutError) as exc:
            return dict(applied_ram=False, verified=False, persisted=False, error=error or str(exc),
                        outcome="uncertain_partial_ram", output_enabled=False)
    complete = actual == expected
    model.state = "applied" if complete else "partial"
    return dict(applied_ram=complete, verified=True, persisted=False, output_enabled=False,
                applied_keys=[k for k in selected if actual["keys"][k] == candidates[k]],
                error=None if complete else error or "Readback differs from expected profile",
                readback=actual, demo=bool(client.transport.demo))


def register_calibration_tools(server, session, read, write, guard):
    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_batch_calibration_start(press_seconds: StrictInt = 120) -> dict[str, Any]:
        """Release all 14 Hall keys before calling. Disable output, collect 3s released baseline then continuously capture full presses for 10..300s. Poll status; no automatic apply/save/output enable."""
        integer(press_seconds, 10, 300, "press_seconds")
        session.require_idle()
        client = session.require_client()
        if not client.write_enabled:
            raise PermissionError("Calibration requires --write-enabled")
        for side in (0, 1):
            status = client.status(side)
            if not status["fresh"] or status["calibration_state"] != 0:
                raise DeviceError("Both sides must be fresh with no device calibration running")
        original = client.get_config()
        if client.output(False) != {"output_enabled": False}:
            raise DeviceError("Output-off acknowledgement mismatch")
        session.calibration = BatchCalibration(client, original, press_seconds)
        return session.calibration.snapshot()

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_batch_calibration_status() -> dict[str, Any]:
        """Read running whole-keyboard calibration progress, noise baseline and detected/eligible keys. No device mutation."""
        return session.calibration.snapshot() if session.calibration else {"state": "idle"}

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_batch_calibration_apply(keys: list[StrictInt] | None = None) -> dict[str, Any]:
        """Stop capture; apply eligible Hall keys (all eligible if omitted), preserve RT/mapping and untouched calibration. Full readback; RAM only, no SAVE or output enable."""
        if session.calibration is None:
            raise ValueError("No batch calibration")
        if keys is not None:
            key_selection(keys)
        batch = session.calibration
        batch.close()
        return apply_capture(session.require_client(), batch.model, keys)

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_batch_calibration_cancel() -> dict[str, Any]:
        """Stop host capture without applying calibration. Output stays as it was after start (disabled); no save."""
        if session.calibration:
            session.calibration.close()
            session.calibration = None
        return {"state": "cancelled", "applied_ram": False, "persisted": False}

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_demo_pose(keys: list[StrictInt], value: StrictInt) -> dict[str, Any]:
        """Simulation only: hold selected Hall inputs at normalized0..1000 for calibration/diagnostic tests. First use initializes every demo key released. Never available for physical devices."""
        selected = key_selection(keys)
        integer(value, 0, 1000, "value")
        client = session.require_client()
        if not client.transport.demo:
            raise ValueError("Demo pose cannot control physical sensors")
        with client.lock:
            if client.transport.pose is None:
                client.transport.pose = [0]*14
            for key in selected:
                client.transport.pose[key] = value
            return {"demo": True, "normalized_pose": list(client.transport.pose)}
