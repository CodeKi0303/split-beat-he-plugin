"""Firmware 0.3.46 shared custom curve (curve_id 2) in MCP tools; never opens USB."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import struct
import unittest

from host.auto_fit import analyze_auto_fit
from host.client import KeyboardClient
from host.hall_curve import normalized_from_raw, normalized_to_mm, raw_to_estimated_mm
from host.mcp_auto_fit import register_auto_fit_tools
from host.mcp_diagnostics import capture_key_timing, capture_sensors
from host.mcp_server import build_server
from host.performance import decode_scheduler_diagnostics
from host.protocol import (CAP_CUSTOM_CURVE, CUSTOM_CURVE, CUSTOM_CURVE_IDENTITY, DEFAULTS, INFO, MUTATIONS,
                           ProtocolError)
from host.session import KeyboardSession
from host.simulator import DemoTransport, defaults

FIXTURE = json.loads((Path(__file__).parent / "fixtures/auto-fit.json").read_text(encoding="utf-8"))
# A drawn S-shape: shallow start, steep middle, flat run near the bottom.
DRAWN = [100, 250, 500, 900, 1500, 2300, 3300, 4400, 5500, 6500, 7400, 8200, 8800, 8800, 9500]


def record(curve_id=2):
    return {**defaults()["keys"][0], "flags": 7, "released": 1000, "pressed": 3000,
            "noise_deadband": 0, "curve_id": curve_id}


class Transport(DemoTransport):
    def __init__(self, supported=True):
        super().__init__()
        self.commands = []
        self.supported = supported
        self.pose = [0] * 14

    def _command(self, op, payload):
        self.commands.append(op)
        response = super()._command(op, payload)
        if op == INFO and not self.supported:
            response = bytearray(response)
            caps = struct.unpack_from("<I", response, 16)[0]
            struct.pack_into("<I", response, 16, caps & ~CAP_CUSTOM_CURVE)
        return response


def drawn_device(write=True, supported=True):
    """A demo keyboard whose key 0 uses curve 2 with DRAWN, set the way firmware stores it."""
    transport = Transport(supported)
    transport.profile["keys"][0] = record()
    transport.custom_curve = list(DRAWN)
    return KeyboardClient(transport, write_enabled=write), transport


def call(server, name, **arguments):
    return asyncio.run(server.call_tool(name, arguments)).structured_content


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.001
        return self.now

    def delay(self, seconds):
        self.now += seconds


class CustomCurveTests(unittest.TestCase):
    def test_get_config_accepts_curve_two(self):
        client, _ = drawn_device(write=False)
        config = call(build_server(client), "keyboard_get_config")
        self.assertEqual(config["keys"][0]["curve_id"], 2)

    def test_set_custom_curve_roundtrip_is_ram_mutation(self):
        client, transport = drawn_device()
        self.assertEqual(client.custom_curve(), DRAWN)
        transport.enabled = True
        self.assertEqual(client.set_custom_curve(CUSTOM_CURVE_IDENTITY), list(CUSTOM_CURVE_IDENTITY))
        self.assertFalse(transport.enabled)
        self.assertTrue(transport.dirty)
        before = client.info()["config_crc"]
        client.set_custom_curve(DRAWN)
        self.assertNotEqual(client.info()["config_crc"], before)  # 0.3.46 CRC covers the table
        with self.assertRaises(ProtocolError):
            client.set_custom_curve(list(reversed(DRAWN)))
        readonly = KeyboardClient(transport, write_enabled=False)
        self.assertEqual(readonly.custom_curve(), DRAWN)
        with self.assertRaises(PermissionError):
            readonly.set_custom_curve(DRAWN)

    def test_save_and_reconnect_keep_table_defaults_restore_identity(self):
        client, transport = drawn_device()
        client.save()
        client.set_custom_curve(CUSTOM_CURVE_IDENTITY)
        transport.reconnect()
        self.assertEqual(transport.custom_curve, DRAWN)
        client.request(DEFAULTS)
        self.assertEqual(client.custom_curve(), list(CUSTOM_CURVE_IDENTITY))

    def test_firmware_without_capability_rejects_table_and_curve_two(self):
        transport = Transport(supported=False)
        client = KeyboardClient(transport, write_enabled=True)
        with self.assertRaisesRegex(ProtocolError, "0.3.46"):
            client.custom_curve()
        with self.assertRaisesRegex(ProtocolError, "0x200000"):
            client.set_key(record())
        self.assertFalse(set(transport.commands) & MUTATIONS)
        self.assertNotIn(CUSTOM_CURVE, transport.commands)

    def test_demo_status_travel_follows_device_table(self):
        client, transport = drawn_device()
        transport.pose[0] = 3000  # between grid points: 900 + 500*(1500-900)/625
        status = client.status(0)
        self.assertEqual(status["raw"][0], 1600)
        self.assertEqual(status["travel"][0], normalized_from_raw(1600, 1000, 3000, 2, DRAWN))
        self.assertEqual(status["travel"][0], 1380)
        transport.pose[0] = 3125  # on the grid: DRAWN[4]
        self.assertEqual(client.status(0)["travel"][0], 1500)

    def test_capture_sensors_estimates_with_device_table(self):
        client, transport = drawn_device(write=False)
        transport.pose[0] = 3125
        clock = Clock()
        result = capture_sensors(client, 0.5, 20, True, clock=clock, delay=clock.delay,
                                 total_travel_mm=[4.0] * 14)
        self.assertEqual(result["custom_curve"], DRAWN)
        self.assertEqual(result["curve_ids"][0], 2)
        row = result["samples"][0]
        self.assertEqual(row["raw_estimated_mm"][0], normalized_to_mm(1500, 4.0))
        self.assertNotEqual(row["raw_estimated_mm"][0], normalized_to_mm(3125, 4.0))  # not linear
        self.assertFalse(set(transport.commands) & MUTATIONS)

    def test_capture_sensors_skips_table_when_no_key_uses_it(self):
        client, transport = drawn_device(write=False)
        transport.profile["keys"][0] = record(0)
        clock = Clock()
        result = capture_sensors(client, 0.5, 20, False, clock=clock, delay=clock.delay,
                                 total_travel_mm=[4.0] * 14)
        self.assertNotIn("custom_curve", result)
        self.assertNotIn(CUSTOM_CURVE, transport.commands)

    def test_key_timing_estimates_with_device_table(self):
        client, transport = drawn_device(write=False)
        transport.pose[0] = 6250
        clock = Clock()
        result = capture_key_timing(client, 0, 0.5, 20, True, clock=clock, delay=clock.delay,
                                    total_travel_mm=3.5)
        self.assertEqual(result["custom_curve"], DRAWN)
        expected = raw_to_estimated_mm(2250, record(), 3.5, DRAWN)
        self.assertEqual(result["samples"][0]["raw"], 2250)
        self.assertEqual(result["samples"][0]["raw_estimated_mm"], expected)
        self.assertAlmostEqual(expected, 6500 * 3.5 / 10000)

    def test_mcp_capture_tools_work_on_curve_two(self):
        client, _ = drawn_device(write=False)
        server = build_server(client)
        sensors = call(server, "keyboard_capture_sensors", duration_seconds=0.3, sample_hz=20, include_samples=True)
        self.assertEqual(sensors["custom_curve"], DRAWN)
        timing = call(server, "keyboard_capture_key_timing", key=0, duration_seconds=0.3, sample_hz=20)
        self.assertEqual(timing["custom_curve"], DRAWN)

    def test_curve_catalog_reads_device_table(self):
        client, transport = drawn_device(write=False)
        catalog = call(build_server(client), "keyboard_curve_catalog")
        self.assertEqual([c["curve_id"] for c in catalog["curves"]], [0, 1, 2])
        self.assertEqual(catalog["device"]["custom_curve"], DRAWN)
        self.assertEqual(catalog["device"]["custom_curve_keys"], [0])
        self.assertFalse(set(transport.commands) & MUTATIONS)
        old = KeyboardClient(Transport(supported=False))
        catalog = call(build_server(old), "keyboard_curve_catalog")
        self.assertFalse(catalog["device"]["custom_curve_supported"])
        self.assertNotIn("custom_curve", catalog["device"])
        disconnected = call(build_server(None), "keyboard_curve_catalog")
        self.assertFalse(disconnected["device"]["connected"])

    def test_preview_accepts_curve_two_patch(self):
        client, transport = drawn_device()
        transport.profile["keys"][0] = record(0)
        preview = call(build_server(client), "keyboard_preview_keys", keys=[0], patch={"curve_id": 2})
        self.assertEqual(preview["proposed_keys"][0]["curve_id"], 2)
        self.assertFalse(set(transport.commands) & MUTATIONS)

    def test_scheduler_fault_evidence_fields(self):
        payload = struct.pack("<13I", *([0] * 6), 0x03020105, *([0] * 6))  # cc_error is field 6
        result = decode_scheduler_diagnostics(payload)
        self.assertEqual((result["cc_current_error"], result["cc_last_error"], result["cc_retries"],
                          result["link_restarts"]), (5, 1, 2, 3))


class CustomCurveAutoFitTests(unittest.TestCase):
    def fixture(self, curve_id=2, table=CUSTOM_CURVE_IDENTITY):
        value = deepcopy(FIXTURE["valid"][0]["input"])
        value["keyConfig"]["curve_id"] = curve_id
        if table is not None:
            value["customCurve"] = list(table)
        return value

    def test_identity_table_reproduces_linear_recommendation(self):
        linear = analyze_auto_fit(deepcopy(FIXTURE["valid"][0]["input"]))
        custom = analyze_auto_fit(self.fixture())
        self.assertTrue(custom["ready"], custom["reasons"])
        self.assertEqual(custom["evidence"], linear["evidence"])
        expected = {**linear["recommendation"]["config"], "curve_id": 2}
        self.assertEqual(custom["recommendation"]["config"], expected)

    def test_curve_two_needs_table_and_uses_it(self):
        missing = analyze_auto_fit(self.fixture(table=None))
        self.assertFalse(missing["ready"])
        self.assertIn("곡선 표", missing["reasons"][0])
        drawn = analyze_auto_fit(self.fixture(table=DRAWN))
        linear = analyze_auto_fit(deepcopy(FIXTURE["valid"][0]["input"]))
        self.assertNotEqual(drawn["evidence"], linear["evidence"])

    def test_session_records_device_table_and_rejects_table_change(self):
        value = deepcopy(FIXTURE["valid"][0]["input"])
        transport = Transport()
        transport.pose = None
        transport.profile["keys"][0] = {**value["keyConfig"], "curve_id": 2}
        transport.custom_curve = list(DRAWN)
        client = KeyboardClient(transport, write_enabled=True)
        session = KeyboardSession(client=client)
        registered = {}

        class Server:
            def tool(self, **kwargs):
                def decorate(fn):
                    registered[fn.__name__] = fn
                    return fn
                return decorate

        register_auto_fit_tools(Server(), session, {}, {}, lambda fn: fn)
        registered["keyboard_autofit_begin"](0, 3.4)
        self.assertEqual(session.auto_fit.input["customCurve"], DRAWN)
        self.assertEqual(registered["keyboard_autofit_export"]()["customCurve"], DRAWN)
        client.set_custom_curve(CUSTOM_CURVE_IDENTITY)
        with self.assertRaisesRegex(Exception, "custom curve table changed"):
            session.auto_fit.check(session)


if __name__ == "__main__":
    unittest.main()
