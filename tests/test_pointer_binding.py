"""Firmware 0.3.48 pointer bindings (kind 7), 0.3.49 encoder pointer acceleration and the 0.3.47 ADC compare range in MCP tools; never opens USB."""
import asyncio
import struct
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from host.client import KeyboardClient
from host.mcp_configuration import binding_catalog, binding_label, layout
from host.mcp_server import build_server
from host.protocol import (ADC_COMPARE, CAP_ADC_TUNING, CAP_MOUSE_BUTTON, CAP_POINTER, CAP_POINTER_ACCEL, INFO, MUTATIONS, POINTER_ACCEL_MAX, POINTER_MAX, ProtocolError,
                           binding_record, decode_binding, encode_binding, require_binding_capability)
from host.simulator import DemoTransport, defaults

RIGHT = dict(input=0, kind=7, code=800, modifiers=0)
UP_KNOB = dict(input=22, kind=7, code=-25, modifiers=1)


class Transport(DemoTransport):
    def __init__(self, supported=True, strip=0):
        super().__init__()
        self.commands = []
        self.supported = supported
        self.strip = strip | (0 if supported else CAP_POINTER | CAP_POINTER_ACCEL)

    def _command(self, op, payload):
        self.commands.append(op)
        if op == ADC_COMPARE:  # the demo has no ADC; echo the requested key the way firmware does
            return struct.pack("<BBIHHH", payload[0], 4, 1, 2000, 2000, 2000)
        response = super()._command(op, payload)
        if op == INFO:
            response = bytearray(response)
            caps = struct.unpack_from("<I", response, 16)[0] | CAP_ADC_TUNING
            struct.pack_into("<I", response, 16, caps & ~self.strip)
        return bytes(response)


def device(write=True, supported=True, bindings=(), strip=0):
    transport = Transport(supported, strip)
    for binding in bindings:
        transport.profile["bindings"][binding["input"]] = dict(binding)
    return KeyboardClient(transport, write_enabled=write), transport


def call(server, name, **arguments):
    return asyncio.run(server.call_tool(name, arguments)).structured_content


class PointerCodecTests(unittest.TestCase):
    def test_round_trip_and_wire_layout(self):
        for record in (RIGHT, dict(input=20, kind=7, code=-25, modifiers=0), dict(input=14, kind=7, code=-4000, modifiers=1),
                       dict(input=13, kind=7, code=4000, modifiers=1), dict(input=27, kind=7, code=1, modifiers=0)):
            self.assertEqual(decode_binding(encode_binding(record)), record)
        self.assertEqual(encode_binding(dict(input=20, kind=7, code=-25, modifiers=1)), bytes((20, 7, 0xE7, 0xFF, 1, 0)))
        self.assertEqual(encode_binding(RIGHT), bytes((0, 7, 0x20, 0x03, 0, 0)))

    def test_validation(self):
        for bad in (dict(code=0), dict(code=POINTER_MAX + 1), dict(code=-POINTER_MAX - 1), dict(modifiers=2),
                    dict(modifiers=-1), dict(code=None)):
            with self.assertRaises(ProtocolError, msg=bad):
                binding_record({**RIGHT, **bad})
        with self.assertRaises(ProtocolError):
            binding_record({**RIGHT, "kind": 8})
        with self.assertRaises(ProtocolError):  # kind 7 has no keys list
            binding_record({**RIGHT, "keys": [4, 5]})
        with self.assertRaises(ProtocolError):  # 0x8000 decodes to -32768, out of range
            decode_binding(bytes((0, 7, 0x00, 0x80, 0, 0)))

    def test_capability_gate(self):
        record = binding_record(RIGHT)
        with self.assertRaisesRegex(ProtocolError, "0x400000"):
            require_binding_capability(record, CAP_MOUSE_BUTTON)
        require_binding_capability(record, CAP_POINTER)

    def test_label_and_catalog(self):
        self.assertEqual(binding_label(RIGHT), "Pointer Right (800)")
        self.assertEqual(binding_label(dict(input=0, kind=7, code=-800, modifiers=0)), "Pointer Left (800)")
        self.assertEqual(binding_label(dict(input=0, kind=7, code=5, modifiers=1)), "Pointer Down (5)")
        self.assertEqual(binding_label(UP_KNOB), "Pointer Up (25)")
        catalog = binding_catalog()
        self.assertEqual(catalog["kinds"]["7"], "pointer")
        self.assertEqual(catalog["pointer"]["capability"], CAP_POINTER)
        self.assertEqual(catalog["pointer"]["speed_range"], [-4000, 4000])
        self.assertEqual(set(catalog["pointer"]["axis_in_modifiers"]), {"0", "1"})


class PointerToolTests(unittest.TestCase):
    def test_get_config_layout_and_catalog_with_pointer_bindings(self):
        client, transport = device(write=False, bindings=(RIGHT, UP_KNOB))
        server = build_server(client)
        config = call(server, "keyboard_get_config")
        self.assertEqual(config["bindings"][0], RIGHT)
        self.assertEqual(config["bindings"][22], UP_KNOB)
        inputs = call(server, "keyboard_layout")["inputs"]
        self.assertEqual(inputs[0]["binding_label"], "Pointer Right (800)")
        self.assertEqual(inputs[0]["label"], "왼쪽 Pointer Right (800)")
        self.assertEqual(inputs[22]["binding_label"], "Pointer Up (25)")
        self.assertFalse(inputs[0]["binding_ambiguous"])
        self.assertEqual(call(server, "keyboard_binding_catalog")["kinds"]["7"], "pointer")
        self.assertTrue(call(server, "keyboard_info")["features"]["pointer_mapping"])
        exported = call(server, "keyboard_export_studio_profile")
        self.assertEqual(exported["device"]["bindings"][0], RIGHT)
        self.assertFalse(set(transport.commands) & MUTATIONS)

    def test_offline_layout_accepts_pointer_profile(self):
        profile = defaults()
        profile["bindings"][14] = dict(input=14, kind=7, code=-300, modifiers=0)
        item = layout(profile)["inputs"][14]
        self.assertEqual(item["binding_label"], "Pointer Left (300)")

    def test_set_binding_writes_pointer_and_reads_back(self):
        client, transport = device()
        server = build_server(client)
        echoed = call(server, "keyboard_set_binding", input=22, kind=7, code=-25, modifiers=1)
        self.assertEqual(echoed, UP_KNOB)
        self.assertEqual(transport.profile["bindings"][22], UP_KNOB)
        self.assertEqual(call(server, "keyboard_get_config")["bindings"][22], UP_KNOB)

    def test_set_bindings_applies_pointer_batch(self):
        client, transport = device()
        server = build_server(client)
        batch = [dict(input=i, kind=7, code=code, modifiers=axis)
                 for i, code, axis in ((0, -600, 0), (1, 600, 0), (2, -600, 1), (3, 600, 1))]
        outcome = call(server, "keyboard_set_bindings", bindings=batch)
        self.assertNotIn("failed", str(outcome.get("status", "")).lower())
        config = call(server, "keyboard_get_config")
        self.assertEqual(config["bindings"][:4], batch)
        labels = [item["binding_label"] for item in call(server, "keyboard_layout")["inputs"][:4]]
        self.assertEqual(labels, ["Pointer Left (600)", "Pointer Right (600)", "Pointer Up (600)", "Pointer Down (600)"])

    def test_firmware_without_pointer_rejects_before_any_write(self):
        client, transport = device(supported=False)
        server = build_server(client)
        self.assertFalse(call(server, "keyboard_info")["features"]["pointer_mapping"])
        before = list(transport.profile["bindings"])
        transport.commands.clear()
        with self.assertRaisesRegex(ToolError, "0x400000"):
            asyncio.run(server.call_tool("keyboard_set_binding", dict(input=0, kind=7, code=800, modifiers=0)))
        with self.assertRaisesRegex(ToolError, "0x400000"):
            asyncio.run(server.call_tool("keyboard_set_bindings", dict(bindings=[RIGHT])))
        self.assertFalse(set(transport.commands) & MUTATIONS)
        self.assertEqual(transport.profile["bindings"], before)

    def test_invalid_pointer_rejected_by_tool(self):
        client, transport = device()
        server = build_server(client)
        transport.commands.clear()
        for arguments in (dict(code=0), dict(code=5000), dict(modifiers=2)):
            with self.assertRaises(ToolError, msg=arguments):
                asyncio.run(server.call_tool("keyboard_set_binding", {**RIGHT, **arguments}))
        self.assertFalse(set(transport.commands) & MUTATIONS)


ACCEL_KNOB = dict(input=20, kind=7, code=-25, modifiers=0, accel=4)


class PointerAccelTests(unittest.TestCase):
    def test_codec_wire_layout_and_round_trip(self):
        self.assertEqual(encode_binding(ACCEL_KNOB), bytes((20, 7, 0xE7, 0xFF, 0, 4)))
        for record in (ACCEL_KNOB, dict(input=27, kind=7, code=4000, modifiers=1, accel=POINTER_ACCEL_MAX),
                       dict(input=23, kind=7, code=1, modifiers=0, accel=1)):
            self.assertEqual(decode_binding(encode_binding(record)), record)
        # accel 0 is the same record as no accel field, and encodes a zero last byte
        self.assertEqual(binding_record({**ACCEL_KNOB, "accel": 0}), dict(input=20, kind=7, code=-25, modifiers=0))
        self.assertEqual(encode_binding({**ACCEL_KNOB, "accel": 0})[5], 0)

    def test_validation(self):
        for bad in ({"accel": POINTER_ACCEL_MAX + 1}, {"accel": -1}, {"accel": True}, {"input": 19}, {"input": 0}):
            with self.assertRaises(ProtocolError, msg=bad):
                binding_record({**ACCEL_KNOB, **bad})
        for other in (dict(input=20, kind=2, code=1, modifiers=0), dict(input=20, kind=5, code=1, modifiers=0)):
            with self.assertRaises(ProtocolError):  # only kind 7 has an accel field
                binding_record({**other, "accel": 2})
        with self.assertRaises(ProtocolError):  # other kinds still reject a nonzero reserved byte
            decode_binding(bytes((20, 2, 1, 0, 0, 3)))
        with self.assertRaises(ProtocolError):  # accel byte on a Hall pointer binding
            decode_binding(bytes((0, 7, 0x20, 0x03, 0, 2)))

    def test_capability_gate(self):
        record = binding_record(ACCEL_KNOB)
        with self.assertRaisesRegex(ProtocolError, "0x800000"):
            require_binding_capability(record, CAP_POINTER)
        require_binding_capability(record, CAP_POINTER | CAP_POINTER_ACCEL)
        require_binding_capability(binding_record({**ACCEL_KNOB, "accel": 0}), CAP_POINTER)

    def test_label_and_catalog(self):
        self.assertEqual(binding_label(dict(input=20, kind=7, code=20, modifiers=0, accel=4)), "Pointer Right (20, accel 4)")
        self.assertEqual(binding_label(ACCEL_KNOB), "Pointer Left (25, accel 4)")
        accel = binding_catalog()["pointer"]["accel"]
        self.assertEqual(accel["capability"], CAP_POINTER_ACCEL)
        self.assertEqual(accel["range"], [0, 15])
        self.assertEqual(accel["inputs"], [20, 27])
        self.assertIn("40 detents/s", accel["model"])

    def test_read_tools_show_accel(self):
        client, transport = device(write=False, bindings=(ACCEL_KNOB,))
        server = build_server(client)
        self.assertEqual(call(server, "keyboard_get_config")["bindings"][20], ACCEL_KNOB)
        self.assertEqual(call(server, "keyboard_layout")["inputs"][20]["binding_label"], "Pointer Left (25, accel 4)")
        features = call(server, "keyboard_info")["features"]
        self.assertTrue(features["pointer_acceleration"])
        self.assertEqual(call(server, "keyboard_export_studio_profile")["device"]["bindings"][20], ACCEL_KNOB)
        self.assertFalse(set(transport.commands) & MUTATIONS)

    def test_set_binding_with_accel(self):
        client, transport = device()
        server = build_server(client)
        self.assertEqual(call(server, "keyboard_set_binding", input=20, kind=7, code=-25, modifiers=0, accel=4), ACCEL_KNOB)
        self.assertEqual(transport.profile["bindings"][20], ACCEL_KNOB)
        # accel=0 (the default) writes a plain pointer binding with no accel field
        plain = call(server, "keyboard_set_binding", input=20, kind=7, code=-25, modifiers=0)
        self.assertEqual(plain, dict(input=20, kind=7, code=-25, modifiers=0))
        self.assertEqual(call(server, "keyboard_get_config")["bindings"][20], plain)

    def test_set_bindings_with_accel(self):
        client, _ = device()
        server = build_server(client)
        batch = [ACCEL_KNOB, dict(input=21, kind=7, code=25, modifiers=0, accel=4),
                 dict(input=22, kind=7, code=-25, modifiers=1)]
        call(server, "keyboard_set_bindings", bindings=batch)
        self.assertEqual(call(server, "keyboard_get_config")["bindings"][20:23], batch)
        labels = [item["binding_label"] for item in call(server, "keyboard_layout")["inputs"][20:23]]
        self.assertEqual(labels, ["Pointer Left (25, accel 4)", "Pointer Right (25, accel 4)", "Pointer Up (25)"])

    def test_firmware_0348_rejects_accel_before_any_write(self):
        client, transport = device(strip=CAP_POINTER_ACCEL)
        server = build_server(client)
        self.assertFalse(call(server, "keyboard_info")["features"]["pointer_acceleration"])
        before = list(transport.profile["bindings"])
        transport.commands.clear()
        with self.assertRaisesRegex(ToolError, "0x800000"):
            asyncio.run(server.call_tool("keyboard_set_binding", dict(input=20, kind=7, code=-25, modifiers=0, accel=4)))
        with self.assertRaisesRegex(ToolError, "0x800000"):
            asyncio.run(server.call_tool("keyboard_set_bindings", dict(bindings=[ACCEL_KNOB])))
        self.assertFalse(set(transport.commands) & MUTATIONS)
        self.assertEqual(transport.profile["bindings"], before)
        # a plain pointer binding still works on 0.3.48
        self.assertEqual(call(server, "keyboard_set_binding", input=20, kind=7, code=-25, modifiers=0)["kind"], 7)

    def test_accel_on_non_encoder_rejected_by_tool(self):
        client, transport = device()
        server = build_server(client)
        transport.commands.clear()
        for arguments in (dict(input=0, kind=7, code=800, modifiers=0, accel=2), dict(input=20, kind=7, code=-25, modifiers=0, accel=16),
                          dict(input=20, kind=2, code=1, modifiers=0, accel=2)):
            with self.assertRaises(ToolError, msg=arguments):
                asyncio.run(server.call_tool("keyboard_set_binding", arguments))
        self.assertFalse(set(transport.commands) & MUTATIONS)


class AdcCompareRangeTests(unittest.TestCase):
    def test_right_hall_keys_accepted(self):
        client, _ = device(write=False)
        for key in (0, 6, 7, 13):
            self.assertEqual(client.adc_compare(key)["decision"], 2000)
        for key in (-1, 14):
            with self.assertRaises(ProtocolError):
                client.adc_compare(key)


if __name__ == "__main__":
    unittest.main()
