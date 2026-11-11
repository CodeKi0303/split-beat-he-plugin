"""Bounded RT recommendations from host snapshots, shared with Studio auto-fit.mjs."""
from __future__ import annotations

from copy import deepcopy
import math

from .hall_curve import normalized_from_raw
from .protocol import key_record

PHASES = ("released", "resting", "tapping", "holding")
PHASE_NAMES = dict(zip(PHASES, ("손 떼기", "손 얹기", "반복 입력", "길게 누르기")))
ERROR_FIELDS = ("restActivations", "missedTaps", "extraTaps", "missedReleases", "holdDrops")
LIMITS = "관찰한 센서 샘플로 비교한 추천입니다. 짧은 변화가 샘플 사이에서 누락될 수 있습니다. 추천값 적용 후 실제 눌림·해제 판정을 다시 확인하세요."
DISTANCES = ("actuation", "rt_press", "rt_release", "top_deadzone", "bottom_deadzone")


def js_round(value):
    return math.floor(value + .5)


def rounded(value):
    return js_round(value * 1000000) / 1000000


def clamp(value, low, high):
    return max(low, min(high, value))


def unique(values):
    return list(dict.fromkeys(js_round(value) for value in values))


def quantile(values, fraction):
    return sorted(values)[math.floor((len(values) - 1) * fraction)]


def depth(raw, config):
    return normalized_from_raw(raw, config["released"], config["pressed"], config.get("curve_id", 0))


class SampledRT:
    def __init__(self, config):
        self.config = key_record(config)
        if self.config["flags"] & 5 != 5 or abs(config["pressed"] - config["released"]) < 128:
            raise ValueError("활성화된 Hall 키를 먼저 캘리브레이션하세요.")
        self.filtered = None
        self.pressed = self.rapid = False
        self.extreme = 0

    def step(self, raw):
        c = self.config
        if self.filtered is None or abs(raw - self.filtered) >= c["noise_deadband"]:
            self.filtered = raw
        travel = depth(self.filtered, c)
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
        return self.pressed


def find_cycles(rows, low, high):
    cycles = []
    start = peak = None
    active = False
    for index, row in enumerate(rows):
        value = row["depth"]
        if not active:
            if value <= low and (start is None or value <= rows[start]["depth"]):
                start = index
            elif start is not None and value >= high:
                active, peak = True, index
        else:
            if value > rows[peak]["depth"]:
                peak = index
            if value <= low:
                if row["timeMs"] - rows[start]["timeMs"] >= 40:
                    cycles.append(dict(start=start, peak=peak, end=index))
                active, start, peak = False, index, None
    for index, cycle in enumerate(cycles):
        cycle["until"] = cycles[index + 1]["start"] if index + 1 < len(cycles) else max(cycle["end"], start or 0)
    return cycles


def prepare(value):
    if not isinstance(value, dict):
        raise ValueError("측정 기록이 필요합니다.")
    config = key_record(value["keyConfig"])
    if "key" in value and (type(value["key"]) is not int or value["key"] != config["key"]):
        raise ValueError("측정한 키와 설정의 키가 다릅니다.")
    SampledRT(config)
    preference = value.get("preference", "balanced")
    if preference not in ("balanced", "repeat", "hold"):
        raise ValueError("사용 목적을 다시 선택하세요.")
    total = value["totalTravelMm"]
    if type(total) not in (int, float) or not math.isfinite(total) or not .1 <= total <= 10:
        raise ValueError("스위치 총 이동거리를 확인하세요.")
    records, max_gap = {}, 0
    phases = value.get("phases", {})
    if not isinstance(phases, dict):
        raise ValueError("측정 단계가 필요합니다.")
    for phase in PHASES:
        rows = phases.get(phase)
        name = PHASE_NAMES[phase]
        if not isinstance(rows, list) or not 40 <= len(rows) <= 4000:
            raise ValueError(f"{name} 측정을 완료하세요. 최소 40개 샘플이 필요합니다.")
        records[phase] = []
        for index, row in enumerate(rows):
            if (not isinstance(row, dict) or type(row.get("timeMs")) not in (int, float)
                    or not math.isfinite(row["timeMs"]) or type(row.get("raw")) is not int
                    or not 0 <= row["raw"] <= 4095 or type(row.get("sequence")) is not int
                    or not 0 <= row["sequence"] <= 0xffffffff or row.get("fresh") is False
                    or row.get("calibrationState") or ("pressed" in row and type(row["pressed"]) is not bool)):
                raise ValueError(f"{name} 기록에 유효하지 않은 센서 값이 있습니다. 다시 측정하세요.")
            if index:
                gap = row["timeMs"] - rows[index - 1]["timeMs"]
                advance = (row["sequence"] - rows[index - 1]["sequence"]) & 0xffffffff
                if gap <= 0 or gap > 100 or not advance or advance >= 0x80000000:
                    raise ValueError(f"{name} 측정 중 통신이 끊기거나 중복된 샘플이 있습니다. 다시 측정하세요.")
                max_gap = max(max_gap, gap)
            records[phase].append({**row, "depth": depth(row["raw"], config)})
        if rows[-1]["timeMs"] - rows[0]["timeMs"] < 2000:
            raise ValueError(f"{name}를 2초 이상 측정하세요.")
    released = [row["depth"] for row in records["released"]]
    if quantile(released, .95) > 80:
        raise ValueError("손 떼기 기록이 보정된 시작 위치에서 멉니다. 손을 완전히 떼고 다시 측정하세요.")
    released_max = max(released)
    rest_max = max(released_max, max(row["depth"] for row in records["resting"]))
    margin = max(6, math.ceil((released_max - min(released)) * 1.5))
    taps = [row["depth"] for row in records["tapping"]]
    tap_low, tap_high = quantile(taps, .1), quantile(taps, .9)
    amplitude = tap_high - tap_low
    if amplitude < max(100, margin * 4):
        raise ValueError("반복 입력의 움직임을 구분하기 어렵습니다. 키를 충분히 눌렀다 놓으며 다시 측정하세요.")
    cycles = find_cycles(records["tapping"], tap_low + amplitude * .25, tap_low + amplitude * .65)
    if len(cycles) < 6:
        raise ValueError("완료된 반복 입력이 6회 미만입니다. 편한 속도로 6회 이상 눌렀다 놓아 주세요.")
    minimum_peak = min(records["tapping"][cycle["peak"]]["depth"] for cycle in cycles)
    if rest_max + margin >= minimum_peak - margin:
        raise ValueError("손을 얹은 깊이와 누른 깊이가 너무 가깝습니다. 두 동작을 구분해서 다시 측정하세요.")
    holds = records["holding"]
    interior = [row for row in holds if holds[0]["timeMs"] + 250 <= row["timeMs"] <= holds[-1]["timeMs"] - 250]
    hold_depths = [row["depth"] for row in interior]
    if quantile(hold_depths, .1) < rest_max + max(80, margin * 3):
        raise ValueError("길게 누르기 기록에서 키가 충분히 눌리지 않았습니다. 누른 상태를 유지하며 다시 측정하세요.")
    peak, retreat = hold_depths[0], 0
    for current in hold_depths:
        peak = max(peak, current)
        retreat = max(retreat, peak - current)
    evidence = dict(tapCycles=len(cycles), releasedMax=released_max, restMax=rest_max, noiseMargin=margin,
                    minimumTapPeak=minimum_peak, tapAmplitude=amplitude, holdMinimum=min(hold_depths),
                    holdRetreat=retreat, maxGapMs=rounded(max_gap))
    return dict(config=config, preference=preference, totalTravelMm=total, records=records,
                cycles=cycles, evidence=evidence)


def rising(states, start=0, end=None):
    if end is None:
        end = len(states) - 1
    return [index for index in range(start, end + 1) if states[index] and (index == 0 or not states[index - 1])]


def metrics(config, data, observed=False):
    states = {}
    for phase in PHASES:
        model = SampledRT(config)
        states[phase] = [row["pressed"] if observed else model.step(row["raw"]) for row in data["records"][phase]]
    taps, pressed = data["records"]["tapping"], states["tapping"]
    missed, extra, missed_release, retreats = 0, 0, 0, []
    for cycle in data["cycles"]:
        presses = rising(pressed, cycle["start"], cycle["until"] - 1)
        if not presses or not pressed[cycle["peak"]]:
            missed += 1
        extra += max(0, len(presses) - 1)
        release = next((index for index in range(cycle["peak"] + 1, cycle["until"] + 1)
                        if not pressed[index] and pressed[index - 1]), None)
        if release is None:
            missed_release += 1
        else:
            retreats.append(max(0, taps[cycle["peak"]]["depth"] - taps[release]["depth"]))
    holds = data["records"]["holding"]
    interior = [states["holding"][index] for index, row in enumerate(holds)
                if holds[0]["timeMs"] + 250 <= row["timeMs"] <= holds[-1]["timeMs"] - 250]
    drops = sum(not state and (index == 0 or interior[index - 1]) for index, state in enumerate(interior))
    return dict(restActivations=len(rising(states["released"])) + len(rising(states["resting"])),
                missedTaps=missed, extraTaps=extra, missedReleases=missed_release, holdDrops=drops,
                holdUnpressedFraction=rounded(sum(not state for state in interior) / len(interior)),
                releaseRetreatNormalized=rounded(sum(retreats) / len(retreats)) if retreats else None,
                tapCycles=len(data["cycles"]))


def has_observed(data):
    return all(type(row.get("pressed")) is bool for phase in PHASES for row in data["records"][phase])


def describe(config, data):
    return dict(config=config, label="래피드 트리거 사용" if config["flags"] & 2 else "고정 작동점 사용",
                metrics=metrics(config, data),
                mm={field: rounded(config[field] * data["totalTravelMm"] / 1000) for field in DISTANCES})


def evaluate_auto_fit(config, value):
    try:
        data = prepare(value)
        model = SampledRT(config)
        return dict(ready=True, reasons=[], metrics=metrics(model.config, data),
                    observedMetrics=metrics(model.config, data, True) if has_observed(data) else None,
                    evidence=data["evidence"], limits=LIMITS)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        return dict(ready=False, reasons=[str(error)], limits=LIMITS)


def analyze_auto_fit(value):
    try:
        data = prepare(value)
        config, preference, e = data["config"], data["preference"], data["evidence"]
        baseline = describe(config, data)
        top = clamp(e["releasedMax"] + e["noiseMargin"], 0, 998)
        low = max(top + 1, e["restMax"] + e["noiseMargin"])
        span = e["minimumTapPeak"] - low
        target_actuation = low + span * dict(balanced=.35, repeat=.2, hold=.4)[preference]
        target_press = max(e["noiseMargin"], e["tapAmplitude"] * dict(balanced=.08, repeat=.05, hold=.12)[preference])
        target_release = max(e["noiseMargin"], e["holdRetreat"] + e["noiseMargin"] + e["tapAmplitude"] * dict(balanced=.08, repeat=.03, hold=.15)[preference])
        actuations = [v for v in unique([low, low + span * .2, low + span * .35, low + span * .5, target_actuation, config["actuation"]])
                      if top < v < e["minimumTapPeak"]]
        presses = [clamp(v, 1, 1000) for v in unique([e["noiseMargin"], target_press, e["tapAmplitude"] * .15, config["rt_press"]])]
        releases = [clamp(v, 1, 1000) for v in unique([e["noiseMargin"], target_release, e["holdRetreat"] + e["noiseMargin"], e["tapAmplitude"] * .25, config["rt_release"]])]
        bottoms = unique([0, config["bottom_deadzone"], min(200, 1000 - e["holdMinimum"] + e["noiseMargin"]) if e["holdMinimum"] >= 800 else 0])
        candidates = [baseline]
        for actuation in actuations:
            candidates.append(describe({**config, "flags": config["flags"] & ~2, "actuation": actuation,
                                        "top_deadzone": top, "bottom_deadzone": 0}, data))
            for press in presses:
                for release in releases:
                    for bottom in bottoms:
                        if bottom <= 1000 - actuation:
                            candidates.append(describe({**config, "flags": config["flags"] | 2, "actuation": actuation,
                                                        "rt_press": press, "rt_release": release,
                                                        "top_deadzone": top, "bottom_deadzone": bottom}, data))
        safe = [candidate for candidate in candidates
                if all(candidate["metrics"][field] <= baseline["metrics"][field] for field in ERROR_FIELDS)
                and candidate["metrics"]["holdUnpressedFraction"] <= baseline["metrics"]["holdUnpressedFraction"]]

        def score(candidate):
            c, m = candidate["config"], candidate["metrics"]
            errors = m["restActivations"] * 8 + m["holdDrops"] * 8 + m["holdUnpressedFraction"] * 8 + m["missedTaps"] * 4 + m["missedReleases"] * 4 + m["extraTaps"] * 6
            fit = abs(c["actuation"] - target_actuation) / 1000
            fit += (abs(c["rt_press"] - target_press) + abs(c["rt_release"] - target_release)) / 3000 if c["flags"] & 2 else dict(balanced=.08, repeat=.15, hold=0)[preference]
            return errors * 1000 + fit + c["bottom_deadzone"] / 100000

        safe.sort(key=score)
        best = safe[0]
        if any(best["metrics"][field] > 0 for field in ERROR_FIELDS) or best["metrics"]["holdUnpressedFraction"] > 0:
            return dict(ready=False, reasons=["측정한 연타와 유지 동작을 모두 만족하는 후보를 찾지 못했습니다. 힘을 편하게 두고 다시 측정하세요."],
                        baseline=baseline, candidates=safe[:3], evidence=e, limits=LIMITS)
        changed = any(best["config"][field] != config[field] for field in ("flags", *DISTANCES))
        return dict(ready=True, reasons=[], baseline=baseline,
                    recommendation={**best, "changed": changed, "requiresRetest": True},
                    candidates=safe[:3], evidence=e,
                    observedMetrics=metrics(config, data, True) if has_observed(data) else None, limits=LIMITS)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        return dict(ready=False, reasons=[str(error)], limits=LIMITS)
