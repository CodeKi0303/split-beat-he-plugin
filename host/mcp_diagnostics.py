"""Bounded read-only measurements for MCP; host snapshots are not ADC streams."""
from __future__ import annotations

import math
import statistics
import time
from typing import Any

from pydantic import StrictInt, StrictBool, StrictFloat

from .client import DeviceError
from .performance import (PERFORMANCE, LINK_PERFORMANCE, LINK_FIELDS, ADC_DIAGNOSTICS,
                          SCHEDULER_DIAGNOSTICS, decode_scheduler_diagnostics,
                          ADC_TRACE, encode_adc_trace_keys, decode_adc_trace,
                          CPU_PROFILE, decode_cpu_profile_begin, decode_cpu_profile_page,
                          ADC_IRQ_PROFILE, decode_adc_irq_profile_begin, decode_adc_irq_profile_page,
                          decode_performance, decode_link_performance, decode_adc_diagnostics,
                          compare_samples, delta32)
from .protocol import integer, CAP_ADC_TRACE, CAP_CPU_PROFILE, CAP_ADC_IRQ_PROFILE, CAP_KEY_CHORD
from .hall_curve import normalized_to_mm, raw_to_estimated_mm


def duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not .2 <= value <= 10:
        raise ValueError("duration_seconds must be 0.2..10")
    return float(value)


def capture_sensors(client, duration_seconds=2.0, sample_hz=20, include_samples=False,
                    *, clock=time.monotonic, delay=time.sleep, total_travel_mm=None):
    seconds = duration(duration_seconds)
    integer(sample_hz, 5, 50, "sample_hz")
    if type(include_samples) is not bool:
        raise ValueError("include_samples must be boolean")
    info_before, config = None, None
    if total_travel_mm is not None:
        from .mcp_configuration import validate_travel
        total_travel_mm = validate_travel(total_travel_mm)
        info_before = client.info()
        config = client.get_config()
    rows = [[] for _ in range(14)]
    sequences = [None, None]
    snapshots, start = [], clock()
    samples = [0, 0]
    # Finite loop bounds also prevent an invalid/mock clock from hanging a tool.
    iterations = min(501, int(seconds * sample_hz) + 1)
    for step in range(iterations):
        target = start + step / sample_hz
        remaining = target - clock()
        if remaining > 0:
            delay(remaining)
        stamp = clock() - start
        statuses = [client.status(side) for side in (0, 1)]
        for side, status in enumerate(statuses):
            if not status["fresh"]:
                raise DeviceError(f"Side {side} sensor data became stale; measurement rejected")
            sequence = status["sample_sequence"]
            if sequence == sequences[side]:
                continue
            if sequences[side] is not None and delta32(sequence, sequences[side]) > 0x7fffffff:
                raise DeviceError("Sensor sequence moved backwards; reconnect/reset invalidates this window")
            sequences[side] = sequence
            samples[side] += 1
            for offset, value in enumerate(status["raw"]):
                rows[side * 7 + offset].append(value)
        if include_samples:
            row = {"t_ms": round(stamp * 1000, 3),
                   "raw": statuses[0]["raw"] + statuses[1]["raw"],
                   "travel": statuses[0]["travel"] + statuses[1]["travel"]}
            if total_travel_mm is not None:
                # STATUS travel already includes the selected firmware curve.
                row["travel_mm"] = [normalized_to_mm(t, total_travel_mm[k]) for k, t in enumerate(row["travel"])]
                row["raw_estimated_mm"] = [raw_to_estimated_mm(raw, config["keys"][k], total_travel_mm[k]) for k, raw in enumerate(row["raw"])]
            snapshots.append(row)
        if clock() - start >= seconds:
            break
    if min(samples) < 2:
        raise DeviceError("Not enough distinct fresh sensor snapshots")
    if info_before is not None:
        after = client.info()
        if any(info_before.get(field) != after.get(field) for field in ("board_rev", "generation", "config_crc")):
            raise DeviceError("Configuration changed during sensor capture; distance estimates rejected")
    stats = []
    for key, values in enumerate(rows):
        stats.append({"key": key, "count": len(values), "mean": statistics.fmean(values),
                      "variance": statistics.pvariance(values), "stdev": statistics.pstdev(values),
                      "min": min(values), "max": max(values), "peak_to_peak": max(values)-min(values)})
    result = {"demo": bool(client.transport.demo), "duration_seconds": clock()-start,
              "requested_snapshot_hz_per_side": sample_hz, "fresh_snapshots_per_side": samples,
              "statistics": stats,
              "limits": "Host-polled snapshots only, not full-rate ADC data or calibrated physical noise accuracy. No settings/output changes."}
    if include_samples:
        result["samples"] = snapshots
    if config is not None:
        result.update(total_travel_mm=list(total_travel_mm),
                      curve_ids=[k.get("curve_id", 0) for k in config["keys"]],
                      distance_note="STATUS travel is curve-corrected by firmware and scaled once. raw_estimated_mm uses the same curve on raw snapshots; filtering can differ. Both are approximate, not measured physical mm.")
    return result


def capture_key_timing(client, key, duration_seconds=5.0, sample_hz=100, include_samples=True,
                       *, clock=time.monotonic, delay=time.sleep, total_travel_mm=None,
                       require_output_off=False):
    """Observe one side only; state changes are interval-censored between polls."""
    integer(key, 0, 13, "key")
    seconds = duration(duration_seconds)
    integer(sample_hz, 5, 100, "sample_hz")
    if type(include_samples) is not bool:
        raise ValueError("include_samples must be boolean")
    side, offset = divmod(key, 7)
    before = client.info()
    key_config = None
    if total_travel_mm is not None:
        normalized_to_mm(0, total_travel_mm)  # validate metadata before capture
        key_config = client.get_config()["keys"][key]
    start = clock()
    rows, edges, holds, issues = [], [], [], []
    counts = dict(duplicates=0, stale=0, resets=0, gaps=0, invalid=0)
    sequence, previous, open_press, segment = None, None, None, 0
    maximum_gap_ms = max(80.0, 2500.0 / sample_hz)
    last_host_time = start
    for step in range(min(1001, int(seconds * sample_hz) + 1)):
        target = start + step / sample_hz
        now = clock()
        if now < last_host_time:
            raise DeviceError("Host clock moved backwards; timing capture rejected")
        if target > now:
            delay(target - now)
        request_time = clock()
        status = client.status(side)
        if require_output_off and status.get("output_enabled") is not False:
            raise DeviceError("Output became enabled during automatic fitting; capture rejected")
        response_time = clock()
        if response_time < request_time or request_time < last_host_time:
            raise DeviceError("Host clock moved backwards; timing capture rejected")
        last_host_time = response_time
        reason = None
        if status.get("fresh") is not True or status.get("calibration_state", 0) != 0:
            counts["stale"] += 1
            reason = "stale_or_calibrating"
        try:
            seq = integer(status.get("sample_sequence"), 0, 0xffffffff, "sample_sequence")
            age = integer(status.get("link_age_us"), 0, 0xffffffff, "link_age_us")
            raw = integer(status["raw"][offset], 0, 4095, "raw")
            travel = integer(status["travel"][offset], 0, 1000, "travel")
            mask = integer(status.get("pressed_mask"), 0, 0xffffffff, "pressed_mask")
        except (ValueError, IndexError, KeyError, TypeError):
            counts["invalid"] += 1
            reason = "invalid_telemetry"
        if reason is None and age > 5000:
            counts["stale"] += 1
            reason = "source_age_over_5ms"
        if reason is not None:
            segment += 1
            previous, open_press = None, None
            if len(issues) < 100:
                issues.append(dict(reason=reason, response_ms=(response_time-start)*1000))
        else:
            duplicate = sequence is not None and seq == sequence
            if duplicate:
                counts["duplicates"] += 1
            else:
                reset = sequence is not None and delta32(seq, sequence) >= 0x80000000
                request_ms, response_ms = (request_time-start)*1000, (response_time-start)*1000
                midpoint = (request_ms + response_ms) / 2
                estimated = midpoint - age / 1000
                gap = previous is not None and (estimated - previous["estimated_sample_ms"] > maximum_gap_ms
                      or response_ms - previous["response_ms"] > maximum_gap_ms)
                regression = previous is not None and estimated <= previous["estimated_sample_ms"]
                if reset or gap or regression:
                    counts["resets" if reset else "gaps" if gap else "invalid"] += 1
                    segment += 1
                    previous, open_press = None, None
                    if len(issues) < 100:
                        issues.append(dict(reason="sequence_reset" if reset else "sample_gap" if gap else "sample_clock_regression",
                                           response_ms=response_ms))
                row = dict(request_ms=request_ms, response_ms=response_ms, midpoint_ms=midpoint,
                           estimated_sample_ms=estimated, reported_age_us=age,
                           earliest_ms=request_ms-age/1000, latest_ms=response_ms,
                           sequence=seq, raw=raw, travel=travel,
                           pressed=bool(mask & (1 << key)), segment=segment)
                if key_config is not None:
                    row["travel_mm"] = normalized_to_mm(travel, total_travel_mm)
                    row["raw_estimated_mm"] = raw_to_estimated_mm(raw, key_config, total_travel_mm)
                if previous is not None and row["pressed"] != previous["pressed"]:
                    event = dict(type="press" if row["pressed"] else "release",
                                 time_ms=(previous["estimated_sample_ms"]+estimated)/2,
                                 earliest_ms=previous["earliest_ms"], latest_ms=row["latest_ms"],
                                 segment=segment,
                                 travel_range=[previous["travel"], travel])
                    event["uncertainty_ms"] = max(event["time_ms"]-event["earliest_ms"], event["latest_ms"]-event["time_ms"])
                    edges.append(event)
                    if event["type"] == "press":
                        open_press = event
                    elif open_press is not None:
                        holds.append(dict(press_ms=open_press["time_ms"], release_ms=event["time_ms"],
                                          duration_ms=event["time_ms"]-open_press["time_ms"],
                                          minimum_ms=max(0.0, event["earliest_ms"]-open_press["latest_ms"]),
                                          maximum_ms=event["latest_ms"]-open_press["earliest_ms"], segment=segment))
                        open_press = None
                rows.append(row)
                sequence, previous = seq, row
        if response_time-start >= seconds:
            break
    after = client.info()
    changed = any(before.get(field) != after.get(field) for field in ("board_rev", "generation", "config_crc"))
    intervals = [b["estimated_sample_ms"]-a["estimated_sample_ms"] for a, b in zip(rows, rows[1:])
                 if a["segment"] == b["segment"]]
    result = dict(key=key, side=side, demo=bool(client.transport.demo), duration_seconds=last_host_time-start,
                  requested_snapshot_hz=sample_hz, fresh_snapshots=len(rows),
                  observed_snapshot_hz=1000/statistics.fmean(intervals) if intervals else None,
                  maximum_join_gap_ms=maximum_gap_ms, configuration_changed=changed,
                  timing_usable=bool(len(rows) >= 2 and not changed),
                  press_count=sum(e["type"] == "press" for e in edges),
                  release_count=sum(e["type"] == "release" for e in edges),
                  edges=edges, complete_holds=holds, counts=counts, issues=issues,
                  output_changed=False, settings_changed=False,
                  limits="Host snapshots and interval-censored edges only; short transitions between polls can be missed. Right source age refers to left-side receipt, not the remote ADC instant. No audio reference or physical latency measurement; use the browser rhythm lab for sound comparison.")
    if include_samples:
        result["samples"] = rows
    if key_config is not None:
        result.update(total_travel_mm=total_travel_mm,
                      curve_id=key_config.get("curve_id", 0),
                      key_config=key_config,
                      distance_note="Firmware STATUS travel is already curve-corrected; travel_mm scales it once. raw_estimated_mm is an approximate raw-snapshot estimate. Proportional stroke scaling is not physical distance verification.")
    return result


def capability_summary(info):
    caps = info["capabilities"]
    version = (info["major"], info["minor"], info["patch"])
    return {"mouse_button_mapping": bool(caps & 0x20),
            "keyboard_chord_mapping": bool(caps & CAP_KEY_CHORD),
            "bottom_deadzone": bool(caps & 0x100),
            "nonlinear_hall_curve": bool(caps & 0x200),
            "firmware_update_protocol": bool(caps & 0x40),
            "device_performance_counters": bool(caps & 0x80),
            "link_performance_counters": bool(caps & 0x80) and version >= (0, 3, 2),
            "adc_diagnostics": (bool(caps & 0x80) and version >= (0, 3, 8)
                                and info.get("board_rev") == 86 and info.get("role") == 0
                                and not info.get("demo", False)),
            "scheduler_diagnostics": (bool(caps & 0x80) and version >= (0, 3, 9)
                                      and info.get("board_rev") == 86 and info.get("role") == 0
                                      and not info.get("demo", False)),
            "adc_trace": (bool(caps & CAP_ADC_TRACE) and info.get("board_rev") == 86
                          and info.get("role") == 0 and not info.get("demo", False)),
            "cpu_profile": (bool(caps & CAP_CPU_PROFILE) and info.get("board_rev") == 86
                            and info.get("role") == 0 and not info.get("demo", False)),
            "adc_irq_profile": (bool(caps & CAP_ADC_IRQ_PROFILE) and info.get("board_rev") == 86
                                and info.get("role") == 0 and not info.get("demo", False)),
            "nkro": True, "hall_keys": 14, "mapping_inputs": 28,
            "firmware_update_note": "App capability does not establish installed bootloader compatibility."}


def measure_counters(client, seconds=2.0, link=False, *, clock=time.monotonic, delay=time.sleep):
    seconds = duration(seconds)
    info = client.info()
    capability = "link_performance_counters" if link else "device_performance_counters"
    if not capability_summary(info)[capability]:
        return {"supported": False, "demo": bool(client.transport.demo),
                "reason": "This firmware/demo does not expose these counters; no measurement command sent."}
    opcode = LINK_PERFORMANCE if link else PERFORMANCE
    decode = decode_link_performance if link else decode_performance
    start = clock()
    first = decode(client.request(opcode))
    delay(seconds)
    last = decode(client.request(opcode))
    elapsed_host = clock() - start
    if not link:
        measured = compare_samples(first, last, elapsed_host)
    else:
        elapsed = delta32(last["micros"], first["micros"]) / 1_000_000
        if elapsed <= 0 or not .5 * elapsed_host <= elapsed <= 2 * elapsed_host:
            raise ValueError("Link counters span a possible reset/disconnection")
        deltas = {name: delta32(last[name], first[name]) for name in LINK_FIELDS[1:11]}
        measured = {"device_seconds": elapsed, "host_seconds": elapsed_host, "deltas": deltas,
                    "poll_hz": deltas["polls"] / elapsed,
                    "accepted_hz": deltas["accepted"] / elapsed,
                    "lifetime_rtt_min_us": last["rtt_min_us"], "lifetime_rtt_max_us": last["rtt_max_us"],
                    "limits": "Device counters only; RTT extrema are lifetime values. This is not a Windows USB receive-rate or wire-latency measurement."}
    # An acquisition fault may have stopped the ADC before this window. Zero
    # error deltas must not hide a nonzero boot-lifetime failure count.
    return {"supported": True, "demo": bool(client.transport.demo), **measured,
            "counters_before": first, "counters_after": last}


def read_adc_diagnostics(client):
    """Read one stop snapshot; never clear, restart or otherwise change acquisition."""
    info = client.info()
    unchanged = {"changed_device_settings": False, "changed_output": False,
                 "changed_firmware": False}
    demo = bool(client.transport.demo)
    if demo or not capability_summary(info)["adc_diagnostics"]:
        return {"supported": False, "demo": demo, **unchanged,
                "reason": "Requires non-demo C86 left firmware v0.3.8+ with performance counters; no ADC diagnostics command sent."}
    snapshot = decode_adc_diagnostics(client.request(ADC_DIAGNOSTICS, b""))
    return {"supported": True, "demo": False, **unchanged, **snapshot,
            "limits": "Read-only ADC state and boot-lifetime counters with latched stop evidence. stop_phase_cycles is the unsigned cycle difference since the expected trigger, or since the slower bank's last serviced DMA boundary in independent DMA builds. Batched profiles use the final trigger in the batch. This field can wrap and is not an absolute latency measurement. The command leaves acquisition and settings as-is."}


def read_scheduler_diagnostics(client):
    """One bounded observation, including CC/link state even if a source is stale."""
    info = client.info()
    unchanged = {"changed_device_settings": False, "changed_output": False,
                 "changed_firmware": False}
    demo = bool(client.transport.demo)
    if demo or not capability_summary(info)["scheduler_diagnostics"]:
        return {"supported": False, "demo": demo, **unchanged,
                "reason": "Requires non-demo C86 left firmware v0.3.9+ with performance counters; no scheduler command sent."}
    snapshot = decode_scheduler_diagnostics(client.request(SCHEDULER_DIAGNOSTICS, b""))
    return {"supported": True, "demo": False, **unchanged, **snapshot,
            "limits": "Device observations, not CPU utilization or input latency. IRQ durations include higher-priority preemption. Main-loop gap and IRQ maxima are lifetime values; counters wrap at 32 bits. Local/remote updates count newly consumed source frames, not ADC triggers. Link and CC snapshots are diagnostic observations, not an atomic electrical measurement. No reset or mutation."}


def read_adc_trace(client, keys):
    """One bounded trace request after INFO; never enable tracing via a write."""
    payload = encode_adc_trace_keys(keys)
    info = client.info()
    unchanged = {"changed_device_settings": False, "changed_output": False,
                 "changed_firmware": False}
    demo = bool(client.transport.demo)
    if demo or not capability_summary(info)["adc_trace"]:
        return {"supported": False, "demo": demo, **unchanged,
                "reason": "Requires non-demo C86 left firmware with ADC trace capability 0x400; no trace command sent."}
    snapshot = decode_adc_trace(client.request(ADC_TRACE, payload))
    if snapshot["keys"] != keys:
        raise DeviceError("ADC trace returned keys different from the requested order")
    for row in snapshot["rows"]:
        expected = (sum(row["raw_history"]) + 4) // 8
        row.update(expected_rounded_average=expected,
                   average_matches_history=row["averaged"] == expected)
    return {"supported": True, "demo": False, **unchanged, **snapshot,
            "firmware_version": f'{info["major"]}.{info["minor"]}.{info["patch"]}',
            "config_generation": info.get("generation"), "config_crc": info.get("config_crc"),
            "history_order": "oldest_to_newest", "trace_requests": 1,
            "limits": "One synchronous device bundle for two local Hall keys, not a continuous ADC stream. Firmware requires output off and quiet storage/calibration; refusal is returned without disabling output, saving or retrying. Reading diagnostics can perturb scheduling. Raw history, averaged ADC, ADC after deadband and normalized travel are distinct stages; travel is not measured physical distance. No settings, output or firmware changes."}


def read_cpu_profile(client, *, delay=time.sleep):
    """One fixed-duration diagnostic run, serialized through the final page."""
    unchanged = {"changed_device_settings": False, "changed_output": False,
                 "changed_firmware": False}
    with client.lock:
        info = client.info()
        demo = bool(client.transport.demo)
        if demo or not capability_summary(info)["cpu_profile"]:
            return {"supported": False, "demo": demo, **unchanged,
                    "reason": "Requires non-demo C86 left firmware with CPU profile capability 0x800; no profiling command sent."}
        run_id = decode_cpu_profile_begin(client.request(CPU_PROFILE, b"\x00"))
        # The fixed 250ms window begins after its acknowledgement drains. Do not
        # poll the USB vendor interface during collection or retry BUSY/NOT_READY.
        delay(.35)
        pages = []
        for page in range(5):
            decoded = decode_cpu_profile_page(client.request(CPU_PROFILE, bytes((1, page))), page)
            if decoded["run_id"] != run_id:
                raise DeviceError("CPU profile run_id changed between begin and result pages")
            pages.append(decoded)
    summary = pages[0]
    seconds = summary["elapsed_cycles"] / summary["cpu_hz"]
    stages = []
    for page in pages[1:]:
        for row in page["rows"]:
            timing = row["id"] != 11
            stages.append({**row,
                "group": "foreground" if row["id"] < 5 else "irq" if row["id"] < 9 else "nested" if timing else "call_counter",
                "timing_available": timing, "calls_per_second": row["calls"] / seconds,
                "total_us": row["total_cycles"] * 1_000_000 / summary["cpu_hz"] if timing else None,
                "mean_us": row["total_cycles"] * 1_000_000 / summary["cpu_hz"] / row["calls"] if timing and row["calls"] else None,
                "max_us": row["max_cycles"] * 1_000_000 / summary["cpu_hz"] if timing else None,
                "percent_elapsed": row["total_cycles"] * 100 / summary["elapsed_cycles"] if timing else None})
    return {"supported": True, "demo": False, **unchanged, **summary, "stages": stages,
            "device_elapsed_seconds": seconds, "requested_window_seconds": .25,
            "host_wait_seconds": .35, "profile_runs": 1,
            "report_publication_hz": summary["report_publications"] / seconds,
            "usb_completion_hz": summary["usb_completions"] / seconds,
            "main_loop_hz": summary["main_loop_count"] / seconds,
            "irq_body_us": summary["irq_cycles"] * 1_000_000 / summary["cpu_hz"],
            "irq_body_percent_elapsed": summary["irq_cycles"] * 100 / summary["elapsed_cycles"],
            "firmware_version": f'{info["major"]}.{info["minor"]}.{info["patch"]}',
            "config_generation": info.get("generation"), "config_crc": info.get("config_crc"),
            "limits": "One diagnostic 250ms device window with begin and five result pages, not continuous tracing. Adjusted cycles exclude completed instrumented higher-priority IRQ bodies but retain unwrapped overhead, exception entry/return and observer cost. IRQ bodies and foreground stages are distinct; nonlinear/linear miss timings are nested within foreground work and must not be added to stages. RT total is a call counter with no timing measurement. Percentages are shares of elapsed cycles, not a generic CPU utilization or input-latency measurement. No automatic retry, output change, settings write or firmware update; instrumentation can affect scheduling."}


def read_adc_irq_profile(client, *, delay=time.sleep):
    """One narrow ADC IRQ diagnostic run; no automatic retry or state changes."""
    unchanged = {"changed_device_settings": False, "changed_output": False,
                 "changed_firmware": False}
    with client.lock:
        info = client.info()
        demo = bool(client.transport.demo)
        if demo or not capability_summary(info)["adc_irq_profile"]:
            return {"supported": False, "demo": demo, **unchanged,
                    "reason": "Requires non-demo C86 left firmware with ADC IRQ profile capability 0x1000; no profiling command sent."}
        run_id = decode_adc_irq_profile_begin(client.request(ADC_IRQ_PROFILE, b"\x00"))
        # Fixed500ms collection, plus at most50ms ACK drain. Leave the vendor
        # channel quiet throughout collection; BUSY/NOT_READY is not retried.
        delay(.60)
        pages = []
        for page in range(3):
            decoded = decode_adc_irq_profile_page(client.request(ADC_IRQ_PROFILE, bytes((1, page))), page)
            if decoded["run_id"] != run_id:
                raise DeviceError("ADC IRQ profile run_id changed between begin and result pages")
            pages.append(decoded)
    summary = pages[0]
    seconds = summary["elapsed_cycles"] / summary["cpu_hz"]
    stages = []
    for page in pages[1:]:
        for row in page["rows"]:
            if row["calls"] != summary["profiled_samples"]:
                raise DeviceError("ADC IRQ profile stage calls differ from profiled_samples")
            stages.append({**row,
                "group": "total" if row["id"] == 0 else "partition",
                "total_us": row["total_cycles"] * 1_000_000 / summary["cpu_hz"],
                "mean_us": row["total_cycles"] * 1_000_000 / summary["cpu_hz"] / row["calls"] if row["calls"] else None,
                "max_us": row["max_cycles"] * 1_000_000 / summary["cpu_hz"]})
    partition_sum = sum(stage["total_cycles"] for stage in stages[1:])
    partition_difference = partition_sum - stages[0]["total_cycles"]
    return {"supported": True, "demo": False, **unchanged, **summary, "stages": stages,
            "device_elapsed_seconds": seconds, "requested_window_seconds": .5,
            "host_wait_seconds": .6, "profile_runs": 1,
            "report_publication_hz": summary["report_publications"] / seconds,
            "usb_completion_hz": summary["usb_completions"] / seconds,
            "adc_scan_hz": summary["adc_sequence_delta"] / seconds,
            "partition_sum_cycles": partition_sum,
            "partition_difference_cycles": partition_difference,
            "partition_matches": partition_difference == 0,
            "firmware_version": f'{info["major"]}.{info["minor"]}.{info["patch"]}',
            "config_generation": info.get("generation"), "config_crc": info.get("config_crc"),
            "limits": "One fixed500ms ADC IRQ diagnostic window. A sampled IRQ body total and its five segments overlap by definition: do not add total to its partitions. The body excludes exception entry/exit and the sample recorder. Times describe instrumented accepted samples, not all IRQs or real CPU occupancy; discarded samples and observer/preemption effects must be considered. Segment maxima need not add to total maximum. Raw cycle totals and any partition mismatch are retained. No automatic retry, output change, settings write or firmware update."}


def register_diagnostic_tools(server, session, read, write, guard):
    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_health() -> dict[str, Any]:
        """Read board capabilities, source freshness, calibration coverage and output/storage state. Does not change settings."""
        client = session.require_client()
        info, config = client.info(), client.get_config()
        sides = [client.status(side) for side in (0, 1)]
        return {"info": info, "capabilities": capability_summary(info), "sides": sides,
                "uncalibrated_enabled_keys": [k["key"] for k in config["keys"] if k["flags"] & 1 and not k["flags"] & 4],
                "calibrated_keys": [k["key"] for k in config["keys"] if k["flags"] & 4],
                "all_sources_fresh": all(s["fresh"] for s in sides),
                "session": session.status()}

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_capture_sensors(duration_seconds: StrictFloat = 2.0, sample_hz: StrictInt = 20,
                                 include_samples: StrictBool = False) -> dict[str, Any]:
        """Read raw Hall mean/variance/min/max for all14 keys over0.2..10seconds at5..50host snapshots/s. Optional bounded raw/travel samples. Not the fullADC stream; no output writes."""
        session.require_idle()
        return capture_sensors(session.require_client(), duration_seconds, sample_hz, include_samples,
                               total_travel_mm=session.travel_mm)

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_capture_key_timing(key: StrictInt, duration_seconds: StrictFloat = 5.0,
                                    sample_hz: StrictInt = 100, include_samples: StrictBool = True) -> dict[str, Any]:
        """Read selected Hall key raw/travel/actual pressed state for0.2..10s at5..100host snapshots/s. Returns request/response and source-age timing, observed edge brackets and complete hold-duration bounds. Stale/reset/gap samples never join edges. Read-only; no audio, output or settings changes. Use browser rhythm lab for BPM/audio comparison."""
        session.require_idle()
        integer(key, 0, 13, "key")
        return capture_key_timing(session.require_client(), key, duration_seconds, sample_hz, include_samples,
                                  total_travel_mm=session.travel_mm[key])

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_measure_performance(duration_seconds: StrictFloat = 2.0) -> dict[str, Any]:
        """Measure device USB completion/ADC counters for0.2..10seconds. No mutations. Does not measure host Windows delivery or actual switch latency."""
        session.require_idle()
        return measure_counters(session.require_client(), duration_seconds)

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_measure_link(duration_seconds: StrictFloat = 2.0) -> dict[str, Any]:
        """Measure accepted RS485 polls and error deltas for0.2..10seconds. Returns lifetimeRTT bounds separately; does not change output or firmware."""
        session.require_idle()
        return measure_counters(session.require_client(), duration_seconds, link=True)

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_adc_diagnostics() -> dict[str, Any]:
        """Read C86 left v0.3.8+ ADC running state, lifetime faults and latched stop registers. No reset/restart, settings, output or firmware changes. Unsupported/demo firmware receives no diagnostics request."""
        session.require_idle()
        return read_adc_diagnostics(session.require_client())

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_scheduler_diagnostics() -> dict[str, Any]:
        """Read C86 left v0.3.9+ main-loop/IRQ timing, consumed frames and CC/link state, including stale-source failures. No settings, output or firmware changes."""
        session.require_idle()
        return read_scheduler_diagnostics(session.require_client())

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_adc_trace(keys: list[StrictInt]) -> dict[str, Any]:
        """Read one synchronous 8-scan history/filter/deadband/travel bundle for exactly two distinct local Hall keys0..6. Requires non-demo C86 left capability0x400 and output already off. INFO then one trace request; no output/settings changes or automatic retries."""
        session.require_idle()
        return read_adc_trace(session.require_client(), keys)

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_cpu_profile() -> dict[str, Any]:
        """Collect one fixed250ms CPU-stage diagnostic run on non-demo C86 left capability0x800. INFO then begin,350ms quiet wait and five pages under one client lock. Requires firmware readiness; no output/config/firmware changes, polling or retry. Nested curve timings are not additive CPU utilization."""
        session.require_idle()
        return read_cpu_profile(session.require_client())

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_adc_irq_profile() -> dict[str, Any]:
        """Collect one fixed500ms ADC IRQ stage sample on non-demo C86 left capability0x1000. INFO then begin,600ms quiet wait and three result pages under one lock. No output/config/firmware changes or retry. Total overlaps its five partitions; timings are not real CPU occupancy."""
        session.require_idle()
        return read_adc_irq_profile(session.require_client())

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_interference_baseline(duration_seconds: StrictFloat = 2.0) -> dict[str, Any]:
        """Capture a quiet all-keys-released baseline for a later interference comparison. User must release keys first. Host memory only; no device mutations."""
        session.require_idle()
        client = session.require_client()
        # A failed replacement capture must not leave an older baseline usable.
        session.interference = None
        before = client.info()
        measured = capture_sensors(client, duration_seconds, 20)
        after = client.info()
        if (before["generation"], before["config_crc"]) != (after["generation"], after["config_crc"]):
            raise DeviceError("Settings changed during baseline; capture again")
        if any(k["peak_to_peak"] > 64 for k in measured["statistics"]):
            raise DeviceError("Baseline movement exceeds64ADC; release all keys and capture again")
        session.interference = {"created": time.monotonic(), "info": after, "capture": measured}
        return {"baseline_ready": True, **measured,
                "next": "Hold only the chosen Hall key, keep other keys released, then call keyboard_interference_compare within5minutes."}

    @server.tool(annotations=read, structured_output=True)
    @guard
    def keyboard_interference_compare(key: StrictInt, duration_seconds: StrictFloat = 2.0) -> dict[str, Any]:
        """Compare held-key snapshots against the released baseline. Reports changes in the other13 sensors, not proof of magnetic/electrical causality. User controls the physical press."""
        integer(key, 0, 13, "key")
        session.require_idle()
        client, baseline = session.require_client(), session.interference
        if baseline is None or time.monotonic()-baseline["created"] > 300:
            raise DeviceError("Capture a fresh released baseline first")
        current = client.info()
        if any(current[name] != baseline["info"][name] for name in ("generation", "config_crc", "board_rev")):
            session.interference = None
            raise DeviceError("Settings/board changed since baseline; capture again")
        measured = capture_sensors(client, duration_seconds, 20)
        after = client.info()
        if any(after[name] != current[name] for name in ("generation", "config_crc", "board_rev")):
            session.interference = None
            raise DeviceError("Settings changed during comparison; capture again")
        before = baseline["capture"]["statistics"]
        delta = measured["statistics"][key]["mean"] - before[key]["mean"]
        threshold = max(32, 4 * before[key]["peak_to_peak"])
        sufficient = abs(delta) >= threshold
        rows = []
        for row in measured["statistics"]:
            other = row["key"]
            if other == key:
                continue
            difference = row["mean"] - before[other]["mean"]
            rows.append({"key": other, "mean_change_adc": difference,
                         "baseline_stdev": before[other]["stdev"],
                         "pressed_stdev": row["stdev"],
                         "relative_to_selected_percent": 100 * difference / abs(delta) if sufficient else None})
        return {"demo": bool(client.transport.demo), "selected_key": key,
                "selected_change_adc": delta, "sufficient_selected_motion": sufficient,
                "other_keys": sorted(rows, key=lambda row: abs(row["mean_change_adc"]), reverse=True),
                "limits": "Observed sensor changes only. Other physical key movement, mount flex, field interaction and electrical noise are not distinguished. Insufficient selected motion makes percentage comparison unavailable."}
