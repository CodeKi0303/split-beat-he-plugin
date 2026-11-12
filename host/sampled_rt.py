"""Sampled RT state/event intent model; never emulates USB acknowledgements."""
from .protocol import key_record
from .hall_curve import normalized_from_raw


class SampledRT:
    def __init__(self, config):
        self.config = key_record(config)
        if self.config["flags"] & 5 != 5 or abs(config["pressed"] - config["released"]) < 128:
            raise ValueError("활성화된 Hall 키를 먼저 캘리브레이션하세요.")
        self.filtered = None
        self.pressed = self.rapid = False
        self.extreme = 0
        self.previous_travel = 0
        self.trough = 0
        self.trough_armed = self.retriggered = False

    def step(self, raw):
        c = self.config
        if self.filtered is None or abs(raw - self.filtered) >= c["noise_deadband"]:
            self.filtered = raw
        travel = normalized_from_raw(self.filtered, c["released"], c["pressed"], c.get("curve_id", 0))
        was_pressed = self.pressed
        self.retriggered = False
        if not c["flags"] & 1 or travel <= c["top_deadzone"]:
            self.pressed = self.rapid = False
            self.extreme = travel
        elif not c["flags"] & 2:
            self.pressed = travel >= c["actuation"]
            self.rapid = False
            self.extreme = travel
        elif c["bottom_deadzone"] and travel >= 1000 - c["bottom_deadzone"]:
            self.pressed = self.rapid = True
            self.extreme = 1000 - c["bottom_deadzone"]
        elif not self.rapid:
            if travel >= c["actuation"]:
                self.pressed = self.rapid = True
                self.extreme = travel
        elif self.pressed:
            self.extreme = max(self.extreme, travel)
            if self.extreme - travel >= c["rt_release"]:
                self.pressed = False
                self.extreme = travel
        else:
            self.extreme = min(self.extreme, travel)
            if travel - self.extreme >= c["rt_press"]:
                self.pressed = True
                self.extreme = travel
        # Event intent only: USB release/press completion is not sampled here.
        # Preserve the old held maximum, so ordinary release thresholds do not move.
        bottom_hold = c["bottom_deadzone"] and travel >= 1000 - c["bottom_deadzone"]
        if (was_pressed and self.pressed and c["flags"] & 10 == 10
                and travel > c["top_deadzone"] and not bottom_hold):
            if not self.trough_armed and travel < self.previous_travel:
                self.trough, self.trough_armed = travel, True
            if self.trough_armed:
                self.trough = min(self.trough, travel)
                if travel - self.trough >= c["rt_press"]:
                    self.retriggered, self.trough_armed = True, False
        else:
            self.trough_armed = False
        self.previous_travel = travel
        return self.pressed
