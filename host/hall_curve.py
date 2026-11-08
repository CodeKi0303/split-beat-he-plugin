"""Approximate monotone Hall travel mapping, shared numerically with firmware.

The OW Ti reference is public NeonHE Tab60 profile data, not a measured C86
distance calibration. Its original 3.3 mm axis is normalized here; choosing
3.4 mm (user-provided Owlab Ti HE stroke) or another stroke scales distance,
not the curve shape. Prior caliper positions are not treated as physical mm.
"""
from __future__ import annotations

import math

CURVE_LINEAR = 0
CURVE_OWLAB_TI_REFERENCE = 1
OW_TI_SIGNAL = (0, 467, 1211, 2244, 3667, 5644, 8500, 10000)
OW_TI_REFERENCE_DISTANCE = (0, 5000, 10000, 15000, 20000, 25000, 30000, 33000)
SOURCE_URL = "https://neonhe.com/assets/index-C2gKFU8p.js"


def _integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name}: integer {minimum}..{maximum} required")
    return value


def normalized_from_raw(raw, released, pressed, curve_id=0):
    """Firmware-identical integer mapping, including polarity and rounding."""
    for name, value in (("raw", raw), ("released", released), ("pressed", pressed)):
        _integer(value, 0, 65535, name)
    _integer(curve_id, 0, 1, "curve_id")
    span, distance = pressed - released, raw - released
    if span < 0:
        span, distance = -span, -distance
    if span < 128:
        raise ValueError("Calibrated ADC span must be at least 128")
    if distance <= 0:
        return 0
    if distance >= span:
        return 1000
    if curve_id == CURVE_LINEAR:
        return (distance * 1000 + span // 2) // span
    signal = (distance * 10000 + span // 2) // span
    for index in range(1, len(OW_TI_SIGNAL)):
        if signal <= OW_TI_SIGNAL[index]:
            x0, x1 = OW_TI_SIGNAL[index - 1:index + 1]
            y0, y1 = OW_TI_REFERENCE_DISTANCE[index - 1:index + 1]
            y = y0 + ((signal - x0) * (y1 - y0) + (x1 - x0) // 2) // (x1 - x0)
            return (y * 1000 + 16500) // 33000
    return 1000


def normalized_to_mm(travel, total_travel_mm):
    """Device STATUS travel is already curve-corrected: scale once, never LUT twice."""
    _integer(travel, 0, 1000, "travel")
    if type(total_travel_mm) not in (int, float) or not math.isfinite(total_travel_mm) or not .1 <= total_travel_mm <= 10:
        raise ValueError("total_travel_mm must be finite and 0.10..10.00")
    return travel * total_travel_mm / 1000


def raw_to_estimated_mm(raw, key_config, total_travel_mm):
    """Estimated distance for raw snapshots; not proof of physical accuracy."""
    if not key_config.get("flags", 0) & 4:
        return None
    return normalized_to_mm(normalized_from_raw(raw, key_config["released"],
                            key_config["pressed"], key_config.get("curve_id", 0)), total_travel_mm)


def curve_catalog():
    return {
        "curves": [
            {"curve_id": 0, "name": "Linear endpoint estimate", "requires_capability": 0},
            {"curve_id": 1, "name": "Owlab Ti HE reference (approximate)",
             "recommended_total_travel_mm": 3.4, "reference_total_travel_mm": 3.3,
             "requires_capability": 0x200, "minimum_firmware": "0.3.5",
             "source_url": SOURCE_URL, "source_board": "NeonHE Tab60 HE / OW Ti",
             "signal_nodes": list(OW_TI_SIGNAL),
             "reference_distance_nodes": list(OW_TI_REFERENCE_DISTANCE)},
        ],
        "scaling": "Estimated mm = corrected normalized travel * per-key total_travel_mm / 1000. Other strokes keep the same approximate curve shape.",
        "limits": "Reference curve is not a measured C86 transfer function. Different switch magnets and board geometry can change the curve; proportional scaling does not establish physical mm accuracy. Preserve per-key measured ADC endpoints.",
        "selection": "Use keyboard_preview_keys / keyboard_set_keys patch {curve_id: 1}; set total stroke separately with keyboard_set_travel_mm. No automatic profile selection or firmware update.",
    }
