"""Serialized USB HID transactions and explicit RAM/persistent operations."""
from __future__ import annotations

import contextlib
import struct
import threading
import time
import hashlib
import os
import tempfile

from .protocol import *


class DeviceError(RuntimeError):
    pass


class DeviceLock:
    """Prevent two instances of this Python host owning the same serial."""
    def __init__(self, serial):
        path = os.path.join(tempfile.gettempdir(), 'c77-'+hashlib.sha256(serial.encode()).hexdigest()[:24]+'.lock')
        self.file = open(path, 'a+b')
        self.file.seek(0)
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise DeviceError('Another C77 MCP/Python host owns this serial') from exc

    def close(self):
        self.file.close()


class HidTransport:
    """Open ONLY one serial-selected vendor collection. Never a keyboard collection."""
    demo = False

    def __init__(self, serial):
        if not serial or not serial.strip():
            raise DeviceError("real device requires an explicit serial selector")
        try:
            import hid
        except ImportError as exc:
            raise DeviceError("Install host/requirements-hid.txt in the project venv") from exc
        self.hid = hid
        self.serial = serial
        self.device = None
        self.owner = None
        self.open()

    @classmethod
    def enumerate(cls):
        import hid
        return [{k:d.get(k) for k in ("serial_number", "product_string", "usage_page", "usage")} for d in hid.enumerate(VID, PID) if d.get("usage_page") == USAGE_PAGE and d.get("usage") == USAGE]

    def open(self):
        if self.owner:
            raise DeviceError('HID is already owned')
        candidates = [d for d in self.hid.enumerate(VID, PID) if d.get("serial_number") == self.serial and d.get("usage_page") == USAGE_PAGE and d.get("usage") == USAGE]
        if len(candidates) != 1:
            raise DeviceError(f"Expected one vendor HID for this serial; found {len(candidates)}")
        self.owner = DeviceLock(self.serial)
        device = self.hid.device()
        try:
            device.open_path(candidates[0]["path"])
        except Exception:
            device.close()
            self.owner.close()
            self.owner = None
            raise
        self.device = device

    def write(self, data):
        if not self.device:
            raise DeviceError("device disconnected")
        try:
            written = self.device.write(b"\0"+data)  # hidapi requires report-ID byte, even ID=0.
            if written != 65:
                raise DeviceError("incomplete HID write")
        except Exception:
            self.close()
            raise

    def read(self, timeout_ms):
        if not self.device:
            raise DeviceError("device disconnected")
        try:
            return bytes(self.device.read(64, timeout_ms))
        except Exception:
            self.close()
            raise

    def close(self):
        if self.device:
            self.device.close()
        self.device = None
        if self.owner:
            self.owner.close()
            self.owner = None

    def reconnect(self, timeout=15):
        self.close()
        time.sleep(0.5)
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try:
                self.open()
                return
            except (OSError, DeviceError):
                time.sleep(0.25)
        raise DeviceError("SAVE reconnect timed out; persistence UNVERIFIED; do not retry SAVE automatically")


class KeyboardClient:
    def __init__(self, transport, write_enabled=False):
        self.transport = transport
        self.write_enabled = write_enabled
        self.sequence = 0
        self.lock = threading.RLock()
        self.output_enabled = False
        self.identified = False
        self.device_info = None

    def request(self, op, payload=b"", timeout=1.5):
        # The dedicated FirmwareUpdater owns update permission and sequencing.
        # Even right-side update INFO changes relay/output state, so unknown
        # opcodes must never bypass the normal configuration mutation gate.
        if type(op) is not int or not (1 <= op <= 10 or op in (0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46)):
            raise ProtocolError("Unsupported configuration opcode; firmware updates require the dedicated updater")
        if op in MUTATIONS and not self.write_enabled:
            raise PermissionError("read-only session: restart with --write-enabled for configuration changes")
        with self.lock:
            if op in MUTATIONS and not self.identified:
                self.info()
            if op == SET_BINDING:
                require_binding_capability(decode_binding(payload), self.device_info["capabilities"])
            if op == SET_KEY:
                require_key_capability(decode_key(payload), self.device_info["capabilities"])
            self.sequence = (self.sequence % 65535)+1
            seq = self.sequence
            try:
                self.transport.write(packet(op, seq, payload))
                deadline = time.monotonic()+timeout
                while time.monotonic() < deadline:
                    data = self.transport.read(min(100, max(1, int((deadline-time.monotonic())*1000))))
                    if not data:
                        continue
                    try:
                        reply_op, reply_seq, status, response = unpack_packet(data)
                    except ProtocolError:
                        continue
                    if reply_op != op | 0x80 or reply_seq != seq:
                        continue
                    if status:
                        raise DeviceError(STATUSES[status] if status < len(STATUSES) else f"device status {status}")
                    if op in MUTATIONS:
                        self.output_enabled = False
                    return response
                raise TimeoutError("HID transaction timed out; no automatic mutation retry")
            except Exception:
                self.output_enabled = False
                raise

    def info(self):
        result = decode_info(self.request(INFO))
        self.device_info = result
        self.identified = True
        return {**result, "demo": self.transport.demo, "write_enabled": self.write_enabled}

    def status(self, side=0):
        integer(side, 0, 1, "side")
        result = decode_status(self.request(STATUS, bytes([side])))
        self.output_enabled = result["output_enabled"]
        return {**result, "side": side, "demo": self.transport.demo}

    def get_config(self):
        with self.lock:
            keys = [decode_key(self.request(GET_KEY, bytes([i]))) for i in range(14)]
            bindings = [decode_binding(self.request(GET_BINDING, bytes([i]))) for i in range(28)]
            return validate_profile(dict(schema="c77-profile-v1", board="C77", keys=keys, bindings=bindings))

    def set_key(self, value):
        return decode_key(self.request(SET_KEY, encode_key(value)))

    def set_binding(self, value):
        return decode_binding(self.request(SET_BINDING, encode_binding(value)))

    def calibrate(self, key, action):
        integer(key, 0, 13, "key")
        actions = {"capture_rest":0, "capture_press":1, "commit":2, "cancel":3}
        if action not in actions:
            raise ProtocolError("action must be capture_rest/capture_press/commit/cancel")
        p = self.request(CALIBRATE, bytes([key, actions[action]]))
        if len(p) != 8:
            raise ProtocolError("calibration reply length")
        key, state, rest, pressed, count = struct.unpack("<BB3H", p)
        return dict(key=key, state=state, rest=rest, pressed=pressed, count=count, note="Capture is asynchronous: poll status until rest_ready=2 / press_ready=4; commit then explicit SAVE")

    def output(self, enabled):
        if type(enabled) is not bool:
            raise ProtocolError("enabled must be boolean")
        result = self.request(OUTPUT, bytes([enabled]))
        if len(result) != 1 or result[0] not in (0, 1):
            raise ProtocolError("output reply")
        self.output_enabled = bool(result[0])
        return {"output_enabled":self.output_enabled}

    def import_profile(self, value):
        """Validate everything BEFORE any device mutation. Not atomic on USB loss."""
        profile = validate_profile(value)
        with self.lock:
            info = self.info()
            for key in profile["keys"]:
                require_key_capability(key, info["capabilities"])
            for binding in profile["bindings"]:
                require_binding_capability(binding, info["capabilities"])
            self.output(False)
            changed = 0
            try:
                for key in profile["keys"]:
                    if self.set_key(key) != key:
                        raise DeviceError("key readback mismatch")
                    changed += 1
                for binding in profile["bindings"]:
                    if self.set_binding(binding) != binding:
                        raise DeviceError("binding readback mismatch")
                    changed += 1
                if self.get_config() != profile:
                    raise DeviceError("profile readback mismatch")
            except Exception as exc:
                raise DeviceError(f"Import stopped after {changed} records; RAM may be partial, output disabled; flash unchanged. {exc}") from exc
        return {"applied_ram": True, "persisted": False, "output_enabled":False, "records":42}

    def save(self):
        with self.lock:
            expected = self.get_config()
            before = self.info()
            expected_output = bool(before["capabilities"] & CAP_SAVE_OUTPUT) and self.status(0)["output_enabled"]
            response = self.request(SAVE)
            if len(response) != 4:
                raise ProtocolError("SAVE reply length")
            generation = struct.unpack("<I", response)[0]
            if generation != ((before["generation"]+1) & 0xFFFFFFFF or 1):
                raise DeviceError("unexpected SAVE generation; persistence UNVERIFIED")
            self.transport.reconnect()
            after = self.info()
            actual = self.get_config()
            if after["generation"] != generation or not after["storage_valid"] or actual != expected:
                raise DeviceError("SAVE accepted but persistent generation/profile readback FAILED")
            status = self.status(0)
            if expected_output and not status["output_enabled"]:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not status["output_enabled"]:
                    time.sleep(.1)
                    status = self.status(0)
            if status["output_enabled"] != expected_output or status["dirty"] or not status["stored_valid"]:
                raise DeviceError("SAVE readback did not verify the expected output and storage state")
            return dict(persisted=True, verified=True, generation=generation, config_crc=after["config_crc"], output_enabled=status["output_enabled"], demo=self.transport.demo)

    def close(self):
        # Do not mutate a device when closing a read-only diagnostic session.
        if self.write_enabled:
            with contextlib.suppress(Exception):
                self.output(False)
        self.transport.close()
        self.output_enabled = False
