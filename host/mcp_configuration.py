"""Explicit, verified configuration operations shared by MCP tools and tests.

No device is opened here. All changes use the serial-selected session client;
profile payloads are JSON data, never filesystem paths or arbitrary opcodes.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from typing import Any

from pydantic import StrictFloat, StrictInt

from .client import DeviceError
from .hall_curve import curve_catalog
from .protocol import (
    CAP_KEY_CHORD, CAP_RETRIGGER_BEFORE_RELEASE, DEFAULTS, ProtocolError, binding_record, integer, key_record,
    require_binding_capability, require_key_capability, validate_profile,
)

DISTANCES = ("actuation", "rt_press", "rt_release", "top_deadzone", "bottom_deadzone")
PATCH_FIELDS = {*DISTANCES, "noise_deadband", "enabled", "rapid_trigger", "retrigger_before_release", "curve_id"}
POSITION_NAMES = ("CAPS", "Q", "W", "E", "R", "SHIFT", "SPACE")


def profile_fingerprint(profile):
    """Canonical hash includes calibration and all bindings, not only selections."""
    normalized = validate_profile(profile)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_travel(value):
    if not isinstance(value, list) or len(value) != 14:
        raise ProtocolError("Exactly 14 total-travel values are required")
    result = []
    for key, mm in enumerate(value):
        if type(mm) not in (int, float) or not math.isfinite(mm) or not .1 <= mm <= 10:
            raise ProtocolError(f"Hall {key}: total travel must be 0.10..10.00 mm")
        result.append(float(mm))
    return result


def selected_keys(keys):
    if not isinstance(keys, list) or not keys or len(keys) > 14:
        raise ProtocolError("Select 1..14 Hall indices")
    for key in keys:
        integer(key, 0, 13, "Hall key")
    if len(set(keys)) != len(keys):
        raise ProtocolError("Duplicate Hall key indices")
    return list(keys)


def _millimetres(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ProtocolError(f"{name}: finite, nonnegative mm value required")
    decimal = Decimal(str(value))
    if decimal != decimal.quantize(Decimal("0.01")):
        raise ProtocolError(f"{name}: use 0.01 mm increments")
    return decimal


def propose_keys(profile, keys, patch, *, unit="normalized", travel_mm=None, capabilities=0):
    """Pure partial-field patch. Existing endpoints/calibrated flags are retained."""
    before = validate_profile(profile)
    ids = selected_keys(keys)
    if unit not in ("normalized", "mm"):
        raise ProtocolError("unit must be normalized or mm")
    if not isinstance(patch, dict) or not patch or not set(patch) <= PATCH_FIELDS:
        raise ProtocolError("Patch supports only actuation, rt_press, rt_release, top_deadzone, bottom_deadzone, noise_deadband, enabled, rapid_trigger, retrigger_before_release, curve_id")
    travel = validate_travel(travel_mm if travel_mm is not None else [4.0] * 14)
    integer(capabilities, 0, 0xffffffff, "capabilities")
    for name in ("enabled", "rapid_trigger", "retrigger_before_release"):
        if name in patch and type(patch[name]) is not bool:
            raise ProtocolError(f"{name}: boolean required")
    if "noise_deadband" in patch:
        integer(patch["noise_deadband"], 0, 100, "noise_deadband")
    if "curve_id" in patch:
        integer(patch["curve_id"], 0, 1, "curve_id")
    for name in DISTANCES:
        if name in patch:
            if unit == "mm":
                _millimetres(patch[name], name)
            else:
                integer(patch[name], 0, 1000, name)
    proposed = []
    for key in ids:
        old = before["keys"][key]
        record = dict(old)
        if any(name in patch for name in DISTANCES) and not old["flags"] & 4:
            raise ProtocolError(f"Hall {key}: calibrate before changing distances")
        total = Decimal(str(travel[key]))
        if unit == "mm":
            values = {name: Decimal(str(patch[name])) if name in patch
                      else Decimal(old[name]) * total / 1000 for name in DISTANCES}
            if any(values[name] > total for name in DISTANCES):
                raise ProtocolError(f"Hall {key}: distance exceeds total travel {total} mm")
            if values["top_deadzone"] >= values["actuation"] or values["actuation"] + values["bottom_deadzone"] > total:
                raise ProtocolError(f"Hall {key}: actuation must be deeper than top zone and above bottom zone")
        for name in DISTANCES:
            if name in patch:
                record[name] = (int((Decimal(str(patch[name])) * 1000 / total).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                                if unit == "mm" else patch[name])
        if "noise_deadband" in patch:
            record["noise_deadband"] = patch["noise_deadband"]
        if "curve_id" in patch:
            record["curve_id"] = patch["curve_id"]
        if "enabled" in patch:
            record["flags"] = (record["flags"] & ~1) | int(patch["enabled"])
        if "rapid_trigger" in patch:
            record["flags"] = (record["flags"] & ~2) | (int(patch["rapid_trigger"]) << 1)
        if "retrigger_before_release" in patch:
            record["flags"] = (record["flags"] & ~8) | (int(patch["retrigger_before_release"]) << 3)
        if patch.get("enabled") is True and not record["flags"] & 4:
            raise ProtocolError(f"Hall {key}: calibrate before enabling")
        record = key_record(record)
        require_key_capability(record, capabilities)
        proposed.append(record)
    return proposed


def propose_bindings(bindings, capabilities):
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 28:
        raise ProtocolError("Supply 1..28 complete binding records")
    result = [binding_record(value) for value in bindings]
    if len({item["input"] for item in result}) != len(result):
        raise ProtocolError("Duplicate binding input indices")
    for item in result:
        require_binding_capability(item, capabilities)
    return result


def _write_permission(client):
    if not client.write_enabled:
        raise PermissionError("read-only session: start with --write-enabled for changes")


def _expect(profile, fingerprint):
    if fingerprint is not None:
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ProtocolError("expected_fingerprint must be the lowercase SHA256 from a preview/export")
        if profile_fingerprint(profile) != fingerprint:
            raise DeviceError("Profile changed since preview; no settings were written")


def apply_records(client, original, *, keys=None, bindings=None, expected_fingerprint=None, allow_calibration=False):
    """Validate all targets before OUTPUT, then apply and compare full readback.

    Partial outcomes are returned explicitly; uncertain writes are not retried.
    No SAVE or output enable is issued.
    """
    _write_permission(client)
    original = validate_profile(original)
    _expect(original, expected_fingerprint)
    expected = deepcopy(original)
    selected = []
    for group, records, id_name, normalizer in (("keys", [] if keys is None else keys, "key", key_record),
                                                ("bindings", [] if bindings is None else bindings, "input", binding_record)):
        if not isinstance(records, list):
            raise ProtocolError("Proposed records must be lists")
        seen = set()
        for value in records:
            record = normalizer(value)
            index = record[id_name]
            if index in seen:
                raise ProtocolError(f"Duplicate {group} index")
            seen.add(index)
            old = original[group][index]
            if group == "keys" and not allow_calibration:
                if any(record[name] != old[name] for name in ("released", "pressed")) or (record["flags"] & 4) != (old["flags"] & 4):
                    raise ProtocolError("Performance changes cannot alter measured calibration")
                if not old["flags"] & 4 and any(record[name] != old[name] for name in DISTANCES):
                    raise ProtocolError("Calibrate before changing distances")
                if record["flags"] & 1 and not old["flags"] & 1 and not record["flags"] & 4:
                    raise ProtocolError("Calibrate before enabling")
            expected[group][index] = record
            selected.append((group, index, record))
    if not selected:
        raise ProtocolError("No settings were selected")
    expected = validate_profile(expected)
    capabilities = client.info()["capabilities"]
    for group, _, value in selected:
        (require_key_capability if group == "keys" else require_binding_capability)(value, capabilities)
    failure = None
    writes_attempted = 0
    output_off_acknowledged = False
    preflight = validate_profile(client.get_config())
    if preflight != original:
        return dict(complete=False, profile=preflight, fingerprint=profile_fingerprint(preflight),
                    verified_records=[], writes_attempted=0, persisted=False, output_off_acknowledged=False, unexpected_changes=True,
                    error="Profile changed before apply; no writes were sent")
    try:
        if client.output(False) != {"output_enabled": False}:
            raise DeviceError("Output OFF acknowledgement mismatch")
        output_off_acknowledged = True
        # OUTPUT may yield to another configuration owner; compare again.
        if validate_profile(client.get_config()) != original:
            raise DeviceError("Profile changed after output release; settings were not written")
        for group, index, record in selected:
            if record == original[group][index]:
                continue
            writes_attempted += 1
            echoed = client.set_key(record) if group == "keys" else client.set_binding(record)
            if echoed != record:
                raise DeviceError(f"{group} {index}: acknowledgement differs from requested record")
    except (ValueError, DeviceError, PermissionError, OSError, TimeoutError) as exc:
        failure = str(exc)
    try:
        actual = validate_profile(client.get_config())
    except (ValueError, DeviceError, PermissionError, OSError, TimeoutError) as exc:
        return dict(complete=False, profile=None, verified_records=[], writes_attempted=writes_attempted,
                    persisted=False, output_off_acknowledged=output_off_acknowledged,
                    unexpected_changes=None, error=f"{failure or ''} Final RAM readback unavailable: {exc}. Some settings may have changed; no retry or SAVE was sent.")
    selected_ids = {(group, index) for group, index, _ in selected}
    verified = [{"group": group, "index": index} for group, index, record in selected if actual[group][index] == record]
    unexpected = any(actual[group][index] != original[group][index] and
                     ((group, index) not in selected_ids or actual[group][index] != expected[group][index])
                     for group in ("keys", "bindings") for index in range(len(actual[group])))
    complete = output_off_acknowledged and actual == expected
    return dict(complete=complete, profile=actual, fingerprint=profile_fingerprint(actual),
                verified_records=verified, writes_attempted=writes_attempted, persisted=False,
                output_off_acknowledged=output_off_acknowledged, unexpected_changes=unexpected,
                error=None if complete else f"{failure or 'Readback differs from requested settings'}. No automatic retry or SAVE was sent.")


def parse_studio_profile(value):
    if isinstance(value, dict) and value.get("schema") == "c77-profile-v1":
        return validate_profile(value), None
    if not isinstance(value, dict) or set(value) != {"schema", "device", "ui"} or value["schema"] != "split-studio-profile-v1":
        raise ProtocolError("Expected c77-profile-v1 or split-studio-profile-v1 JSON data")
    if not isinstance(value["ui"], dict) or set(value["ui"]) != {"totalTravelMm"}:
        raise ProtocolError("UI metadata must contain only totalTravelMm")
    return validate_profile(value["device"]), validate_travel(value["ui"]["totalTravelMm"])


def make_studio_profile(profile, travel_mm):
    return dict(schema="split-studio-profile-v1", device=validate_profile(profile),
                ui={"totalTravelMm": validate_travel(travel_mm)})


def layout():
    """Physical positions from web/layout.mjs; labels never imply current mappings."""
    inputs = []
    for side in (0, 1):
        for index, name in enumerate(POSITION_NAMES):
            inputs.append(dict(input=side * 7 + index, side=side, type="hall", position=name,
                               label=("왼쪽" if side == 0 else "오른쪽") + f" {name} 위치",
                               source_ref=f"U{1001 + side * 100 + index}", supports_rt=True))
    extra = [(14, 0, "digital", "MX 1", "SW1201", None),
             (15, 0, "digital", "MX 4", "SW1204", None),
             (16, 0, "knob", "노브 클릭", "ENC1401", "click"),
             (17, 1, "digital", "MX 1", "SW1301", None),
             (18, 1, "digital", "MX 4", "SW1304", None),
             (19, 1, "knob", "노브 클릭", "ENC1501", "click"),
             (20, 0, "roller", "롤러 CW", "L_J1202", "cw"),
             (21, 0, "roller", "롤러 CCW", "L_J1202", "ccw"),
             (22, 0, "knob", "노브 CW", "ENC1401", "cw"),
             (23, 0, "knob", "노브 CCW", "ENC1401", "ccw"),
             (24, 1, "roller", "롤러 CW", "R_J1302", "cw"),
             (25, 1, "roller", "롤러 CCW", "R_J1302", "ccw"),
             (26, 1, "knob", "노브 CW", "ENC1501", "cw"),
             (27, 1, "knob", "노브 CCW", "ENC1501", "ccw")]
    for index, side, kind, name, ref, direction in extra:
        inputs.append(dict(input=index, side=side, type=kind, position=name,
                           label=("왼쪽" if side == 0 else "오른쪽") + f" {name}",
                           source_ref=ref, direction=direction, supports_rt=False))
    return dict(inputs=sorted(inputs, key=lambda item: item["input"]),
                hall_count=14, input_count=28, sides={"0": "left", "1": "right"},
                note="Position names follow web/layout.mjs, not current HID mappings. Right side positions are mirrored. Encoder CW/CCW is electrical decoding; verify desired physical direction on your device.")


def binding_catalog():
    keys = [{"code": 0, "name": "Modifiers only"}]
    keys.extend({"code": 4 + i, "name": chr(65 + i)} for i in range(26))
    keys.extend({"code": 30 + i, "name": str((i + 1) % 10)} for i in range(10))
    names = [(0x28, "Enter"), (0x29, "Escape"), (0x2a, "Backspace"), (0x2b, "Tab"), (0x2c, "Space"),
             (0x2d, "- / _"), (0x2e, "= / +"), (0x2f, "[ / {"), (0x30, "] / }"),
             (0x31, "Backslash / pipe"), (0x32, "Non-US #"), (0x33, "; / :"),
             (0x34, "Quote"), (0x35, "Grave / tilde"), (0x36, ", / <"), (0x37, ". / >"),
             (0x38, "/ / ?"), (0x39, "Caps Lock"), (0x46, "Print Screen"), (0x47, "Scroll Lock"),
             (0x48, "Pause"), (0x49, "Insert"), (0x4a, "Home"), (0x4b, "Page Up"),
             (0x4c, "Delete"), (0x4d, "End"), (0x4e, "Page Down"), (0x4f, "Right"),
             (0x50, "Left"), (0x51, "Down"), (0x52, "Up"), (0x64, "Non-US backslash"),
             (0x65, "Application/Menu"), (0x90, "한/영 (LANG1)"), (0x91, "한자 (LANG2)"),
             (0x53, "Num Lock"), (0x54, "Num /"), (0x55, "Num *"), (0x56, "Num -"),
             (0x57, "Num +"), (0x58, "Num Enter"), (0x62, "Num 0"), (0x63, "Num ."),
             (0x67, "Num ="), (0x85, "Num ,"),
             (0x87, "International1 / ろ"), (0x88, "International2 / かな"),
             (0x89, "International3 / Yen"), (0x8a, "International4 / 変換"),
             (0x8b, "International5 / 無変換"), (0x8c, "International6 / Num ,"),
             (0x8d, "International7"), (0x8e, "International8"), (0x8f, "International9"),
             (0x92, "カタカナ (LANG3)"), (0x93, "ひらがな (LANG4)"), (0x94, "全角/半角 (LANG5)")]
    keys.extend({"code": code, "name": name} for code, name in names)
    keys.extend({"code": 0x59 + i, "name": f"Num {i + 1}"} for i in range(9))
    keys.extend({"code": 0x3a + i, "name": f"F{i + 1}"} for i in range(12))
    keys.extend({"code": 0x68 + i, "name": f"F{i + 13}"} for i in range(12))
    modifier_names = ("L Ctrl", "L Shift", "L Alt", "L Win/Cmd", "R Ctrl", "R Shift", "R Alt", "R Win/Cmd")
    keys.extend({"code": 0xe0 + i, "name": name} for i, name in enumerate(modifier_names))
    return dict(
        kinds={"0": "disabled", "1": "keyboard", "2": "vertical_scroll", "3": "horizontal_scroll",
               "4": "consumer", "5": "mouse_button", "6": "keyboard_chord"},
        keyboard_page=7, named_keyboard_usages=sorted(keys, key=lambda item: item["code"]),
        accepted_keyboard_usage_ranges=[[0, 0], [4, 231]],
        keyboard_chord={"capability": CAP_KEY_CHORD, "ordinary_usage_range": [4, 223],
                        "ordinary_key_count": [2, 3], "keys_order": "distinct ascending"},
        modifiers=[{"bit": 1 << i, "name": name} for i, name in enumerate(modifier_names)],
        media=[{"code": code, "name": name} for code, name in ((0xb5, "Next track"), (0xb6, "Previous track"),
               (0xb7, "Stop"), (0xcd, "Play/pause"), (0xe2, "Mute"), (0xe9, "Volume up"), (0xea, "Volume down"))],
        mouse_buttons=[{"code": i + 1, "name": name} for i, name in enumerate(("Left", "Right", "Middle", "Back", "Forward"))],
        scroll_codes={"1": "up/right", "-1": "down/left"},
        notes=["Keyboard usages are USB HID page 0x07, not characters or OS scan codes. OS language/layout affects resulting characters.",
               "Modifier bits are supported in kinds 1 and 6. Kind 6 takes a keys list instead of code. Disabled records require code=0,modifiers=0.",
               "Mouse buttons and Hall/MX press-edge scroll require firmware capability0x20. No text macro or arbitrary input injection is exposed."])


def _not_calibrating(session, client):
    if getattr(getattr(session, "calibration", None), "active", False):
        raise DeviceError("Finish or cancel the current batch calibration before changing other settings")
    for side in (0, 1):
        if client.status(side)["calibration_state"]:
            raise DeviceError("Finish or cancel the current individual calibration before changing other settings")


def key_options(record):
    return dict(key=record["key"], enabled=bool(record["flags"] & 1),
                rapid_trigger=bool(record["flags"] & 2),
                retrigger_before_release=bool(record["flags"] & 8),
                retrigger_active=bool(record["flags"] & 15 == 15))


def register_configuration_tools(server, session, read, write, guard):
    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_key_options(keys: list[StrictInt] | None = None) -> dict[str, Any]:
        """Read named Hall flags, including retrigger_before_release. Omitted keys selects all14. Feature support comes from installed firmware; active requires enabled+calibrated+RT. Raw profiles keep their unchanged flags field."""
        ids = list(range(14)) if keys is None else selected_keys(keys)
        client = session.require_client()
        profile, info = client.get_config(), client.info()
        return dict(keys=[key_options(profile["keys"][key]) for key in ids],
                    retrigger_supported=bool(info["capabilities"] & CAP_RETRIGGER_BEFORE_RELEASE),
                    retrigger_default=False,
                    note="Synthetic USB release/press interrupts long notes. Option is inactive while RT is off. STATUS snapshots do not expose the USB pulse.")

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_curve_catalog() -> dict[str, Any]:
        """List linear and approximate Owlab Ti HE nonlinear profiles, stroke scaling, provenance and firmware requirements. No device access or changes."""
        return curve_catalog()

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_layout() -> dict[str, Any]:
        """List the exact 28 input IDs/physical positions and the 14 Hall RT-capable keys. Does not need a connected device."""
        return layout()

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_binding_catalog() -> dict[str, Any]:
        """List keyboard HID usage names/ranges, modifier bits, mouse, scroll and media binding codes. No device required."""
        return binding_catalog()

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_set_travel_mm(keys: list[StrictInt], total_travel_mm: StrictFloat | StrictInt) -> dict[str, Any]:
        """Set total switch stroke for selected Hall keys in this MCP session; 0.10..10.00 mm in0.01 increments. This is UI metadata, not a hardware calibration or Flash write. Export a Studio profile to retain it."""
        client = session.require_client()
        _write_permission(client)
        _not_calibrating(session, client)
        ids = selected_keys(keys)
        value = float(_millimetres(total_travel_mm, "total_travel_mm"))
        travel = validate_travel(session.travel_mm)
        for key in ids:
            travel[key] = value
        session.travel_mm = validate_travel(travel)
        return dict(total_travel_mm=list(session.travel_mm), changed_keys=ids, device_written=False,
                    persisted=False, scope="MCP session metadata; export/import a Studio profile to share with the GUI")

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_preview_keys(keys: list[StrictInt], patch: dict, unit: str = "normalized") -> dict[str, Any]:
        """Preview a partial Hall-settings patch without writes. unit normalized(0..1000 integers) or mm(0.01 increments using session stroke). Return fingerprint for optimistic apply. Valid patch fields: actuation,rt_press,rt_release,top_deadzone,bottom_deadzone,noise_deadband,enabled,rapid_trigger,retrigger_before_release,curve_id(0 linear,1 approximate Owlab Ti HE). Nonzero curve requires capability0x200. retrigger_before_release is a strict boolean, needs capability0x40000 and sends release/press pulses while RT remains held, breaking long notes. See keyboard_curve_catalog."""
        client = session.require_client()
        original = client.get_config()
        info = client.info()
        proposed = propose_keys(original, keys, patch, unit=unit, travel_mm=session.travel_mm, capabilities=info["capabilities"])
        return dict(fingerprint=profile_fingerprint(original), proposed_keys=proposed,
                    proposed_options=[key_options(record) for record in proposed],
                    effective_mm=[{"key": key["key"], **{field: key[field] * session.travel_mm[key["key"]] / 1000 for field in DISTANCES}} for key in proposed],
                    device_written=False, note="Only patched fields change; measured endpoints and mappings stay per key. Curve changes alter RT/AP distance behavior without automatically changing numeric thresholds; mm is a travel-scaled estimate.")

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_set_keys(keys: list[StrictInt], patch: dict, unit: str = "normalized", expected_fingerprint: str | None = None) -> dict[str, Any]:
        """Apply a partial patch to selected Hall keys, preserving calibration and unedited fields. mm uses per-key session total travel. Preflight all keys/capabilities, disable output, compare full RAM readback. No automatic SAVE or enable; partial failure is reported. Optional fingerprint comes from preview."""
        client = session.require_client()
        _write_permission(client)
        _not_calibrating(session, client)
        original = client.get_config()
        _expect(original, expected_fingerprint)
        info = client.info()
        proposed = propose_keys(original, keys, patch, unit=unit, travel_mm=session.travel_mm, capabilities=info["capabilities"])
        return apply_records(client, original, keys=proposed, expected_fingerprint=expected_fingerprint)

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_set_bindings(bindings: list[dict], expected_fingerprint: str | None = None) -> dict[str, Any]:
        """Apply 1..28 distinct full bindings. Kind 6 uses {input,kind,keys,modifiers}; other kinds use code instead of keys. Validate all capabilities and verify full readback. RAM only; output off."""
        client = session.require_client()
        _write_permission(client)
        _not_calibrating(session, client)
        original = client.get_config()
        _expect(original, expected_fingerprint)
        proposed = propose_bindings(bindings, client.info()["capabilities"])
        return apply_records(client, original, bindings=proposed, expected_fingerprint=expected_fingerprint)

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_export_studio_profile() -> dict[str, Any]:
        """Export exact GUI-compatible split-studio-profile-v1 JSON with firmware config and per-key totalTravelMm. No file or Flash writes."""
        return make_studio_profile(session.require_client().get_config(), session.travel_mm)

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_import_studio_profile(profile: dict, expected_fingerprint: str | None = None) -> dict[str, Any]:
        """Import GUI split-studio-profile-v1 or raw c77-profile-v1 JSON. Preflight entire config/capabilities before OUTPUT or writes; only on complete verified apply commit UI metadata to session. RAM only, no SAVE or enable."""
        client = session.require_client()
        _write_permission(client)
        target, travel = parse_studio_profile(profile)
        _not_calibrating(session, client)
        original = client.get_config()
        outcome = apply_records(client, original, keys=target["keys"], bindings=target["bindings"],
                                expected_fingerprint=expected_fingerprint, allow_calibration=True)
        if outcome["complete"] and travel is not None:
            session.travel_mm = travel
        return {**outcome, "ui_metadata_applied": bool(outcome["complete"] and travel is not None),
                "total_travel_mm": list(session.travel_mm)}

    @server.tool(annotations=write, structured_output=True)
    @guard
    def keyboard_defaults(expected_fingerprint: str | None = None) -> dict[str, Any]:
        """Explicitly restore firmware defaults in RAM, including removal of calibration and mappings. Flash unchanged until a separate SAVE. Reads back the result; never retries an uncertain command. Export before use if restoration may be needed."""
        client = session.require_client()
        _write_permission(client)
        _not_calibrating(session, client)
        before = client.get_config()
        _expect(before, expected_fingerprint)
        if client.output(False) != {"output_enabled": False}:
            raise DeviceError("Output OFF acknowledgement mismatch")
        if client.get_config() != before:
            raise DeviceError("Profile changed before defaults; reset was not sent")
        acknowledged = False
        failure = None
        try:
            if client.request(DEFAULTS):
                raise DeviceError("Defaults acknowledgement was malformed")
            acknowledged = True
        except (ValueError, DeviceError, PermissionError, OSError, TimeoutError) as exc:
            failure = str(exc)
        try:
            actual = validate_profile(client.get_config())
            status = client.status(0)
        except (ValueError, DeviceError, PermissionError, OSError, TimeoutError) as exc:
            return dict(reset_ram=None, readback_verified=False, profile=None,
                        command_acknowledged=acknowledged, persisted=False, ui_metadata_changed=False,
                        error=f"{failure or ''} RAM reset outcome uncertain: {exc}. No retry or SAVE was sent.")
        reset = acknowledged and not status["output_enabled"] and not any(
            key["flags"] & 4 or key["released"] or key["pressed"] for key in actual["keys"])
        return dict(reset_ram=True if reset else None, readback_verified=reset,
                    profile=actual, fingerprint=profile_fingerprint(actual), command_acknowledged=acknowledged,
                    persisted=False, output_enabled=status["output_enabled"], ui_metadata_changed=False,
                    error=None if reset else f"{failure or 'Defaults readback did not confirm calibration/output reset'}. RAM reset outcome uncertain; no retry or SAVE was sent.")
