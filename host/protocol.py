"""Strict protocol v1 codecs; only stdlib. See ../PROTOCOL.md."""
from __future__ import annotations

import struct

VID, PID, USAGE_PAGE, USAGE = 0x0483, 0x5744, 0xFF60, 0x61
CAP_MOUSE_BUTTON = 0x20
CAP_BOTTOM_DEADZONE = 0x100
CAP_HALL_CURVE = 0x200
CAP_ADC_TRACE = 0x400
CAP_CPU_PROFILE = 0x800
CAP_ADC_IRQ_PROFILE = 0x1000
CAP_KEY_CHORD = 0x2000
CAP_SAVE_OUTPUT = 0x4000
INFO, STATUS, GET_KEY, SET_KEY, CALIBRATE, OUTPUT, SAVE, DEFAULTS, GET_BINDING, SET_BINDING = range(1, 11)
MUTATIONS = {SET_KEY, CALIBRATE, OUTPUT, SAVE, DEFAULTS, SET_BINDING}
STATUSES = ["OK", "BAD_COMMAND", "BAD_PACKET", "BAD_INDEX", "BAD_CONFIG", "BUSY", "CALIBRATION_INVALID", "NOT_READY", "STORAGE_ERROR"]
KEY_FIELDS = ("key", "flags", "released", "pressed", "actuation", "rt_press", "rt_release", "top_deadzone", "noise_deadband", "bottom_deadzone", "curve_id")
BIND_FIELDS = ("input", "kind", "code", "modifiers")
CHORD_BIND_FIELDS = ("input", "kind", "keys", "modifiers")
CONSUMER = {0xB5, 0xB6, 0xB7, 0xCD, 0xE2, 0xE9, 0xEA}


class ProtocolError(ValueError):
    pass


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ProtocolError(f"{name}: integer {low}..{high} required")
    return value


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


def packet(op, sequence, payload=b"", status=0):
    integer(op, 1, 255, "opcode")
    integer(sequence, 1, 65535, "sequence")
    integer(status, 0, 255, "status")
    if len(payload) > 54:
        raise ProtocolError("payload >54")
    data = bytearray(64)
    struct.pack_into("<2sBBHBB", data, 0, b"C7", 1, op, sequence, status, len(payload))
    data[8:8+len(payload)] = payload
    struct.pack_into("<H", data, 62, crc16(data[:62]))
    return bytes(data)


def unpack_packet(data):
    if len(data) != 64:
        raise ProtocolError("report must be 64 bytes")
    magic, version, op, seq, status, length = struct.unpack_from("<2sBBHBB", data)
    if magic != b"C7" or version != 1 or not seq or length > 54:
        raise ProtocolError("invalid report header")
    if crc16(data[:62]) != struct.unpack_from("<H", data, 62)[0]:
        raise ProtocolError("CRC mismatch")
    if any(data[8+length:62]):
        raise ProtocolError("nonzero padding")
    return op, seq, status, bytes(data[8:8+length])


def key_record(value):
    required = set(KEY_FIELDS) - {"bottom_deadzone", "curve_id"}
    if not isinstance(value, dict) or not required <= set(value) <= set(KEY_FIELDS):
        raise ProtocolError("key record fields do not match schema")
    d = {k: integer(value.get(k, 0), 0, 65535, k) for k in KEY_FIELDS}
    integer(d["key"], 0, 13, "key")
    integer(d["flags"], 0, 7, "flags")
    for k in ("released", "pressed"):
        integer(d[k], 0, 4095, k)
    for k in ("actuation", "rt_press", "rt_release"):
        integer(d[k], 1, 1000, k)
    integer(d["top_deadzone"], 0, d["actuation"]-1, "top_deadzone")
    integer(d["bottom_deadzone"], 0, 1000-d["actuation"], "bottom_deadzone")
    integer(d["noise_deadband"], 0, 100, "noise_deadband")
    integer(d["curve_id"], 0, 1, "curve_id")
    if d["flags"] & 4 and abs(d["pressed"]-d["released"]) < 128:
        raise ProtocolError("calibrated span must be >=128")
    return d


def encode_key(value):
    d = key_record(value)
    return struct.pack("<BB10H", *(d[k] for k in KEY_FIELDS), 0)


def decode_key(data):
    if len(data) != 22:
        raise ProtocolError("key reply length")
    v = struct.unpack("<BB10H", data)
    if v[-1]:
        raise ProtocolError("key reserved bytes")
    return key_record(dict(zip(KEY_FIELDS, v)))


def require_key_capability(key, capabilities):
    if key.get("bottom_deadzone", 0) and not capabilities & CAP_BOTTOM_DEADZONE:
        raise ProtocolError("Bottom deadzone requires firmware 0.3.4+; no device mutation performed")
    if key.get("curve_id", 0) and not capabilities & CAP_HALL_CURVE:
        raise ProtocolError("Nonlinear Hall curve requires firmware 0.3.5+ capability 0x200; no device mutation performed")


def binding_record(value):
    if not isinstance(value, dict):
        raise ProtocolError("binding fields do not match schema")
    kind = integer(value.get("kind"), 0, 6, "kind")
    fields = CHORD_BIND_FIELDS if kind == 6 else BIND_FIELDS
    if set(value) != set(fields):
        raise ProtocolError("binding fields do not match schema")
    d = dict(value)
    integer(d["input"], 0, 27, "input")
    integer(d["modifiers"], 0, 255, "modifiers")
    if kind == 6:
        if not isinstance(d["keys"], list) or not 2 <= len(d["keys"]) <= 3:
            raise ProtocolError("keyboard chord requires 2 or 3 ordinary usages")
        keys = [integer(key, 4, 0xDF, "keyboard usage") for key in d["keys"]]
        if any(a >= b for a, b in zip(keys, keys[1:])):
            raise ProtocolError("keyboard chord usages must be distinct and ascending")
        d["keys"] = keys
        return d
    code = integer(d["code"], -1, 65535, "code")
    if kind == 0 and (code or d["modifiers"]):
        raise ProtocolError("disabled binding must have zero code/modifiers")
    if kind == 1 and not (code == 0 or 4 <= code <= 0xDF or 0xE0 <= code <= 0xE7):
        raise ProtocolError("invalid keyboard usage")
    if kind in (2, 3) and code not in (-1, 1):
        raise ProtocolError("scroll code must be +/-1")
    if kind == 4 and code not in CONSUMER:
        raise ProtocolError("unsupported consumer usage")
    if kind == 5 and not 1 <= code <= 5:
        raise ProtocolError("mouse button must be 1..5 (left/right/middle/back/forward)")
    if kind != 1 and d["modifiers"]:
        raise ProtocolError("modifiers only allowed for keyboard")
    return d


def require_binding_capability(binding, capabilities):
    if binding["kind"] == 6 and not capabilities & CAP_KEY_CHORD:
        raise ProtocolError("Keyboard chords require C86 firmware capability 0x2000")
    extended = binding["kind"] == 5 or (binding["kind"] in (2, 3) and binding["input"] < 20)
    if extended and not capabilities & CAP_MOUSE_BUTTON:
        raise ProtocolError("This binding requires firmware 0.2+; no device mutation performed")


def encode_binding(value):
    d = binding_record(value)
    if d["kind"] == 6:
        keys = d["keys"]
        return bytes((d["input"], d["kind"], keys[0], keys[1], d["modifiers"], keys[2] if len(keys) == 3 else 0))
    return struct.pack("<BBHBB", d["input"], d["kind"], d["code"] & 0xFFFF, d["modifiers"], 0)


def decode_binding(data):
    if len(data) != 6:
        raise ProtocolError("binding reply length")
    if data[1] == 6:
        inp, kind, first, second, mod, third = data
        keys = [first, second, third] if third else [first, second]
        return binding_record(dict(input=inp, kind=kind, keys=keys, modifiers=mod))
    inp, kind, code, mod, reserved = struct.unpack("<BBHBB", data)
    if reserved:
        raise ProtocolError("binding reserved byte")
    if kind in (2, 3) and code == 65535:
        code = -1
    return binding_record(dict(input=inp, kind=kind, code=code, modifiers=mod))


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != {"schema", "board", "keys", "bindings"}:
        raise ProtocolError("profile fields must be schema, board, keys, bindings")
    if profile["schema"] != "c77-profile-v1" or profile["board"] != "C77":
        raise ProtocolError("unsupported profile version/board")
    if not isinstance(profile["keys"], list) or len(profile["keys"]) != 14:
        raise ProtocolError("exactly 14 Hall keys required")
    if not isinstance(profile["bindings"], list) or len(profile["bindings"]) != 28:
        raise ProtocolError("exactly 28 bindings required")
    keys = [key_record(k) for k in profile["keys"]]
    bindings = [binding_record(b) for b in profile["bindings"]]
    if sorted(k["key"] for k in keys) != list(range(14)) or sorted(b["input"] for b in bindings) != list(range(28)):
        raise ProtocolError("duplicate or missing indices")
    return dict(schema="c77-profile-v1", board="C77", keys=sorted(keys, key=lambda k:k["key"]), bindings=sorted(bindings, key=lambda b:b["input"]))


def decode_info(p):
    if len(p) < 34 or any(p[34:]):
        raise ProtocolError("invalid info reply")
    fields = struct.unpack_from("<8B5IH4B", p)
    names = ("major", "minor", "patch", "role", "hall", "digital", "encoders", "inputs", "scan_target_hz", "usb_interval_us", "capabilities", "generation", "config_crc", "flash_kib", "board_rev", "protocol", "usb_speed", "storage_valid")
    d = dict(zip(names, fields))
    if (d["hall"] != 14 or d["digital"] != 6 or d["encoders"] != 4 or d["inputs"] != 28
            or d["board_rev"] not in (77, 85, 86) or d["protocol"] != 1 or d["role"] != 0):
        raise ProtocolError("incompatible board/role/protocol")
    return d


def decode_status(p):
    if len(p) != 54:
        raise ProtocolError("status reply length")
    up, seq, errors, age = struct.unpack_from("<4I", p)
    raw = list(struct.unpack_from("<7H", p, 16))
    travel = list(struct.unpack_from("<7H", p, 30))
    digital, flags, key, state = struct.unpack_from("<4B", p, 44)
    pressed, reserved = struct.unpack_from("<IH", p, 48)
    if reserved or any(v > 4095 for v in raw) or any(v > 1000 for v in travel):
        raise ProtocolError("invalid telemetry")
    return dict(uptime_ms=up, sample_sequence=seq, errors=errors, link_age_us=age, raw=raw, travel=travel, digital_mask=digital, output_enabled=bool(flags&1), fresh=bool(flags&2), dirty=bool(flags&4), stored_valid=bool(flags&8), calibration_key=key, calibration_state=state, pressed_mask=pressed)
