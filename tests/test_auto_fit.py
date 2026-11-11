"""Recommendation parity and device-state gates, using synthetic data only."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from host.auto_fit import SampledRT, analyze_auto_fit, evaluate_auto_fit
from host.client import KeyboardClient, DeviceError
from host.mcp_auto_fit import AutoFitSession, register_auto_fit_tools
from host.protocol import MUTATIONS, SAVE, SET_KEY
from host.session import KeyboardSession
from host.simulator import DemoTransport

FIXTURE = json.loads((Path(__file__).parent / "fixtures/auto-fit.json").read_text(encoding="utf-8"))


class RecordingTransport(DemoTransport):
    def __init__(self, record):
        super().__init__()
        self.commands = []
        self.profile["keys"][record["key"]] = deepcopy(record)

    def _command(self, op, payload):
        self.commands.append(op)
        return super()._command(op, payload)


def setup(write=True):
    record = deepcopy(FIXTURE["valid"][0]["input"])
    transport = RecordingTransport(record["keyConfig"])
    client = KeyboardClient(transport, write_enabled=write)
    session = KeyboardSession(client=client)
    registered = {}

    class Server:
        def tool(self, **kwargs):
            def decorate(fn):
                registered[fn.__name__] = fn
                return fn
            return decorate

    register_auto_fit_tools(Server(), session, {}, {}, lambda fn: fn)
    return session, transport, record, registered


def feed(state, session, record):
    for phase, rows in record["phases"].items():
        def capture(*args, **kwargs):
            return dict(timing_usable=True, configuration_changed=False, issues=[], counts={},
                        samples=[dict(estimated_sample_ms=row["timeMs"], raw=row["raw"], sequence=row["sequence"],
                                      pressed=row["pressed"]) for row in rows])
        state.capture(session, phase, capture=capture)


class AutoFitTests(unittest.TestCase):
    def test_studio_python_parity(self):
        for case in FIXTURE["valid"]:
            with self.subTest(case=case["name"]):
                result = analyze_auto_fit(case["input"])
                expected = case["expected"]
                self.assertTrue(result["ready"], result["reasons"])
                self.assertEqual(result["recommendation"]["config"], expected["config"])
                self.assertEqual(result["recommendation"]["metrics"], expected["metrics"])
                self.assertEqual(result["evidence"], expected["evidence"])
                self.assertEqual(result["baseline"]["metrics"], expected["baselineMetrics"])
                self.assertEqual(result["observedMetrics"], expected["observedMetrics"])

    def test_rejects_bad_evidence(self):
        for change in ("duplicate", "nonfinite", "oversize", "wrong_key", "mismatched_key"):
            value = deepcopy(FIXTURE["valid"][0]["input"])
            if change == "duplicate":
                value["phases"]["released"][1]["sequence"] = value["phases"]["released"][0]["sequence"]
            elif change == "nonfinite":
                value["phases"]["released"][1]["timeMs"] = float("nan")
            elif change == "oversize":
                value["phases"]["released"] *= 30
            elif change == "wrong_key":
                value["keyConfig"]["key"] = 14
            else:
                value["key"] = (value["keyConfig"]["key"] + 1) % 14
            self.assertFalse(analyze_auto_fit(value)["ready"], change)

    def test_readonly_capture_and_explicit_apply(self):
        session, transport, record, tools = setup(False)
        tools["keyboard_autofit_begin"](record["keyConfig"]["key"], 3.4)
        feed(session.auto_fit, session, record)
        self.assertTrue(tools["keyboard_autofit_recommend"]()["result"]["ready"])
        self.assertFalse(any(op in MUTATIONS for op in transport.commands))
        with self.assertRaises(PermissionError):
            tools["keyboard_autofit_apply"]()
        tools["keyboard_autofit_cancel"]()
        session.require_idle()
        self.assertIsNone(session.auto_fit)

    def test_readonly_enabled_output_requires_permission(self):
        session, transport, record, tools = setup(False)
        transport.enabled = True
        with self.assertRaises(PermissionError):
            tools["keyboard_autofit_begin"](record["keyConfig"]["key"], 3.4)
        self.assertFalse(any(op in MUTATIONS for op in transport.commands))

    def test_apply_preserves_profile_then_retest_reports_observed_errors(self):
        session, transport, record, tools = setup()
        key = record["keyConfig"]["key"]
        original = deepcopy(transport.profile)
        tools["keyboard_autofit_begin"](key, 3.4)
        self.assertEqual(session.travel_mm[key], 3.4)
        self.assertTrue(all(value == 4 for index, value in enumerate(session.travel_mm) if index != key))
        with self.assertRaises(DeviceError):
            session.require_idle()
        feed(session.auto_fit, session, record)
        result = tools["keyboard_autofit_recommend"]()["result"]
        outcome = tools["keyboard_autofit_apply"]()
        self.assertTrue(outcome["complete"])
        expected = deepcopy(original)
        expected["keys"][key] = result["recommendation"]["config"]
        self.assertEqual(transport.profile, expected)
        self.assertNotIn(SAVE, transport.commands)
        session.require_idle()
        tools["keyboard_autofit_begin"](key, 3.4, retest=True)
        feed(session.auto_fit, session, record)
        for row in session.auto_fit.input["phases"]["holding"]:
            row["pressed"] = False
        retest = tools["keyboard_autofit_recommend"]()
        self.assertTrue(retest["verification"]["needsRetry"])
        self.assertGreater(retest["verification"]["observedMetrics"]["holdDrops"], 0)
        self.assertTrue(retest["active"])
        new_record = deepcopy(record)
        for rows in new_record["phases"].values():
            model = SampledRT(expected["keys"][key])
            for row in rows:
                row["pressed"] = model.step(row["raw"])
        feed(session.auto_fit, session, new_record)
        successful = tools["keyboard_autofit_recommend"]()
        self.assertFalse(successful["verification"]["needsRetry"])
        self.assertFalse(successful["active"])

    def test_stale_config_blocks_apply_before_writes(self):
        session, transport, record, tools = setup()
        tools["keyboard_autofit_begin"](record["keyConfig"]["key"], 3.4)
        feed(session.auto_fit, session, record)
        tools["keyboard_autofit_recommend"]()
        transport.profile["keys"][2]["actuation"] += 1
        before = len(transport.commands)
        with self.assertRaises(DeviceError):
            tools["keyboard_autofit_apply"]()
        self.assertNotIn(SET_KEY, transport.commands[before:])

    def test_disconnect_discards_fit(self):
        session, transport, record, tools = setup()
        tools["keyboard_autofit_begin"](record["keyConfig"]["key"], 3.4)
        session.disconnect()
        self.assertFalse(session.status()["auto_fit_active"])

    def test_bad_capture_does_not_mark_phase_complete(self):
        session, transport, record, tools = setup()
        tools["keyboard_autofit_begin"](record["keyConfig"]["key"], 3.4)
        with self.assertRaises(ValueError):
            tools["keyboard_autofit_capture"]("holding")
        def invalid(*args, **kwargs):
            return dict(timing_usable=True, configuration_changed=False, issues=[], counts={"stale": 1})
        with self.assertRaises(DeviceError):
            session.auto_fit.capture(session, "released", capture=invalid)
        self.assertEqual(session.auto_fit.snapshot()["phases"], {})

    def test_output_enabled_during_capture_is_rejected(self):
        from host.mcp_diagnostics import capture_key_timing
        session, transport, record, tools = setup()
        transport.enabled = True
        with self.assertRaisesRegex(DeviceError, "Output became enabled"):
            capture_key_timing(session.client, record["keyConfig"]["key"], .2, 100, True, require_output_off=True)

    def test_import_analysis_is_offline_and_cannot_apply(self):
        session, transport, record, tools = setup()
        exported = {"schema": "split-beat-auto-fit-v1", **record}
        self.assertTrue(tools["keyboard_autofit_analyze"](exported)["ready"])
        self.assertEqual(transport.commands, [])
        with self.assertRaises(DeviceError):
            tools["keyboard_autofit_apply"]()


if __name__ == "__main__":
    unittest.main()
