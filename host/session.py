"""Lazy, serial-selected ownership for MCP. Construction never opens hardware."""
from __future__ import annotations

import threading

from .client import DeviceError, HidTransport, KeyboardClient
from .simulator import DemoTransport


class KeyboardSession:
    def __init__(self, client=None, write_enabled=False, firmware_write_enabled=False,
                 serial=None, demo=False):
        if serial is not None and (not isinstance(serial, str) or not serial.strip()):
            raise ValueError("serial must be a nonempty exact device serial")
        if demo and serial:
            raise ValueError("demo and serial are mutually exclusive")
        self.lock = threading.RLock()
        self.client = client
        self.write_enabled = bool(client.write_enabled) if client else bool(write_enabled)
        self.firmware_write_enabled = bool(firmware_write_enabled)
        self.preferred_serial = serial
        self.travel_mm = [4.0] * 14
        self.calibration = None
        self.interference = None
        self.auto_fit = None
        self._travel_by_identity = {}
        if demo:
            if client is not None:
                raise ValueError("client and demo are mutually exclusive")
            self.connect(demo=True)

    def require_client(self):
        if self.client is None:
            raise DeviceError("No keyboard connected. Use keyboard_list_devices then keyboard_connect with an exact serial, or connect demo=True.")
        return self.client

    def require_idle(self):
        if self.calibration is not None and self.calibration.active:
            raise DeviceError("Batch calibration is active. Apply or cancel it before changing settings or starting another measurement.")
        if self.auto_fit is not None and self.auto_fit.active:
            raise DeviceError("Automatic fitting is active. Finish or cancel it before changing settings or starting another measurement.")

    def identity(self):
        if self.client is None:
            return None
        transport = self.client.transport
        return "DEMO-NO-USB" if transport.demo else getattr(transport, "serial", None)

    def status(self):
        return {"connected": self.client is not None,
                "serial": self.identity(), "preferred_serial": self.preferred_serial,
                "demo": bool(self.client and self.client.transport.demo),
                "write_enabled": self.write_enabled,
                "firmware_write_enabled": self.firmware_write_enabled,
                "total_travel_mm": list(self.travel_mm),
                "batch_calibration_active": bool(self.calibration and self.calibration.active),
                "auto_fit_active": bool(self.auto_fit and self.auto_fit.active)}

    def connect(self, serial=None, demo=False):
        with self.lock:
            if self.client is not None:
                raise DeviceError("A keyboard is already connected. Disconnect it before choosing another device.")
            if type(demo) is not bool:
                raise ValueError("demo must be boolean")
            if demo and serial is not None:
                raise ValueError("demo and serial are mutually exclusive")
            chosen = serial if serial is not None else self.preferred_serial
            if not demo and (not isinstance(chosen, str) or not chosen.strip()):
                raise ValueError("Choose an exact serial from keyboard_list_devices; no automatic device selection is performed")
            transport = DemoTransport() if demo else HidTransport(chosen)
            candidate = KeyboardClient(transport, write_enabled=self.write_enabled)
            try:
                info = candidate.info()
            except Exception:
                transport.close()
                raise
            self.client = candidate
            if not demo:
                self.preferred_serial = chosen
            self.travel_mm = list(self._travel_by_identity.get(self.identity(), [4.0] * 14))
            self.calibration = None
            self.interference = None
            self.auto_fit = None
            return {**self.status(), "info": info, "changed_device_settings": False}

    def disconnect(self):
        with self.lock:
            if self.calibration is not None:
                self.calibration.close()
            self.calibration = None
            self.interference = None
            self.auto_fit = None
            client, identity = self.client, self.identity()
            if identity is not None:
                self._travel_by_identity[identity] = list(self.travel_mm)
            self.client = None
            if client is not None:
                # Releasing this application handle must not silently send
                # OUTPUT/SAVE. Explicit keyboard_output owns that choice.
                client.transport.close()
            return {**self.status(), "changed_device_settings": False}
