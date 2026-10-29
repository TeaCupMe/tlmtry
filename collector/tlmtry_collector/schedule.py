"""Autonomous virtual readings: linear transitions between keyframes plus noise.

Between two timestamps the value moves in a straight line. Before the first
point and after the last one the value stays at that endpoint. Noise is a
uniform offset in ``[-noise, +noise]`` added when a sample is written.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .protocol import MetricType, SampleValue

_INT32_MIN = -2147483648
_INT32_MAX = 2147483647
_UINT16_MAX = 65535


@dataclass(frozen=True)
class Keyframe:
    ts: float
    value: float


@dataclass
class VirtualSchedule:
    address: int
    metric_id: int
    step_s: float
    noise: float
    enabled: bool
    last_emit_ts: float | None
    keyframes: list[Keyframe] = field(default_factory=list)


def interpolate(keyframes: list[Keyframe], ts: float) -> float:
    """Value of the smooth curve at ``ts``, without noise."""
    if not keyframes:
        raise ValueError("no keyframes")
    points = sorted(keyframes, key=lambda item: item.ts)
    if ts <= points[0].ts:
        return points[0].value
    if ts >= points[-1].ts:
        return points[-1].value
    for left, right in zip(points, points[1:]):
        if left.ts <= ts <= right.ts:
            span = right.ts - left.ts
            if span == 0:
                return right.value
            ratio = (ts - left.ts) / span
            return left.value + (right.value - left.value) * ratio
    return points[-1].value


def add_noise(value: float, amplitude: float, rng: random.Random) -> float:
    if amplitude <= 0:
        return value
    return value + rng.uniform(-amplitude, amplitude)


def quantize(kind: MetricType, value: float) -> int | float:
    if kind == MetricType.FLOAT:
        return float(value)
    if kind == MetricType.INT32:
        return max(_INT32_MIN, min(_INT32_MAX, int(round(value))))
    if kind == MetricType.UINT16:
        return max(0, min(_UINT16_MAX, int(round(value))))
    raise ValueError(f"schedule is numeric only, got {kind}")


def emit_due(storage, *, now: float, rng: random.Random | None = None, quality: int = 1) -> int:
    """Write one sample for every schedule whose step has elapsed.

    Returns how many samples were inserted. A missing metric is skipped.
    """
    generator = rng if rng is not None else random.Random()
    metrics = {(m.address, m.metric_id): m for m in storage.list_metrics()}
    written = 0
    for schedule in storage.list_schedules():
        if not schedule.enabled or not schedule.keyframes or schedule.step_s <= 0:
            continue
        metric = metrics.get((schedule.address, schedule.metric_id))
        if metric is None:
            continue
        if (
            schedule.last_emit_ts is not None
            and now < schedule.last_emit_ts + schedule.step_s
        ):
            continue
        try:
            kind = MetricType(metric.type)
        except ValueError:
            continue
        base = interpolate(schedule.keyframes, now)
        noisy = add_noise(base, schedule.noise, generator)
        try:
            value = quantize(kind, noisy)
        except ValueError:
            continue
        storage.insert_samples(
            schedule.address,
            [SampleValue(metric_id=schedule.metric_id, type=kind, value=value)],
            ts=now,
            quality=quality,
        )
        storage.mark_schedule_emitted(schedule.address, schedule.metric_id, now)
        written += 1
    return written
