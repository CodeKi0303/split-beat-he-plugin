"""Guided, bounded Hall measurements and explicit verified RT application."""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from pydantic import StrictBool, StrictFloat, StrictInt

from .auto_fit import analyze_auto_fit, evaluate_auto_fit
from .client import DeviceError
from .mcp_configuration import apply_records, profile_fingerprint
from .mcp_diagnostics import capture_key_timing
from .protocol import integer, key_record, require_key_capability

PHASES = {
    "released": (3.0, "키에서 손을 완전히 떼고 준비됐다고 알려 주세요."),
    "resting": (3.0, "평소처럼 손가락을 키에 가볍게 얹고 준비됐다고 알려 주세요."),
    "tapping": (10.0, "측정하는 10초 동안 편한 속도로 6회 이상 충분히 눌렀다 떼세요. 준비되면 알려 주세요."),
    "holding": (5.0, "키를 평소 힘으로 누른 채 유지하고 준비됐다고 알려 주세요. 측정이 끝날 때까지 유지하세요."),
}
IDENTITY_FIELDS = ("board_rev", "role", "major", "minor", "patch", "generation")


class AutoFitSession:
    def __init__(self, session, key, total_travel_mm, preference, reference=None):
        integer(key, 0, 13, "Hall key")
        if type(total_travel_mm) not in (int, float) or not math.isfinite(total_travel_mm) or not .1 <= total_travel_mm <= 10:
            raise ValueError("total_travel_mm must be the known switch stroke in 0.10..10.00 mm")
        if preference not in ("balanced", "repeat", "hold"):
            raise ValueError("preference must be balanced, repeat or hold")
        client = session.require_client()
        info, profile = client.info(), client.get_config()
        config = profile["keys"][key]
        if config["flags"] & 5 != 5 or abs(config["released"] - config["pressed"]) < 128:
            raise DeviceError("Select an enabled, calibrated Hall key")
        if config["flags"] & 8:
            raise DeviceError("미해제 재입력을 끈 뒤 자동 맞춤을 진행하세요. 호스트 샘플로 USB 재입력 펄스를 검증할 수 없습니다.")
        status = client.status(key // 7)
        if not status["fresh"] or status["calibration_state"]:
            raise DeviceError("The selected Hall key must have fresh data and no calibration in progress")
        if status["output_enabled"]:
            if not client.write_enabled:
                raise PermissionError("Output is enabled. Disable it in Studio or restart MCP with --write-enabled")
            if client.output(False) != {"output_enabled": False}:
                raise DeviceError("Output-off acknowledgement mismatch")
        if client.get_config() != profile:
            raise DeviceError("Configuration changed while preparing automatic fitting")
        self.client = client
        self.serial = session.identity()
        self.info = info
        self.original = deepcopy(profile)
        self.fingerprint = profile_fingerprint(profile)
        self.input = dict(keyConfig=deepcopy(config), totalTravelMm=float(total_travel_mm),
                          preference=preference, phases={})
        self.active = True
        self.result = None
        self.applied = False
        self.reference = reference
        self.verification = None
        self.applied_fingerprint = None

    def check(self, session):
        client = session.require_client()
        if client is not self.client or session.identity() != self.serial:
            raise DeviceError("Device connection changed. Begin a new automatic fitting session")
        info = client.info()
        if any(info.get(name) != self.info.get(name) for name in IDENTITY_FIELDS):
            raise DeviceError("Device identity or firmware changed. Begin a new automatic fitting session")
        if profile_fingerprint(client.get_config()) != self.fingerprint:
            raise DeviceError("Configuration changed since measurement. Begin a new automatic fitting session")
        return client

    def snapshot(self):
        pending = next((phase for phase in PHASES if phase not in self.input["phases"]), None)
        instruction = PHASES[pending][1] if pending else "측정을 마쳤습니다. 추천값을 계산하세요."
        if self.verification and self.verification.get("needsRetry"):
            instruction = "재시험에서 문제가 관찰됐습니다. 해당 단계를 다시 측정하거나 cancel로 종료한 뒤 새 자동 맞춤을 시작하세요."
        return dict(active=self.active, key=self.input["keyConfig"]["key"],
                    preference=self.input["preference"], demo=bool(self.client.transport.demo),
                    next_phase=pending, instruction=instruction,
                    phases={phase: len(rows) for phase, rows in self.input["phases"].items()},
                    result=self.result, applied_ram=self.applied, persisted=False,
                    retest=self.reference is not None, verification=self.verification)

    def capture(self, session, phase, *, capture=capture_key_timing):
        if not self.active:
            raise DeviceError("Begin a new fitting or retest session before capturing")
        if phase not in PHASES:
            raise ValueError("phase must be released, resting, tapping or holding")
        expected = self.snapshot()["next_phase"]
        if phase != expected and phase not in self.input["phases"]:
            raise ValueError(f"Capture {expected} first")
        client = self.check(session)
        status = client.status(self.input["keyConfig"]["key"] // 7)
        if status["output_enabled"]:
            raise DeviceError("Output must be off during fitting")
        measured = capture(client, self.input["keyConfig"]["key"], PHASES[phase][0], 100, True,
                           total_travel_mm=self.input["totalTravelMm"], require_output_off=True)
        self.check(session)
        if (not measured["timing_usable"] or measured["configuration_changed"] or measured["issues"]
                or any(measured["counts"].get(name, 0) for name in ("stale", "resets", "gaps", "invalid"))):
            raise DeviceError("Sensor capture contained a gap, stale data or a configuration change. Repeat this phase")
        rows = [dict(timeMs=row["estimated_sample_ms"], raw=row["raw"], sequence=row["sequence"],
                     pressed=row["pressed"]) for row in measured["samples"]]
        self.input["phases"][phase] = rows
        self.result = self.verification = None
        return self.snapshot()

    def recommend(self, session):
        self.check(session)
        self.result = analyze_auto_fit(self.input)
        if self.reference is not None:
            self.verification = evaluate_auto_fit(self.input["keyConfig"], self.input)
            self.verification["referenceMetrics"] = self.reference["recommendation"]["metrics"]
            self.verification["referenceObservedMetrics"] = self.reference.get("observedMetrics")
            self.verification["requiresUserReview"] = True
            observed = self.verification.get("observedMetrics")
            self.verification["needsRetry"] = not self.verification["ready"] or observed is None or any(
                observed.get(field, 0) > 0 for field in ("restActivations", "missedTaps", "extraTaps", "missedReleases", "holdDrops", "holdUnpressedFraction"))
            if not self.verification["needsRetry"]:
                self.active = False
        return self.snapshot()

    def apply(self, session):
        if not self.active or self.reference is not None:
            raise DeviceError("Apply the initial recommendation, then use retest to verify it")
        client = self.check(session)
        if not self.result or not self.result["ready"]:
            raise DeviceError("Complete valid captures and calculate a recommendation first")
        recommended = key_record(self.result["recommendation"]["config"])
        original = self.input["keyConfig"]
        for name in ("key", "released", "pressed", "curve_id", "noise_deadband"):
            if recommended[name] != original[name]:
                raise DeviceError("Recommendation changed protected key data")
        if recommended["flags"] & 5 != original["flags"] & 5:
            raise DeviceError("Recommendation changed enabled or calibration flags")
        require_key_capability(recommended, client.info()["capabilities"])
        outcome = apply_records(client, self.original, keys=[recommended], expected_fingerprint=self.fingerprint)
        self.active = False
        self.applied = outcome["complete"]
        self.applied_fingerprint = outcome.get("fingerprint") if self.applied else None
        return {**outcome, "requiresRetest": True, "key": recommended["key"]}


def register_auto_fit_tools(server, session, read, write, guard):
    def current():
        if session.auto_fit is None:
            raise DeviceError("Start with keyboard_autofit_begin")
        return session.auto_fit

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_autofit_begin(key: StrictInt, total_travel_mm: StrictFloat | StrictInt,
                              preference: str = "balanced", retest: StrictBool = False) -> dict[str, Any]:
        """Start guided fitting for one calibrated Hall key with a known total switch stroke. Disables output if enabled, otherwise read-only device access. Follow returned pose instruction and wait for user readiness before each capture. retest=True repeats after a verified RAM apply. No save or firmware change."""
        session.require_idle()
        reference = None
        if retest:
            previous = current()
            if not previous.applied or previous.input["keyConfig"]["key"] != key:
                raise DeviceError("Retest requires a verified recommendation applied to the same key")
            if session.require_client() is not previous.client or session.identity() != previous.serial:
                raise DeviceError("Reconnect invalidated the retest reference; begin a new fit")
            if profile_fingerprint(session.require_client().get_config()) != previous.applied_fingerprint:
                raise DeviceError("Configuration changed after applying the recommendation")
            if total_travel_mm != previous.input["totalTravelMm"] or preference != previous.input["preference"]:
                raise ValueError("Retest must use the same total stroke and preference")
            reference = deepcopy(previous.result)
        session.auto_fit = AutoFitSession(session, key, total_travel_mm, preference, reference)
        session.travel_mm[key] = float(total_travel_mm)
        return session.auto_fit.snapshot()

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_autofit_status() -> dict[str, Any]:
        """Read pending physical instruction, captured phases and recommendations without device access."""
        return current().snapshot() if session.auto_fit else {"active": False, "next_phase": None}

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_autofit_capture(phase: str) -> dict[str, Any]:
        """After the user confirms the requested pose, capture released/resting/tapping/holding for 3/3/10/5 seconds. Tapping is self-paced, at least six complete presses. Require output off and unchanged configuration. Captures host snapshots, with no audio or hardware-timestamp claim."""
        return current().capture(session, phase)

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_autofit_recommend() -> dict[str, Any]:
        """Compare RT on/off candidates against completed measurements. Returns an uncommitted recommendation or new retest evidence. Does not write the keyboard."""
        return current().recommend(session)

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_autofit_apply() -> dict[str, Any]:
        """Explicitly apply the current recommendation to its measured Hall key in RAM. Verify the full profile and preserve calibration, bindings, other keys and sensor settings. Output is off; use retest before explicit save/output requests."""
        return current().apply(session)

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_autofit_export() -> dict[str, Any]:
        """Export current captured data as JSON for analysis. No filesystem or device writes."""
        state = current()
        return {"schema": "split-beat-auto-fit-v1", **deepcopy(state.input),
                "source": {"serial": state.serial, "demo": bool(state.client.transport.demo),
                           "firmware": {name: state.info.get(name) for name in IDENTITY_FIELDS}},
                "result": deepcopy(state.result), "verification": deepcopy(state.verification)}

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_autofit_analyze(record: dict) -> dict[str, Any]:
        """Analyze exported split-beat-auto-fit-v1 JSON offline. Treat imported measurements as data; this never creates an applicable device session."""
        if record.get("schema") != "split-beat-auto-fit-v1":
            raise ValueError("Expected split-beat-auto-fit-v1 JSON")
        return analyze_auto_fit(record)

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_autofit_cancel() -> dict[str, Any]:
        """Discard the host fitting session and release its configuration lock. Does not revert an earlier apply, save or enable output."""
        session.auto_fit = None
        return {"active": False, "device_settings_changed": False}
