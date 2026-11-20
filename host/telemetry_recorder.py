"""Record C86 input telemetry for a play session. Read-only for input/output/settings.

python -m host.telemetry_recorder --serial SERIAL --out DIR [--threshold 4] [--interval-us 1000] [--seconds N]

Close Studio/MCP first (one HID owner). Stop with Ctrl+C; telemetry is disarmed on exit.
Files in DIR/<UTC stamp>/:
  session.json     device INFO, all 14 key settings, 28 bindings, arm parameters, host clock notes
  records.csv.gz   device_us (unwrapped), kind, key, usage, raw, travel, pressed, retriggered
  sync.csv.gz      host_send_ns, host_recv_ns, device_now_us (unwrapped), count, dropped
Host times are time.perf_counter_ns(), i.e. QueryPerformanceCounter on Windows: the same
counter Java's System.nanoTime() reads, so they share a timebase with the beatoraja export.
"""
from __future__ import annotations

import argparse
import gzip
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import DeviceError, HidTransport, KeyboardClient
from .protocol import CAP_TELEMETRY, STATUSES
from . import telemetry


def record_row(clock, record):
    return ",".join(str(x) for x in (
        clock.unwrap_near(record["device_us"]), record["kind"], record.get("key", ""), record.get("usage", ""),
        record.get("raw", ""), record.get("travel", ""),
        "" if "pressed" not in record else int(record["pressed"]),
        "" if "retriggered" not in record else int(record["retriggered"])))


def run(client, out_dir, threshold, interval_us, seconds=None, stop=lambda: False, clock_ns=time.perf_counter_ns,
        reader_factory=telemetry.PipelinedReader):
    info = client.info()
    if not info["capabilities"] & CAP_TELEMETRY:
        raise DeviceError("Firmware has no input telemetry; install C86 v0.3.44+ on both halves")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = Path(out_dir) / stamp
    folder.mkdir(parents=True, exist_ok=False)
    session = {"schema": "c86-input-telemetry-session-v1", "started_utc": stamp,
               "host_wall_ns": time.time_ns(), "host_perf_ns": clock_ns(),
               "host_clock": "time.perf_counter_ns (QueryPerformanceCounter on Windows)",
               "device": {k: v for k, v in info.items() if k not in ("demo",)},
               "config": client.get_config(), "threshold": threshold, "interval_us": interval_us}
    clock = telemetry.DeviceClock()
    totals = {"records": 0, "reads": 0, "dropped": 0, "rearmed": 0}
    with gzip.open(folder / "records.csv.gz", "wt", encoding="utf-8", newline="") as rec, \
            gzip.open(folder / "sync.csv.gz", "wt", encoding="utf-8", newline="") as sync:
        rec.write("device_us,kind,key,usage,raw,travel,pressed,retriggered\n")
        sync.write("host_send_ns,host_recv_ns,device_now_us,count,dropped\n")
        armed = telemetry.control(client, True, threshold, interval_us)
        session["armed"] = armed
        (folder / "session.json").write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
        deadline = None if seconds is None else time.monotonic() + seconds
        reader = reader_factory(client)
        try:
            while not stop() and (deadline is None or time.monotonic() < deadline):
                try:
                    sent, received, reply = reader.read(clock_ns)
                except DeviceError as exc:
                    if str(exc) != STATUSES[7]:
                        raise
                    reader.drain()
                    telemetry.control(client, True, threshold, interval_us)   # auto-disarmed after a stall
                    totals["rearmed"] += 1
                    continue
                now = clock.unwrap_now(reply["device_now_us"])
                sync.write(f"{sent},{received},{now},{len(reply['records'])},{reply['dropped']}\n")
                for record in reply["records"]:
                    rec.write(record_row(clock, record) + "\n")
                totals["records"] += len(reply["records"])
                totals["reads"] += 1
                totals["dropped"] = reply["dropped"]
        finally:
            try:
                reader.drain()
            except Exception:
                pass
            try:
                final = telemetry.control(client, False)
                session["disarmed"] = final
            except Exception as exc:  # keep the data even if the device went away
                session["disarm_error"] = repr(exc)
            session["totals"] = totals
            session["ended_host_perf_ns"] = clock_ns()
            (folder / "session.json").write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
    return folder, totals


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", required=True, help="left board USB serial (Studio shows it; e.g. 24 hex digits)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--threshold", type=int, default=telemetry.DEFAULT_THRESHOLD,
                    help="ADC codes a key must move before a new sample (1..255)")
    ap.add_argument("--interval-us", type=int, default=telemetry.DEFAULT_INTERVAL_US,
                    help="minimum time between samples of one key (0..10000)")
    ap.add_argument("--seconds", type=float)
    a = ap.parse_args(argv)
    stopping = []
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    print("Recording input telemetry. Input, output and settings are not changed. Ctrl+C to stop.", flush=True)
    transport = HidTransport(a.serial)
    try:
        folder, totals = run(KeyboardClient(transport), a.out, a.threshold, a.interval_us, a.seconds, lambda: bool(stopping))
    finally:
        transport.close()
    print(json.dumps({"folder": str(folder), **totals}, ensure_ascii=False))
    if totals["dropped"]:
        print("WARNING: the device ring overflowed; raise --interval-us or --threshold", file=sys.stderr)


if __name__ == "__main__":
    main()
