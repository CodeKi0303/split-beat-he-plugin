"""Explicit C86 app update over vendor HID; never flash on import or file inspection.

python -m host.update_firmware --package build/...fw.json
python -m host.update_firmware --package ... --serial SERIAL --target left --write --full-readback
Old boot sectors must first be installed with SWD on both boards. Settings are not saved here.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import secrets
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .protocol import packet, unpack_packet, ProtocolError, STATUSES
from .client import DeviceError

INFO, ENTER, BEGIN, WRITE, COMMIT, RUN, READ = range(0x30, 0x37)
APP_BASE, MAX_IMAGE = 0x08020000, 0x3FFE0
TOKEN = b"UPD3"


@dataclass(frozen=True)
class FirmwarePackage:
    manifest: dict
    image: bytes
    role: int


def _u32(n, label):
    if type(n) is not int or not 0 <= n <= 0xFFFFFFFF:
        raise ValueError(f"Invalid {label}")
    return n


def validate_package(source) -> FirmwarePackage:
    m = json.loads(source) if isinstance(source, (str, bytes)) else json.loads(json.dumps(source))
    if not isinstance(m, dict) or m.get("schema") != "c86-firmware-v1" or m.get("hardware") != 86 or m.get("role") not in ("left", "right"):
        raise ValueError("Expected a C86 left/right firmware package")
    size = m.get("image_size")
    if m.get("image_base") != APP_BASE or type(size) is not int or not 0x420 <= size <= MAX_IMAGE or size % 32:
        raise ValueError("Invalid app address/size/alignment")
    _u32(m.get("version_number"), "version_number")
    _u32(m.get("crc32"), "crc32")
    if not m["version_number"]:
        raise ValueError("Firmware version identity cannot be zero")
    if not isinstance(m.get("version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", m["version"]):
        raise ValueError("Invalid displayed version")
    encoded = m.get("image_base64")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE + 2) // 3) * 4:
        raise ValueError("Invalid base64 image")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid base64 image") from exc
    if len(image) != size or binascii.crc32(image) != m["crc32"]:
        raise ValueError("Image length or CRC32 mismatch")
    if not isinstance(m.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", m["sha256"]) or hashlib.sha256(image).hexdigest() != m["sha256"].lower():
        raise ValueError("Image SHA256 mismatch")
    role = int(m["role"] == "right")
    identity = struct.unpack_from("<8sBBH5I", image, 0x400)
    expected = (b"C86FW003", 86, role, 1, m["version_number"], APP_BASE, MAX_IMAGE, 0x33444946, 0xCCBBB6B9)
    if identity != expected:
        raise ValueError("Embedded hardware/role/layout/image identity mismatch")
    return FirmwarePackage(m, image, role)


def decode_info(data):
    if len(data) != 48:
        raise ValueError("Invalid update INFO length")
    protocol, hardware, role, mode, flash, flags, base, maximum, size, crc, version, session, offset, uid = struct.unpack("<4B2H7I12s", data)
    if protocol != 1 or hardware != 86 or role > 1 or mode > 1 or base != APP_BASE or maximum != MAX_IMAGE:
        raise ValueError("Incompatible update protocol/hardware/layout")
    return dict(hardware=hardware, role=role, mode=mode, flash_kib=flash, flags=flags, base=base, max=maximum, size=size, crc=crc, version=version, session=session, next_offset=offset, uid=uid, uid_hex=uid.hex())


class UpdateStatusError(RuntimeError):
    def __init__(self, status, op):
        self.status = status
        message = f"{STATUSES[status] if status < len(STATUSES) else status} (0x{op:02x})"
        if status == 1:
            message += ": old firmware has no USB updater; install the new boot sector with SWD on both boards first"
        super().__init__(message)


class FirmwareUpdater:
    """No retries of BEGIN/WRITE/COMMIT. One owner and one in-flight transaction."""
    def __init__(self, transport, *, write_enabled=False, progress=None, delay=time.sleep, clock=time.monotonic):
        self.transport, self.write_enabled = transport, write_enabled
        self.progress = progress or (lambda message, fraction: None)
        self.delay, self.clock, self.sequence, self.running = delay, clock, 0, False

    def request(self, op, payload=b"", target=0, timeout=3.0):
        # Right INFO also enters relay maintenance and turns key output off.
        if (op != INFO or target) and not self.write_enabled:
            raise PermissionError("Explicit --write (or --exit-maintenance) required")
        wire_op = op + (0x20 if target else 0)
        self.sequence = self.sequence % 65535 + 1
        seq = self.sequence
        self.transport.write(packet(wire_op, seq, payload))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.transport.read(min(100, max(1, int((deadline - time.monotonic()) * 1000))))
            if not data:
                continue
            try:
                reply_op, reply_seq, status, result = unpack_packet(data)
            except ProtocolError:
                continue
            if reply_seq != seq or reply_op != wire_op | 0x80:
                continue
            if status:
                raise UpdateStatusError(status, wire_op)
            return result
        raise TimeoutError(f"Update 0x{wire_op:02x} timed out; no automatic retry")

    def info(self, target=0, *, timeout=3.0):
        return decode_info(self.request(INFO, target=target, timeout=timeout))

    @staticmethod
    def check_target(info, role, uid=None):
        if info["role"] != role or info["hardware"] != 86 or info["flash_kib"] != 1024 or not info["flags"] & 1 or uid is not None and info["uid"] != uid:
            raise ValueError("Target role/UID/bootloader/Flash mismatch; stopped")

    def wait_mode(self, target, mode, uid, *, reconnect=False):
        last = None
        deadline = self.clock() + 20.0
        for _ in range(40):
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            try:
                if reconnect and not target:
                    # Enumeration may briefly reopen the departing app handle.
                    # A failed INFO below retries only this open/read sequence.
                    self.transport.reconnect(timeout=min(1.0, remaining))
                    reconnect = False
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                info = self.info(target, timeout=min(3.0 if target else 1.0, remaining))
                self.check_target(info, target, uid)
                if info["mode"] == mode:
                    return info
                last = "device is still in the previous mode"
                reconnect = not target
            except UpdateStatusError as exc:
                if exc.status not in (5, 7):
                    raise
                last = exc
            except (OSError, TimeoutError, DeviceError) as exc:
                last = exc
                reconnect = not target
            self.delay(min(.25, max(0.0, deadline - self.clock())))
        raise RuntimeError(f"Mode transition not confirmed: {last}")

    def change_mode(self, op, target, uid, mode):
        try:
            self.request(op, TOKEN, target)
        except UpdateStatusError:
            raise
        except (OSError, TimeoutError, DeviceError):
            if target:
                raise
            # Local USB reset can lose the response. Reconnect/read only, never repeat.
        return self.wait_mode(target, mode, uid, reconnect=not target)

    def leave_maintenance(self):
        if self.running:
            raise RuntimeError("Cannot leave while writing")
        info = self.info(0)
        self.check_target(info, 0)
        if not info["flags"] & 2:
            raise RuntimeError("Left has no valid app; record a valid left package first")
        return self.request(RUN, TOKEN)

    def update(self, source, *, target, full_readback=False, session=None):
        if not self.write_enabled:
            raise PermissionError("Explicit --write required; inspection never writes")
        if self.running:
            raise RuntimeError("Update already in progress")
        self.running = True
        try:
            pkg = validate_package(source)
            m, image = pkg.manifest, pkg.image
            if target != pkg.role:
                raise ValueError("Selected side and package role differ")
            self.progress("Package SHA256/CRC/identity verified", 0)
            local = self.info(0)
            self.check_target(local, 0)
            before = self.info(1) if target else local
            self.check_target(before, target)
            uid = before["uid"]
            if before["mode"] == 1:
                before = self.change_mode(ENTER, target, uid, 0)
            self.check_target(before, target, uid)
            if before["mode"] != 0:
                raise ValueError("Not in bootloader mode")
            if before["session"]:
                raise RuntimeError("Previous transfer session remains. Fully remove all board power, reconnect, then explicitly start again; no automatic erase/resume")
            session = (secrets.randbits(32) or 1) if session is None else _u32(session, "session")
            if not session:
                raise ValueError("Session cannot be zero")
            begin = struct.pack("<12sBBH4I", uid, 86, target, 0, session, m["image_size"], m["crc32"], m["version_number"])
            self.progress("Erasing app; keep power connected", 0)
            self.request(BEGIN, begin, target, 15)
            for offset in range(0, len(image), 32):
                self.request(WRITE, struct.pack("<2I", session, offset) + image[offset:offset + 32], target)
                self.progress("Writing app", (offset + 32) / len(image) * .8)
            pending = self.info(target)
            self.check_target(pending, target, uid)
            if pending["mode"] != 0 or pending["session"] != session or pending["next_offset"] != len(image):
                raise ValueError("Recorded offset/session mismatch; COMMIT not sent")
            self.request(COMMIT, struct.pack("<I", session), target)
            committed = self.info(target)
            self.check_target(committed, target, uid)
            if not committed["flags"] & 2 or committed["mode"] != 0 or (committed["size"], committed["crc"], committed["version"]) != (len(image), m["crc32"], m["version_number"]):
                raise ValueError("Device CRC/commit verification failed; RUN not sent")
            if full_readback:
                for offset in range(0, len(image), 48):
                    expected = image[offset:offset + 48]
                    actual = self.request(READ, struct.pack("<IB", offset, len(expected)), target)
                    if actual != expected:
                        raise ValueError(f"Flash readback mismatch at {offset}; RUN not sent")
                    self.progress("Reading back complete app", .8 + (offset + len(expected)) / len(image) * .18)
            self.progress("Starting verified app", .99)
            after = self.change_mode(RUN, target, uid, 1)
            if not after["flags"] & 2 or (after["size"], after["crc"], after["version"]) != (len(image), m["crc32"], m["version_number"]):
                raise ValueError("New app version/CRC confirmation failed")
            if target:
                self.request(RUN, TOKEN)  # Resume left scan/link; never enables key output.
            self.progress("Update verified; key output remains disabled", 1)
            return after
        finally:
            self.running = False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package", type=Path, help=".fw.json file; inspection only unless --write")
    ap.add_argument("--serial", help="Explicit left vendor HID serial")
    ap.add_argument("--target", choices=("left", "right"))
    ap.add_argument("--write", action="store_true", help="Erase/rewrite selected app; power must remain connected")
    ap.add_argument("--full-readback", action="store_true")
    ap.add_argument("--exit-maintenance", action="store_true", help="Explicit local RUN to leave relay/update mode")
    args = ap.parse_args(argv)
    if args.exit_maintenance and (args.write or args.package):
        ap.error("--exit-maintenance cannot be combined with --write or --package")
    pkg = None
    if args.package:
        if args.package.stat().st_size > 450000:
            ap.error("Package file too large")
        pkg = validate_package(args.package.read_text(encoding="utf-8"))
        print(f"Verified package: C86 {pkg.manifest['role']} v{pkg.manifest['version']}; {len(pkg.image)} bytes; SHA256 {pkg.manifest['sha256']}")
    if not (args.write or args.exit_maintenance):
        if not pkg:
            ap.error("Provide --package to inspect, or explicit --exit-maintenance")
        print("Inspection only. No device opened and no flash written.")
        return 0
    if not args.serial or args.write and (not pkg or not args.target):
        ap.error("Writes require --serial, --target and --package; exit requires --serial")
    if args.write and int(args.target == "right") != pkg.role:
        ap.error("Selected side and package role differ")
    print("Close Studio/MCP/other HID clients first. RAM settings must be saved beforehand; this tool does not SAVE. Keep both boards powered.")
    from .client import HidTransport
    transport = HidTransport(args.serial)
    last_percent = [-1]
    def progress(message, fraction):
        percent = int(fraction * 100)
        if percent != last_percent[0]:
            print(f"{percent:3d}% {message}", flush=True)
            last_percent[0] = percent
    try:
        updater = FirmwareUpdater(transport, write_enabled=True, progress=progress)
        if args.exit_maintenance:
            updater.leave_maintenance()
            print("Exit requested. Key output is not enabled automatically.")
        else:
            updater.update(pkg.manifest, target=pkg.role, full_readback=args.full_readback)
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Stopped: {exc}. No automatic retry. If transfer began, fully power-cycle all boards before another explicit attempt.", file=sys.stderr)
        raise SystemExit(1)
