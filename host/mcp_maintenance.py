"""Local package inspection and explicit application updates; no raw flash API."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from pydantic import StrictBool
from mcp.types import ToolAnnotations

from .client import DeviceError
from .update_firmware import FirmwareUpdater, validate_package


def read_package(package_path):
    path = Path(package_path).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 450000:
        raise ValueError("Expected a local .fw.json package no larger than 450000 bytes")
    data = path.read_bytes()
    if len(data) > 450000:
        raise ValueError("Package too large")
    package = validate_package(data)
    metadata = {key: value for key, value in package.manifest.items() if key != "image_base64"}
    metadata.update(package_path=str(path), package_file_sha256=hashlib.sha256(data).hexdigest(),
                    image_sha256=hashlib.sha256(package.image).hexdigest(), validated=True,
                    device_opened=False, flash_written=False)
    return package, metadata


def update_app(session, package_path, target, expected_sha256, confirm_update,
               verify_readback=True, *, updater_factory=FirmwareUpdater):
    if type(confirm_update) is not bool or not confirm_update:
        raise PermissionError("An explicit firmware-update request and confirm_update=true are required")
    if not session.firmware_write_enabled or not session.write_enabled:
        raise PermissionError("Restart server with --write-enabled --firmware-write-enabled to permit app updates")
    if type(verify_readback) is not bool:
        raise ValueError("verify_readback must be boolean")
    if target not in ("left", "right"):
        raise ValueError("target must be left or right")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("expected_sha256 must be the image_sha256 from inspection")
    package, metadata = read_package(package_path)
    if metadata["image_sha256"] != expected_sha256.lower() or package.manifest["role"] != target:
        raise ValueError("Package image SHA256 or target role differs; nothing written")
    session.require_idle()
    client = session.require_client()
    if client.transport.demo:
        raise ValueError("Demo transport does not emulate firmware flashing; inspect packages without updating")
    with client.lock:
        info = client.info()
        if info["board_rev"] != 86 or not info["capabilities"] & 0x40:
            raise ValueError("Requires C86 firmware and permanent USB/RS485 updater already installed")
        status = client.status(0)
        if status["dirty"] or status["calibration_state"]:
            raise ValueError("Unsaved RAM or active device calibration: explicitly save/cancel before updating; update never saves implicitly")
        if target == "right" and not client.status(1)["fresh"]:
            raise DeviceError("Right link is stale; update not started")
        before = client.get_config()
        if client.output(False) != {"output_enabled": False}:
            raise DeviceError("Output-off acknowledgement mismatch; update not started")
        settled = client.status(0)
        if settled["dirty"] or settled["calibration_state"] or client.get_config() != before:
            raise DeviceError("Settings changed during update preflight; no updater command sent. Close other clients and inspect/save explicitly")
        try:
            updater = updater_factory(client.transport, write_enabled=True)
            updated = updater.update(package.manifest, target=package.role, full_readback=verify_readback)
        except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
            raise DeviceError(f"Firmware update stopped: {exc}. No automatic retry, SAVE or output enable. Device may remain in maintenance; inspect/reconnect before an explicit recovery attempt.") from exc
        # Right RUN resumes the left application. Read-only reconnect attempts
        # may be needed here; never replay a flash/mode-change command.
        after, actual, last = None, None, None
        for attempt in range(10):
            try:
                after = client.info()
                actual = client.get_config()
                break
            except (ValueError, DeviceError, OSError, TimeoutError) as exc:
                last = str(exc)
                if attempt < 9:
                    try:
                        client.transport.reconnect(timeout=1)
                    except (OSError, DeviceError, TimeoutError):
                        pass
                    time.sleep(.1)
        clean = actual == before
        return {"firmware_verified": True, "target": target,
                "version": metadata["version"], "image_sha256": metadata["image_sha256"],
                "full_image_readback": verify_readback,
                "device": {key: value.hex() if isinstance(value, bytes) else value for key, value in updated.items()},
                "settings_verified_unchanged": clean,
                "settings_before": before, "settings_after": actual,
                "application_info": after, "output_enabled": False,
                "settings_saved_by_this_tool": False,
                "warning": None if clean else "Firmware verified, but settings readback is unverified or differs. Do not enable output until checked. " + (last or "")}


def register_maintenance_tools(server, session, read, write, guard):
    firmware_write = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_firmware_inspect(package_path: str) -> dict[str, Any]:
        """Validate local C86 .fw.json size/CRC/SHA256/embedded role/layout without opening USB. Returns image_sha256 for an explicitly requested update. Does not flash."""
        return read_package(package_path)[1]

    @server.tool(annotations=firmware_write, structured_output=True)
    @guard
    def keyboard_firmware_update(package_path: str, target: str, expected_sha256: str,
                                 confirm_update: StrictBool,
                                 verify_readback: StrictBool = True) -> dict[str, Any]:
        """Explicitly update left/right C86 application via left USB and existing split cable. Requires firmware-write permission, inspected image SHA256 and confirm_update=true. Saved settings required; verify image and settings afterward. Never updates permanent bootloader or enables output. No automatic retry/rollback. Keep power connected; this operation can take minutes."""
        return update_app(session, package_path, target, expected_sha256, confirm_update, verify_readback)
