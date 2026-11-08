"""Read-only firmware counters. These are not a USB bus analyzer or ETW capture.

python -m host.performance --serial SERIAL --seconds 5
"""
import argparse
import json
import math
import struct
import time

from .client import HidTransport, KeyboardClient

PERFORMANCE = 0x40
LINK_PERFORMANCE = 0x41
ADC_DIAGNOSTICS = 0x42
SCHEDULER_DIAGNOSTICS = 0x43
ADC_TRACE = 0x44
CPU_PROFILE = 0x45
ADC_IRQ_PROFILE = 0x46
ADC_IRQ_PROFILE_FIELDS = ("run_id", "elapsed_cycles", "cpu_hz", "target_cycles",
                          "profiled_samples", "discarded_samples", "report_publications",
                          "usb_completions", "adc_sequence_delta", "reserved")
ADC_IRQ_PROFILE_STAGES = ("total", "precopy_checks", "dma_copy", "postcopy_validate_staging",
                          "hall_average", "publish_remainder")
CPU_PROFILE_FIELDS = ("run_id", "elapsed_cycles", "cpu_hz", "target_cycles",
                      "report_publications", "usb_completions", "irq_cycles", "main_loop_count")
CPU_PROFILE_STAGES = ("service", "local_engine_snapshot", "remote", "report_aux",
                      "vendor_housekeeping", "adc_irq", "split_irq", "usb_irq", "system_irq",
                      "nonlinear_miss", "linear_miss", "rt_total_calls")
SCHEDULER_FIELDS = ("micros", "main_loops", "main_loop_gap_max_us", "local_updates",
                    "remote_updates", "cc_state", "cc_error", "link_flags",
                    "rx_bytes", "uart_irq_max_cycles", "poll_irq_max_cycles",
                    "uart_irqs", "poll_irqs")
ADC_DIAGNOSTIC_FIELDS = ("running", "sample_sequence", "errors", "overruns",
                         "dma_errors", "late_irqs", "init_error", "stop_reason",
                         "stop_phase_cycles", "stop_dma_flags", "stop_ndtr1",
                         "stop_ndtr2", "scan_target_hz")
ADC_STOP_REASONS = {0: "NONE", 1: "FLAGS", 2: "EPOCH", 3: "OWNERSHIP",
                    4: "RAW_RANGE", 5: "FILTER", 6: "ADC_OVR", 7: "DMA"}
LINK_FIELDS = ("micros", "polls", "accepted", "timeouts", "sequence_errors", "late_replies", "uart_errors", "tx_busy", "tx_fail", "crc_errors", "framing_errors", "rtt_min_us", "rtt_max_us")
CPU_HZ = 240_000_000
FIELDS = ("micros", "usb_completed", "usb_published", "usb_submit_failures", "usb_stale_neutralizations", "adc_sequence", "adc_errors", "adc_overruns", "adc_late_irqs", "adc_min_period_cycles", "adc_max_period_cycles", "adc_irq_max_cycles", "scan_target_hz")


def decode_performance(payload):
    if len(payload) != 52:
        raise ValueError("Performance response must contain 13 uint32 counters (52 bytes)")
    return dict(zip(FIELDS, struct.unpack("<13I", payload)))


def decode_link_performance(payload):
    if len(payload) != 52:
        raise ValueError("Link performance response must contain 13 uint32 counters")
    return dict(zip(LINK_FIELDS, struct.unpack("<13I", payload)))


def decode_adc_diagnostics(payload):
    """Decode the latched stop evidence without interpreting its phase as latency."""
    if len(payload) != 52:
        raise ValueError("ADC diagnostics response must contain 13 uint32 fields (52 bytes)")
    result = dict(zip(ADC_DIAGNOSTIC_FIELDS, struct.unpack("<13I", payload)))
    if result["running"] not in (0, 1):
        raise ValueError("ADC diagnostics running flag must be 0 or 1")
    result["stop_reason_name"] = ADC_STOP_REASONS.get(result["stop_reason"], "UNKNOWN")
    return result


def decode_scheduler_diagnostics(payload):
    if len(payload) != 52:
        raise ValueError("Scheduler diagnostics must contain 13 uint32 fields (52 bytes)")
    result = dict(zip(SCHEDULER_FIELDS, struct.unpack("<13I", payload)))
    if result["link_flags"] & ~15:
        raise ValueError("Unknown scheduler link flags")
    return result


def encode_adc_trace_keys(keys):
    """The experimental trace selects exactly two distinct local Hall indices."""
    if not isinstance(keys, list) or len(keys) != 2:
        raise ValueError("ADC trace requires exactly two distinct local keys (0..6)")
    if any(type(key) is not int or not 0 <= key <= 6 for key in keys) or keys[0] == keys[1]:
        raise ValueError("ADC trace requires exactly two distinct local keys (0..6)")
    return bytes(keys)


def decode_adc_trace(payload):
    """Preserve a synchronous trace, including valid but inconsistent averages."""
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != 54:
        raise ValueError("ADC trace response must contain 54 bytes")
    sequence, timestamp, key_a, key_b = struct.unpack_from("<IIBB", payload)
    keys = [key_a, key_b]
    encode_adc_trace_keys(keys)
    rows = []
    for index, key in enumerate(keys):
        values = struct.unpack_from("<11H", payload, 10 + index * 22)
        if any(value > 4095 for value in values[:10]):
            raise ValueError("ADC trace raw, averaged and deadband ADC values must be 12-bit")
        packed = values[10]
        if (packed & 0x7c00) or (packed & 0x3ff) > 1000:
            raise ValueError("ADC trace travel/reserved bits are invalid")
        rows.append({"key": key, "raw_history": list(values[:8]),
                     "averaged": values[8], "after_deadband": values[9],
                     "travel": packed & 0x3ff, "pressed": bool(packed & 0x8000)})
    return {"sample_sequence": sequence, "timestamp_us": timestamp,
            "keys": keys, "rows": rows}


def decode_cpu_profile_begin(payload):
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != 4:
        raise ValueError("CPU profile begin response must contain 4 bytes")
    run_id, = struct.unpack("<I", payload)
    if not run_id:
        raise ValueError("CPU profile run_id must be nonzero")
    return run_id


def decode_cpu_profile_page(payload, page):
    if type(page) is not int or not 0 <= page <= 4:
        raise ValueError("CPU profile page must be 0..4")
    size = 32 if page == 0 else 44
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != size:
        raise ValueError(f"CPU profile page {page} must contain {size} bytes")
    run_id = decode_cpu_profile_begin(payload[:4])
    if page == 0:
        result = dict(zip(CPU_PROFILE_FIELDS, struct.unpack("<8I", payload)))
        if not all(result[name] for name in ("elapsed_cycles", "cpu_hz", "target_cycles")):
            raise ValueError("CPU profile timing fields must be nonzero")
        if result["target_cycles"] * 4 != result["cpu_hz"] or result["elapsed_cycles"] < result["target_cycles"]:
            raise ValueError("CPU profile must contain a completed 250ms window")
        return result
    if payload[4] != 3:
        raise ValueError("CPU profile row page must contain exactly three rows")
    rows = []
    for index in range(3):
        stage, calls, total, maximum = struct.unpack_from("<B3I", payload, 5 + index * 13)
        if stage != (page - 1) * 3 + index:
            raise ValueError("CPU profile stage IDs differ from the requested page/order")
        if stage == 11 and (total or maximum):
            raise ValueError("CPU profile RT call counter must have zero timing fields")
        rows.append({"id": stage, "name": CPU_PROFILE_STAGES[stage], "calls": calls,
                     "total_cycles": total, "max_cycles": maximum})
    return {"run_id": run_id, "rows": rows}


def decode_adc_irq_profile_begin(payload):
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != 4:
        raise ValueError("ADC IRQ profile begin response must contain 4 bytes")
    run_id, = struct.unpack("<I", payload)
    if not run_id:
        raise ValueError("ADC IRQ profile run_id must be nonzero")
    return run_id


def decode_adc_irq_profile_page(payload, page):
    if type(page) is not int or not 0 <= page <= 2:
        raise ValueError("ADC IRQ profile page must be 0..2")
    size = 40 if page == 0 else 44
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != size:
        raise ValueError(f"ADC IRQ profile page {page} must contain {size} bytes")
    run_id = decode_adc_irq_profile_begin(payload[:4])
    if page == 0:
        result = dict(zip(ADC_IRQ_PROFILE_FIELDS, struct.unpack("<10I", payload)))
        if result["reserved"]:
            raise ValueError("ADC IRQ profile reserved field must be zero")
        if not all(result[name] for name in ("elapsed_cycles", "cpu_hz", "target_cycles")):
            raise ValueError("ADC IRQ profile timing fields must be nonzero")
        if result["target_cycles"] * 2 != result["cpu_hz"] or result["elapsed_cycles"] < result["target_cycles"]:
            raise ValueError("ADC IRQ profile must contain a completed 500ms window")
        return result
    if payload[4] != 3:
        raise ValueError("ADC IRQ profile row page must contain exactly three rows")
    rows = []
    for index in range(3):
        stage, calls, total, maximum = struct.unpack_from("<B3I", payload, 5 + index * 13)
        if stage != (page - 1) * 3 + index:
            raise ValueError("ADC IRQ profile stage IDs differ from the requested page/order")
        rows.append({"id": stage, "name": ADC_IRQ_PROFILE_STAGES[stage], "calls": calls,
                     "total_cycles": total, "max_cycles": maximum})
    return {"run_id": run_id, "rows": rows}


def measure_link(transport, seconds=5, *, delay=time.sleep, clock=time.monotonic):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not .2 <= seconds <= 60:
        raise ValueError("Measurement duration must be 0.2..60 seconds")
    client = KeyboardClient(transport, write_enabled=False)
    host_start = clock()
    first = decode_link_performance(client.request(LINK_PERFORMANCE))
    delay(seconds)
    last = decode_link_performance(client.request(LINK_PERFORMANCE))
    elapsed = delta32(last["micros"], first["micros"]) / 1_000_000
    host_elapsed = clock() - host_start
    if elapsed <= 0 or not 0.5 * host_elapsed <= elapsed <= 2 * host_elapsed:
        raise ValueError("Link counters span a possible reset/disconnection")
    deltas = {name: delta32(last[name], first[name]) for name in LINK_FIELDS[1:11]}
    return {"device_seconds": elapsed, "host_seconds": host_elapsed,
            "deltas": deltas, "poll_hz": deltas["polls"] / elapsed,
            "accepted_hz": deltas["accepted"] / elapsed,
            "lifetime_rtt_min_us": last["rtt_min_us"], "lifetime_rtt_max_us": last["rtt_max_us"],
            "first": first, "last": last}


def delta32(new, old):
    return (new - old) & 0xFFFFFFFF


def compare_samples(first, last, host_seconds):
    elapsed = delta32(last["micros"], first["micros"]) / 1_000_000
    if elapsed <= 0 or not 0.5 * host_seconds <= elapsed <= 2 * host_seconds:
        raise ValueError("Device time is inconsistent with host time; possible reset/disconnection. Counters are not comparable")
    if first["scan_target_hz"] != last["scan_target_hz"]:
        raise ValueError("Scan target changed during measurement")
    deltas = {name: delta32(last[name], first[name]) for name in FIELDS[1:9]}
    return {
        "device_elapsed_seconds": elapsed,
        "host_elapsed_seconds": host_seconds,
        "usb_completion_hz": deltas["usb_completed"] / elapsed,
        "adc_scan_hz": deltas["adc_sequence"] / elapsed,
        "scan_target_hz": last["scan_target_hz"],
        "counter_deltas": deltas,
        "adc_lifetime_min_period_us": last["adc_min_period_cycles"] / CPU_HZ * 1_000_000,
        "adc_lifetime_max_period_us": last["adc_max_period_cycles"] / CPU_HZ * 1_000_000,
        "adc_lifetime_max_irq_us": last["adc_irq_max_cycles"] / CPU_HZ * 1_000_000,
        "limits": "Device completion/ADC counters only; USB wire delivery, Windows receive rate, electrical accuracy and high-rate signal quality require separate measurements. Period extrema measure ADC frame publication intervals, or ADC1 half-ring IRQ intervals in independent DMA builds. Infer acquisition rate from sequence delta/time. Period/IRQ extrema are lifetime values, not interval-only values.",
    }


def measure(transport, seconds=5, *, delay=time.sleep, clock=time.monotonic):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not .2 <= seconds <= 60:
        raise ValueError("Measurement duration must be 0.2..60 seconds")
    client = KeyboardClient(transport, write_enabled=False)
    t0 = clock()
    first = decode_performance(client.request(PERFORMANCE))
    delay(seconds)
    last = decode_performance(client.request(PERFORMANCE))
    t1 = clock()
    return compare_samples(first, last, t1 - t0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--seconds", type=float, default=5)
    args = ap.parse_args(argv)
    if not math.isfinite(args.seconds) or not .2 <= args.seconds <= 60:
        ap.error("--seconds must be 0.2..60")
    print("Read-only measurement. Close Studio/MCP/other HID clients first. No settings, output or firmware changes.")
    transport = HidTransport(args.serial)
    try:
        result = measure(transport, args.seconds)
    finally:
        transport.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
