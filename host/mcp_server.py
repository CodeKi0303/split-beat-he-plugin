"""Split keyboard MCP stdio entry point; logs go to stderr, SDK owns stdout."""
from __future__ import annotations
import argparse
import json
import sys
from typing import Any
from functools import wraps

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import StrictInt, StrictBool

from .client import HidTransport, KeyboardClient, DeviceError
from .protocol import validate_profile
from .session import KeyboardSession
from .mcp_diagnostics import capability_summary, register_diagnostic_tools


def build_server(client=None):
    session = client if isinstance(client, KeyboardSession) else KeyboardSession(client=client)
    def explain_errors(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                with session.lock:
                    return fn(*args, **kwargs)
            except (ValueError, DeviceError, PermissionError, OSError, TimeoutError) as exc:
                raise ToolError(str(exc)) from exc
        return wrapped

    server = MCPServer("split_keyboard", version="0.1.2", instructions="Start with keyboard_session and keyboard_list_devices. Connect an exact serial explicitly; startup does not claim a physical device. Demo responses are simulation, never hardware validation. Configuration changes release keys and remain in RAM until explicit save verifies reboot/readback. Separate normalized 0..1000 from estimated mm and preserve measured calibration. Nonlinear curve selection needs capability0x200; use keyboard_curve_catalog for approximation limits and stroke scaling. Use physical user actions during calibration. Capture tools expose bounded host snapshots, not full ADC streams. Firmware update requires its separate permission and explicit action. Never report untested hardware performance as verified.")
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

    @server.tool(annotations=read, structured_output=True)
    @explain_errors
    def keyboard_info() -> dict[str, Any]:
        """Read firmware/board identity, generation, speed, demo and write permission."""
        info = session.require_client().info()
        return {**info, "features": capability_summary(info)}

    @server.tool(annotations=read, structured_output=True)
    @explain_errors
    def keyboard_status(side: StrictInt = 0) -> dict[str, Any]:
        """Read 7 Hall raw/travel samples, freshness, link age, calibration state. side 0=left,1=right."""
        return session.require_client().status(side)

    @server.tool(annotations=read, structured_output=True)
    @explain_errors
    def keyboard_get_config() -> dict[str, Any]:
        """Read all 14 Hall calibration/RT records and 28 bindings (RAM snapshot)."""
        return session.require_client().get_config()

    @server.tool(annotations=read, structured_output=True)
    @explain_errors
    def keyboard_export_profile() -> dict[str, Any]:
        """Export configuration as c77-profile-v1 JSON data; no filesystem writes."""
        return session.require_client().get_config()

    @server.tool(annotations=read, structured_output=True)
    @explain_errors
    def keyboard_validate_profile(profile: dict) -> dict[str, Any]:
        """Validate complete profile before any change, including all ranges and unique indices."""
        return {"valid":True, "profile":validate_profile(profile)}

    @server.tool(annotations=write, structured_output=True)
    @explain_errors
    def keyboard_set_key(key: StrictInt, enabled: StrictBool, rapid_trigger: StrictBool, actuation: StrictInt, rt_press: StrictInt, rt_release: StrictInt, top_deadzone: StrictInt, noise_deadband: StrictInt, bottom_deadzone: StrictInt | None = None, curve_id: StrictInt | None = None) -> dict[str, Any]:
        """Change one Hall key's RAM behavior, preserve calibration. Normalized range0..1000, noise0..100; output is disabled. Omitted bottom_deadzone/curve_id preserve current values. Nonzero bottom needs capability0x100; curve1 approximate Owlab Ti HE needs0x200. See keyboard_curve_catalog. Requires write-enabled session."""
        from .protocol import integer
        integer(key,0,13,"key")
        session.require_idle()
        client = session.require_client()
        record = client.get_config()["keys"][key]
        record.update(flags=(record["flags"]&4)|int(enabled)|(int(rapid_trigger)<<1), actuation=actuation, rt_press=rt_press, rt_release=rt_release, top_deadzone=top_deadzone, noise_deadband=noise_deadband)
        if bottom_deadzone is not None:
            record["bottom_deadzone"] = bottom_deadzone
        if curve_id is not None:
            record["curve_id"] = curve_id
        return client.set_key(record)

    @server.tool(annotations=write, structured_output=True)
    @explain_errors
    def keyboard_set_binding(input: StrictInt, kind: StrictInt, code: StrictInt | None = None, modifiers: StrictInt = 0, keys: list[StrictInt] | None = None) -> dict[str, Any]:
        """Set a RAM binding. Kinds 0..5 use code. Kind 6 uses keys (2 or 3 distinct ascending ordinary HID usages) plus optional modifiers and requires capability 0x2000. See keyboard_binding_catalog."""
        session.require_idle()
        record = dict(input=input, kind=kind, modifiers=modifiers)
        if code is not None:
            record["code"] = code
        if keys is not None:
            record["keys"] = keys
        return session.require_client().set_binding(record)

    @server.tool(annotations=write, structured_output=True)
    @explain_errors
    def keyboard_calibrate(key: StrictInt, action: str) -> dict[str, Any]:
        """capture_rest / capture_press / commit / cancel. Each capture requires256fresh stable samples. Ask user to physically release/press key; inspect status before next stage. Commit changes RAM only."""
        session.require_idle()
        return session.require_client().calibrate(key, action)

    @server.tool(annotations=write, structured_output=True)
    @explain_errors
    def keyboard_output(enabled: StrictBool) -> dict[str, Any]:
        """Explicitly enable or disable HID key output. Enable requires fresh split sources and calibrated enabled keys."""
        session.require_idle()
        return session.require_client().output(enabled)

    @server.tool(annotations=write, structured_output=True)
    @explain_errors
    def keyboard_import_profile(profile: dict) -> dict[str, Any]:
        """Validate entire profile then apply/read back RAM. Output disabled. Flash not changed. Interrupted import may leave partial RAM; never auto-save."""
        session.require_idle()
        return session.require_client().import_profile(profile)

    @server.tool(annotations=write, structured_output=True)
    @explain_errors
    def keyboard_save() -> dict[str, Any]:
        """Explicit flash save/reboot. Reconnect same serial and verify generation + all config records. ACK alone is not success. Do not retry automatically on uncertain result."""
        session.require_idle()
        return session.require_client().save()

    @server.tool(annotations=read, structured_output=True)
    @explain_errors
    def keyboard_list_devices() -> dict[str, Any]:
        """Enumerate only compatible vendorHID identities. Does not open or change devices. Use the exact returned serial in keyboard_connect."""
        return {"devices": HidTransport.enumerate(), "opened_devices": False}

    @server.tool(annotations=read, structured_output=True)
    @explain_errors
    def keyboard_session() -> dict[str, Any]:
        """Read local session/permissions/travel metadata without device I/O; works disconnected."""
        return session.status()

    @server.tool(annotations=write, structured_output=True)
    @explain_errors
    def keyboard_connect(serial: str | None = None, demo: StrictBool = False) -> dict[str, Any]:
        """Explicitly open an exact serial-selected keyboard and read its identity, or demo=True for simulation. No settings/output writes. Omittedserial uses only the launch-time/previous exact serial, never automatic selection."""
        return session.connect(serial=serial, demo=demo)

    @server.tool(annotations=write, structured_output=True)
    @explain_errors
    def keyboard_disconnect() -> dict[str, Any]:
        """Stop host calibration/capture and release HID handle. Does not send OUTPUT, SAVE or a firmware command; explicit keyboard_output controls output."""
        return session.disconnect()

    register_diagnostic_tools(server, session, read, write, explain_errors)
    from .mcp_configuration import register_configuration_tools
    from .mcp_calibration import register_calibration_tools
    from .mcp_maintenance import register_maintenance_tools
    register_configuration_tools(server, session, read, write, explain_errors)
    register_calibration_tools(server, session, read, write, explain_errors)
    register_maintenance_tools(server, session, read, write, explain_errors)
    from .mcp_auto_fit import register_auto_fit_tools
    register_auto_fit_tools(server, session, read, write, explain_errors)
    return server


def main():
    parser = argparse.ArgumentParser(description="Split Studio stdio MCP. Starts disconnected unless --demo. Device control requires --write-enabled.")
    parser.add_argument("--demo", action="store_true", help="Start a simulated session; never opens USB")
    parser.add_argument("--serial", help="Remember an exact serial for a later explicit keyboard_connect; no startup USB open")
    parser.add_argument("--write-enabled", action="store_true", help="Allow calibration/RAM/output/SAVE tools after explicit connection")
    parser.add_argument("--firmware-write-enabled", action="store_true", help="Allow the separate explicit firmware update tools; does not flash at startup")
    parser.add_argument("--list-devices", action="store_true", help="Print compatible vendor-HID serials then exit; no device opened")
    args = parser.parse_args()
    if args.list_devices:
        print(json.dumps(HidTransport.enumerate(), ensure_ascii=False))
        return
    if args.demo and args.serial:
        parser.error("--demo and --serial are mutually exclusive")
    session = KeyboardSession(write_enabled=args.write_enabled,
                              firmware_write_enabled=args.firmware_write_enabled,
                              serial=args.serial, demo=args.demo)
    print(f"Split Studio MCP {'DEMO NO USB' if args.demo else 'DISCONNECTED'}; {'write-enabled' if args.write_enabled else 'read-only'}", file=sys.stderr)
    try:
        build_server(session).run(transport="stdio")
    finally:
        session.disconnect()


if __name__ == "__main__":
    main()
