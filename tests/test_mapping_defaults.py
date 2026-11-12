"""Live mapping names and packaged defaults; no USB or device writes."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from host.client import KeyboardClient
from host.default_bindings import default_bindings
from host.mcp_configuration import binding_label, layout, session_layout
from host.mcp_server import build_server
from host.protocol import MUTATIONS
from host.session import KeyboardSession
from host.simulator import DemoTransport, defaults


class RecordingTransport(DemoTransport):
    def __init__(self):
        super().__init__()
        self.commands = []

    def _command(self, op, payload):
        self.commands.append(op)
        return super()._command(op, payload)


class MappingDefaultsTests(unittest.TestCase):
    def test_canonical_mapping_only_and_independent_defaults(self):
        root = Path(__file__).resolve().parents[1]
        canonical = json.loads((root / 'default-bindings.json').read_text(encoding='utf-8'))
        self.assertEqual(set(canonical), {'schema', 'description', 'bindings'})
        self.assertEqual((root / 'default-bindings.json').read_bytes(),
                         (root / 'host/default-bindings.json').read_bytes())
        self.assertEqual(default_bindings(), canonical['bindings'])
        self.assertEqual(defaults()['bindings'], canonical['bindings'])
        first = default_bindings()
        first[0]['code'] = 4
        self.assertEqual(default_bindings()[0]['code'], 43)
        self.assertTrue(all(key['released'] == key['pressed'] == 0 and key['flags'] == 3
                            for key in defaults()['keys']))

    def test_disconnected_tool_has_no_io_and_never_claims_current_mapping(self):
        with patch('host.session.HidTransport') as transport:
            transport.side_effect = AssertionError('no hardware open')
            transport.enumerate.side_effect = AssertionError('no device enumeration')
            session = KeyboardSession()
            server = build_server(session)
            result = asyncio.run(server.call_tool('keyboard_layout', {})).structured_content
        self.assertIsNone(session.client)
        self.assertEqual(result['mapping_source'], 'packaged_defaults')
        self.assertEqual(result['persistence'], 'not_device_state')
        self.assertTrue(all(item['current_binding'] is None for item in result['inputs']))
        self.assertIn('기본값', result['inputs'][1]['label'])
        self.assertEqual(result['inputs'][1]['binding_label'], 'A')

    def test_connected_remap_names_and_existing_config_preserved(self):
        transport = RecordingTransport()
        transport.profile['bindings'][1] = dict(input=1, kind=1, code=29, modifiers=0)
        transport.profile['bindings'][2] = dict(input=2, kind=0, code=0, modifiers=0)
        transport.profile['keys'][1].update(flags=7, released=1234, pressed=3456, rt_press=35)
        transport.dirty = True
        before = deepcopy(transport.profile)
        session = KeyboardSession(KeyboardClient(transport))
        server = build_server(session)
        result = asyncio.run(server.call_tool('keyboard_layout', {})).structured_content
        self.assertEqual(result['inputs'][1]['label'], '왼쪽 Z')
        self.assertEqual(result['inputs'][1]['physical_position'], 'Q')
        self.assertEqual(result['inputs'][1]['source_ref'], 'U1002')
        self.assertEqual(result['inputs'][1]['current_binding'], before['bindings'][1])
        self.assertEqual(result['inputs'][2]['binding_label'], '미지정')
        self.assertEqual(result['persistence'], 'active_ram_unsaved')
        self.assertEqual(result['mapping_source'], 'demo_ram')
        self.assertTrue(result['demo'])
        self.assertEqual(transport.profile, before)
        self.assertFalse(set(transport.commands) & MUTATIONS)
        # Every call reads the current mapping, including edits made since the prior call.
        transport.profile['bindings'][1]['code'] = 4
        self.assertEqual(session_layout(session)['inputs'][1]['label'], '왼쪽 A')

    def test_saved_requires_clean_and_valid_storage_and_is_demo_marked(self):
        transport = RecordingTransport()
        session = KeyboardSession(KeyboardClient(transport))
        self.assertEqual(session_layout(session)['persistence'], 'active_ram_not_persisted')
        transport.stored = deepcopy(transport.profile)
        result = session_layout(session)
        self.assertEqual(result['persistence'], 'active_ram_matches_saved')
        self.assertTrue(result['demo'])
        transport.dirty = True
        self.assertEqual(session_layout(session)['persistence'], 'active_ram_unsaved')
        self.assertFalse(set(transport.commands) & MUTATIONS)

    def test_duplicate_bindings_modifier_only_and_negative_scroll(self):
        records = layout(defaults())['inputs']
        self.assertEqual(records[13]['binding_label'], 'R Alt')
        self.assertEqual(records[13]['current_binding'], dict(input=13, kind=1, code=0, modifiers=64))
        self.assertEqual(records[25]['binding_label'], 'Scroll Down')
        self.assertEqual(records[14]['same_binding_inputs'], [14, 20])
        self.assertEqual(records[7]['same_binding_inputs'], [7, 19])
        self.assertTrue(records[14]['binding_ambiguous'])
        self.assertNotEqual(records[14]['type'], records[20]['type'])
        self.assertEqual(binding_label(dict(input=0, kind=6, keys=[4, 7], modifiers=3)),
                         'L Ctrl + L Shift + A + D')
        self.assertEqual(binding_label(dict(input=0, kind=3, code=-1, modifiers=0)), 'Scroll Left')
        self.assertEqual(binding_label(dict(input=0, kind=4, code=0xe9, modifiers=0)), 'Volume up')
        self.assertEqual(binding_label(dict(input=0, kind=5, code=1, modifiers=0)), 'Mouse Left')


if __name__ == '__main__':
    unittest.main()
