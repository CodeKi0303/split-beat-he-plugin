"""Deterministic device protocol simulator. NEVER imports hid or opens USB."""
from __future__ import annotations
import copy
import math
import struct
import time
import zlib
from .protocol import *
from .hall_curve import normalized_from_raw


def defaults():
    return validate_profile(dict(schema="c77-profile-v1", board="C77", keys=[dict(key=i, flags=3, released=0, pressed=0, actuation=200, rt_press=100, rt_release=100, top_deadzone=20, noise_deadband=5) for i in range(14)], bindings=[dict(input=i, kind=1 if i<20 else 2, code=4+i if i<20 else (1 if i%2 == 0 else -1), modifiers=0) for i in range(28)]))


class DemoTransport:
    demo = True
    serial = "DEMO-NO-USB"

    def __init__(self):
        self.profile = defaults()
        self.stored = None
        self.stored_output = False
        self.generation = 0
        self.enabled = False
        self.dirty = False
        self.queue = []
        self.cal = dict(key=255, state=0, rest=0, pressed=0, count=0)
        self.start = time.monotonic()
        self.ready_at = 0
        self.pose = None  # Explicit demo control only; None preserves animated traces.

    def close(self):
        self.enabled = False
        self.queue.clear()

    def reconnect(self, timeout=15):
        self.profile = copy.deepcopy(self.stored)
        self.dirty = False
        self.enabled = self.stored_output
        self.queue.clear()

    def _advance(self):
        if self.cal["state"] in (1, 3) and time.monotonic() >= self.ready_at:
            self.cal["state"] += 1
            self.cal["count"] = 256
            self.cal["rest" if self.cal["state"] == 2 else "pressed"] = 1000 if self.cal["state"] == 2 else 3000

    def write(self, data):
        op, seq, status, payload = unpack_packet(data)
        if status or op & 0x80:
            raise ProtocolError("invalid request")
        self._advance()
        status = 0
        try:
            reply = self._command(op, payload)
        except (ProtocolError, ValueError, IndexError, struct.error):
            status, reply = 4, b""
        except RuntimeError:
            status, reply = 7, b""
        self.queue.append(packet(op | 0x80, seq, reply, status))

    def _command(self, op, p):
        if op == INFO:
            if p: raise ProtocolError("length")
            crc = zlib.crc32(b"".join(encode_key(k) for k in self.profile["keys"])+b"".join(encode_binding(b) for b in self.profile["bindings"]))
            return struct.pack("<8B5IH4B", 0,3,22,0,14,6,4,28,64000,125,0x33f | CAP_KEY_CHORD | CAP_SAVE_OUTPUT, self.generation,crc,1024,86,1,2,bool(self.stored))
        if op == STATUS:
            if len(p) != 1 or p[0] > 1: raise ProtocolError("side")
            elapsed = time.monotonic()-self.start
            raw = [1000+int((math.sin(elapsed+i+p[0]*7)+1)*1000) for i in range(7)]
            if self.pose is not None:
                raw = [1000 + 2*v for v in self.pose[p[0]*7:p[0]*7+7]]
            travel = [(v-1000)//2 for v in raw]
            for offset, value in enumerate(raw):
                key = self.profile["keys"][p[0] * 7 + offset]
                if key["flags"] & 4:
                    travel[offset] = normalized_from_raw(value, key["released"], key["pressed"], key["curve_id"])
            flags = int(self.enabled)|2|(int(self.dirty)<<2)|(int(self.stored is not None)<<3)
            return struct.pack("<4I14H4BIH", int(elapsed*1000),int(elapsed*64000),0,100,*raw,*travel,0,flags,self.cal["key"],self.cal["state"],0,0)
        if op == GET_KEY:
            if len(p) != 1: raise ProtocolError("length")
            return encode_key(self.profile["keys"][p[0]])
        if op == GET_BINDING:
            if len(p) != 1: raise ProtocolError("length")
            return encode_binding(self.profile["bindings"][p[0]])
        if op == SET_KEY:
            d = decode_key(p)
            self.profile["keys"][d["key"]] = d
            self.enabled, self.dirty = False, True
            return encode_key(d)
        if op == SET_BINDING:
            d = decode_binding(p)
            self.profile["bindings"][d["input"]] = d
            self.enabled, self.dirty = False, True
            return encode_binding(d)
        if op == OUTPUT:
            if len(p) != 1 or p[0] > 1: raise ProtocolError("length")
            if p[0] and any(k["flags"]&1 and not k["flags"]&4 for k in self.profile["keys"]): raise RuntimeError("uncalibrated")
            self.enabled = bool(p[0])
            return bytes([self.enabled])
        if op == CALIBRATE:
            if len(p) != 2 or p[0] > 13 or p[1] > 3: raise ProtocolError("length")
            key, action = p
            self.enabled = False
            if action == 0:
                self.cal = dict(key=key, state=1, rest=0, pressed=0, count=0)
                self.ready_at = time.monotonic()+0.025
            elif action == 3:
                self.cal = dict(key=255, state=0, rest=0, pressed=0, count=0)
            elif key != self.cal["key"]:
                raise RuntimeError("wrong key")
            elif action == 1 and self.cal["state"] == 2:
                self.cal.update(state=3, count=0)
                self.ready_at = time.monotonic()+0.025
            elif action == 2 and self.cal["state"] == 4:
                self.profile["keys"][key].update(released=self.cal["rest"], pressed=self.cal["pressed"], flags=self.profile["keys"][key]["flags"]|4)
                self.cal["state"] = 0
                self.dirty = True
            else:
                raise RuntimeError("capture not ready")
            c = self.cal
            return struct.pack("<BB3H", c["key"],c["state"],c["rest"],c["pressed"],c["count"])
        if op == SAVE:
            if p: raise ProtocolError("length")
            self.stored_output = self.enabled
            self.enabled = False
            self.stored = copy.deepcopy(self.profile)
            self.generation = ((self.generation+1) & 0xFFFFFFFF) or 1
            return struct.pack("<I", self.generation)
        if op == DEFAULTS:
            if p: raise ProtocolError("length")
            self.profile = defaults()
            self.enabled, self.dirty = False, True
            return b""
        raise ProtocolError("unknown command")

    def read(self, timeout_ms):
        if self.queue:
            return self.queue.pop(0)
        time.sleep(min(timeout_ms/1000, 0.005))
        return b""
