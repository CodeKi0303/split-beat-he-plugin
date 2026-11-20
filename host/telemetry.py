"""Input telemetry codecs (0x4A TELEMETRY_CONTROL, 0x4B TELEMETRY_READ). Stdlib only.

Telemetry is RAM-only on the C86 left master, off at boot, and never changes
input, output or settings. See ../PROTOCOL.md "C86 v0.3.44 input telemetry".
"""
from __future__ import annotations

import struct
import time

from .client import DeviceError
from .protocol import (CAP_TELEMETRY, STATUSES, ProtocolError, TELEMETRY_CONTROL, TELEMETRY_READ, TRAVEL_MAX, integer,
                       packet, unpack_packet)

# Measured on the C86 at rest (2026-11-19): some Hall keys wander up to ~45 ADC codes. A 12-code step
# (~0.1 mm with the current calibration span) keeps idle traffic to a few hundred records per second
# while a moving key still yields a sample every interval.
DEFAULT_THRESHOLD = 12
DEFAULT_INTERVAL_US = 1000

KIND_SAMPLE, KIND_DECISION, KIND_USB = 0, 1, 2
KIND_NAMES = {KIND_SAMPLE: "sample", KIND_DECISION: "decision", KIND_USB: "usb"}
HEADER = struct.Struct("<IIIBB")
RECORD = struct.Struct("<hBH")
CONTROL_REPLY = struct.Struct("<BBHIIII")
MAX_RECORDS = 8


def encode_control(enable, threshold=DEFAULT_THRESHOLD, interval_us=DEFAULT_INTERVAL_US):
    if enable is None:
        return b""
    if not isinstance(enable, bool):
        raise ProtocolError("enable: bool required")
    if enable:
        integer(threshold, 1, 255, "threshold")
        integer(interval_us, 0, 10000, "interval_us")
    return struct.pack("<BBH", 1 if enable else 0, threshold if enable else 0, interval_us if enable else 0)


def decode_control(payload):
    if len(payload) != CONTROL_REPLY.size:
        raise ProtocolError("Invalid telemetry control reply")
    armed, threshold, interval, capacity, now, dropped, pending = CONTROL_REPLY.unpack(payload)
    if armed > 1 or capacity == 0:
        raise ProtocolError("Invalid telemetry control reply")
    return {"armed": bool(armed), "threshold": threshold, "interval_us": interval, "capacity": capacity,
            "device_now_us": now, "dropped": dropped, "pending": pending}


def decode_read(payload):
    """Returns header fields and records with 32-bit device times (wrapping, not unwrapped)."""
    if len(payload) < HEADER.size:
        raise ProtocolError("Invalid telemetry read reply")
    now, base, dropped, count, flags = HEADER.unpack_from(payload)
    if count > MAX_RECORDS or len(payload) != HEADER.size + count * RECORD.size or flags & ~3:
        raise ProtocolError("Invalid telemetry read reply")
    records = []
    time = base
    for i in range(count):
        delta, kind_index, value = RECORD.unpack_from(payload, HEADER.size + i * RECORD.size)
        if i == 0 and delta:
            raise ProtocolError("First telemetry record must have zero delta")
        time = (time + delta) & 0xFFFFFFFF
        kind, index = kind_index >> 6, kind_index & 0x3F
        if kind not in KIND_NAMES:
            raise ProtocolError("Unknown telemetry record kind")
        record = {"device_us": time, "kind": KIND_NAMES[kind]}
        if kind == KIND_SAMPLE:
            if index >= 14 or value > 4095:
                raise ProtocolError("Invalid telemetry sample")
            record.update(key=index, raw=value)
        elif kind == KIND_DECISION:
            if index >= 14 or (value & 0x3FFF) > TRAVEL_MAX:
                raise ProtocolError("Invalid telemetry decision")
            record.update(key=index, travel=value & 0x3FFF, retriggered=bool(value & 0x4000), pressed=bool(value & 0x8000))
        else:
            if value & ~0x1FF:
                raise ProtocolError("Invalid telemetry USB change")
            record.update(usage=value & 0xFF, pressed=bool(value & 0x100))
        records.append(record)
    return {"device_now_us": now, "base_us": base, "dropped": dropped, "armed": bool(flags & 1),
            "more": bool(flags & 2), "records": records}


class DeviceClock:
    """Unwraps the 32-bit device microsecond counter (wraps every ~71.6 minutes)."""

    def __init__(self):
        self.last = None
        self.epoch = 0

    def unwrap_now(self, now):
        if self.last is not None and now < self.last and self.last - now > 0x80000000:
            self.epoch += 1 << 32
        self.last = now
        return self.epoch + now

    def unwrap_near(self, value):
        """A record time near the most recent unwrapped 'now'."""
        reference = self.epoch + (self.last or 0)
        candidate = self.epoch + value
        if candidate - reference > 0x80000000:
            candidate -= 1 << 32
        elif reference - candidate > 0x80000000:
            candidate += 1 << 32
        return candidate


def control(client, enable=None, threshold=DEFAULT_THRESHOLD, interval_us=DEFAULT_INTERVAL_US):
    return decode_control(client.request(TELEMETRY_CONTROL, encode_control(enable, threshold, interval_us)))


def read(client):
    return decode_read(client.request(TELEMETRY_READ))


class PipelinedReader:
    """Keeps `depth` read requests in flight. The master answers one vendor request per millisecond and
    queues four, so a strict request/response loop (~3 ms round trip) reads only a third of that.
    Read-only: it sends nothing but TELEMETRY_READ. Call client.info() first (capability check)."""

    def __init__(self, client, depth=3, timeout=1.0):
        if not client.identified or not client.device_info["capabilities"] & CAP_TELEMETRY:
            raise ProtocolError("Identify a telemetry-capable device before reading")
        self.transport, self.depth, self.timeout = client.transport, depth, timeout
        self.outstanding = {}
        self.sequence = 0

    def _send(self, clock_ns):
        self.sequence = self.sequence % 0x7FFF + 1
        seq = 0x8000 + self.sequence            # disjoint from KeyboardClient's low sequence numbers
        self.transport.write(packet(TELEMETRY_READ, seq))
        self.outstanding[seq] = clock_ns()

    def read(self, clock_ns=time.perf_counter_ns):
        """Returns (send_ns, receive_ns, decoded reply) for the oldest outstanding request."""
        while len(self.outstanding) < self.depth:
            self._send(clock_ns)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            data = self.transport.read(100)
            if not data:
                continue
            try:
                op, seq, status, payload = unpack_packet(data)
            except ProtocolError:
                continue
            if op != TELEMETRY_READ | 0x80 or seq not in self.outstanding:
                continue
            sent = self.outstanding.pop(seq)
            received = clock_ns()
            if status:
                raise DeviceError(STATUSES[status] if status < len(STATUSES) else f"device status {status}")
            return sent, received, decode_read(payload)
        raise TimeoutError("Telemetry read timed out")

    def drain(self, timeout=0.2):
        """Collects replies still in flight (e.g. before re-arming); their contents are discarded."""
        deadline = time.monotonic() + timeout
        while self.outstanding and time.monotonic() < deadline:
            data = self.transport.read(20)
            if data:
                try:
                    self.outstanding.pop(unpack_packet(data)[1], None)
                except ProtocolError:
                    pass
        self.outstanding.clear()
