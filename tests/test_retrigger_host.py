"""Retrigger configuration gates and sampled state semantics; never opens USB."""
import asyncio
from copy import deepcopy
import struct
import unittest

from host.client import DeviceError, KeyboardClient
from host.mcp_configuration import propose_keys
from host.mcp_diagnostics import capture_key_timing
from host.mcp_server import build_server
from host.protocol import (CAP_RETRIGGER_BEFORE_RELEASE, INFO, MUTATIONS, SET_KEY,
                           ProtocolError, decode_key, encode_key, validate_profile)
from host.sampled_rt import SampledRT
from host.simulator import DemoTransport, defaults


def record(flags=15):
    return {**defaults()['keys'][0], 'flags': flags, 'released': 1000, 'pressed': 3000,
            'noise_deadband': 0, 'rt_press': 50, 'rt_release': 200}


class RecordingTransport(DemoTransport):
    def __init__(self, supported=True):
        super().__init__()
        self.commands = []
        self.supported = supported
        self.profile['keys'][0] = record(7)
        self.pose = [0] * 14

    def _command(self, op, payload):
        self.commands.append(op)
        response = super()._command(op, payload)
        if op == INFO and not self.supported:
            response = bytearray(response)
            caps = struct.unpack_from('<I', response, 16)[0]
            struct.pack_into('<I', response, 16, caps & ~CAP_RETRIGGER_BEFORE_RELEASE)
        return response


def setup(supported=True):
    transport = RecordingTransport(supported)
    client = KeyboardClient(transport, write_enabled=True)
    return client, transport, build_server(client)


def call(server, name, **arguments):
    return asyncio.run(server.call_tool(name, arguments)).structured_content


class RetriggerTests(unittest.TestCase):
    def test_default_off_and_record_roundtrip_still_22_bytes(self):
        self.assertTrue(all(not k['flags'] & 8 for k in defaults()['keys']))
        for flags in range(16):
            value = record(flags)
            self.assertEqual(len(encode_key(value)), 22)
            self.assertEqual(decode_key(encode_key(value)), value)
        for flags in (16, 255, True):
            with self.assertRaises(ProtocolError):
                encode_key(record(flags))

    def test_unsupported_direct_and_last_profile_key_rejected_before_mutation(self):
        client, transport, _ = setup(False)
        with self.assertRaisesRegex(ProtocolError, '0x40000'):
            client.set_key(record())
        profile = client.get_config()
        profile['keys'][13]['flags'] |= 8
        with self.assertRaisesRegex(ProtocolError, '0x40000'):
            client.import_profile(profile)
        self.assertFalse(set(transport.commands) & MUTATIONS)
        server = build_server(client)
        with self.assertRaisesRegex(Exception, '0x40000'):
            call(server, 'keyboard_import_studio_profile', profile=profile)
        self.assertFalse(set(transport.commands) & MUTATIONS)

    def test_group_boolean_preservation_and_profile_retrieval(self):
        client, transport, server = setup()
        original = client.get_config()
        preview = call(server, 'keyboard_preview_keys', keys=[0, 1], patch={'retrigger_before_release': True})
        self.assertFalse(set(transport.commands) & MUTATIONS)
        self.assertTrue(preview['proposed_options'][0]['retrigger_before_release'])
        applied = call(server, 'keyboard_set_keys', keys=[0, 1], patch={'retrigger_before_release': True},
                       expected_fingerprint=preview['fingerprint'])
        self.assertTrue(applied['complete'])
        value = client.get_config()
        expected = deepcopy(original)
        expected['keys'][0]['flags'] |= 8
        expected['keys'][1]['flags'] |= 8
        self.assertEqual(value, expected)
        options = call(server, 'keyboard_key_options', keys=[0, 1])
        self.assertTrue(options['retrigger_supported'])
        self.assertTrue(all(k['retrigger_before_release'] for k in options['keys']))
        self.assertTrue(options['keys'][0]['retrigger_active'])
        self.assertFalse(options['keys'][1]['retrigger_active'])
        self.assertEqual(call(server, 'keyboard_get_config'), expected)
        self.assertEqual(validate_profile(call(server, 'keyboard_export_profile')), expected)
        self.assertTrue(call(server, 'keyboard_info')['features']['retrigger_before_release'])
        patched = propose_keys(value, [0], {'rt_press': 60}, capabilities=CAP_RETRIGGER_BEFORE_RELEASE)
        self.assertEqual(patched[0]['flags'], 15)
        for invalid in (1, 0, 'true', None):
            with self.assertRaisesRegex(ProtocolError, 'boolean'):
                propose_keys(value, [0], {'retrigger_before_release': invalid}, capabilities=CAP_RETRIGGER_BEFORE_RELEASE)

    def test_group_gate_and_single_strict_boolean_and_omitted_preserved(self):
        _, transport, server = setup(False)
        for name in ('keyboard_preview_keys', 'keyboard_set_keys'):
            with self.assertRaisesRegex(Exception, '0x40000'):
                call(server, name, keys=[0, 1], patch={'retrigger_before_release': True})
        self.assertFalse(set(transport.commands) & MUTATIONS)
        client, transport, server = setup()
        arguments = dict(key=0, enabled=True, rapid_trigger=True, actuation=200,
                         rt_press=50, rt_release=200, top_deadzone=20, noise_deadband=0)
        for invalid in ('true', 1):
            before = len(transport.commands)
            with self.assertRaises(Exception):
                call(server, 'keyboard_set_key', **arguments, retrigger_before_release=invalid)
            self.assertEqual(len(transport.commands), before)
        call(server, 'keyboard_set_key', **arguments, retrigger_before_release=True)
        self.assertEqual(call(server, 'keyboard_set_key', **arguments)['flags'], 15)
        self.assertEqual(call(server, 'keyboard_set_key', **arguments, retrigger_before_release=False)['flags'], 7)

    def test_partial_reversal_one_event_and_held_peak_preserved(self):
        model = SampledRT(record())
        events = []
        for travel in (0, 400, 350, 390, 400, 500, 600, 540, 590):
            model.step(1000 + travel * 2)
            events.append(model.retriggered)
        self.assertEqual(events, [False, False, False, False, True, False, False, False, True])
        self.assertEqual(model.extreme, 600)
        self.assertFalse(model.step(1000 + 399 * 2))
        self.assertFalse(model.retriggered)
        self.assertFalse(model.trough_armed)

    def test_release_deadzone_and_rt_off_precedence(self):
        for flags in (7, 13):
            model = SampledRT(record(flags))
            for travel in (0, 400, 350, 400, 600, 550, 600):
                model.step(1000 + 2 * travel)
                self.assertFalse(model.retriggered)
        model = SampledRT({**record(), 'bottom_deadzone': 100})
        for travel in (0, 950, 850, 900):
            model.step(1000 + 2 * travel)
            self.assertFalse(model.retriggered)
        model.step(1040)
        self.assertFalse(model.pressed)
        self.assertFalse(model.trough_armed)

    def test_demo_logical_state_and_event_intent_not_usb_ack(self):
        client, transport, _ = setup()
        client.set_key(record())
        for travel in (0, 400, 350, 400):
            transport.pose[0] = travel
            status = client.status(0)
        self.assertTrue(status['pressed_mask'] & 1)
        self.assertEqual(transport.retrigger_events[0], 1)
        transport.pose[0] = 700
        client.status(0)
        self.assertEqual(transport.retrigger_events[0], 1)
        transport.pose[0] = 650
        client.status(0)
        client.set_key(record())  # Even identical settings reset pending trough state.
        transport.pose[0] = 700
        client.status(0)
        self.assertEqual(transport.retrigger_events[0], 1)

    def test_timing_refuses_synthetic_edges_but_status_available(self):
        client, transport, _ = setup()
        client.set_key(record())
        before = len(transport.commands)
        with self.assertRaisesRegex(DeviceError, 'synthetic USB'):
            capture_key_timing(client, 0, .2)
        self.assertFalse(set(transport.commands[before:]) & MUTATIONS)
        self.assertTrue(client.status(0)['fresh'])


if __name__ == '__main__':
    unittest.main()
