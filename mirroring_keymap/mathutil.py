from __future__ import annotations

import math
import random
from typing import Iterable

from .config import Point


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def add(a: Point, b: Point) -> Point:
    return (a[0] + b[0], a[1] + b[1])


def sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def length(v: Point) -> float:
    return math.hypot(v[0], v[1])


def scale(v: Point, s: float) -> Point:
    return (v[0] * s, v[1] * s)


def normalize(v: Point) -> Point:
    l = length(v)
    if l <= 1e-9:
        return (0.0, 0.0)
    return (v[0] / l, v[1] / l)


def random_point(center: Point, radius: float, *, rng: random.Random) -> Point:
    if radius <= 0:
        return center
    # 均匀分布在圆内（按面积）
    t = rng.random() * 2.0 * math.pi
    r = math.sqrt(rng.random()) * radius
    return (center[0] + math.cos(t) * r, center[1] + math.sin(t) * r)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_point(a: Point, b: Point, t: float) -> Point:
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))


def segment_points(start: Point, end: Point, *, max_step: float) -> Iterable[Point]:
    """生成从 start 到 end 的离散点，保证相邻点距离 <= max_step。"""
    if max_step <= 0:
        yield end
        return
    d = sub(end, start)
    dist = length(d)
    if dist <= max_step:
        yield end
        return
    steps = max(1, int(math.ceil(dist / max_step)))
    for i in range(1, steps + 1):
        yield lerp_point(start, end, i / steps)

