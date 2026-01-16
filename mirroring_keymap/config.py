from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

Point = tuple[float, float]


def _as_point(v: Any, *, field: str) -> Point:
    if (
        not isinstance(v, (list, tuple))
        or len(v) != 2
        or not isinstance(v[0], (int, float))
        or not isinstance(v[1], (int, float))
    ):
        raise ValueError(f"{field} 必须是 [x, y] 数组")
    return (float(v[0]), float(v[1]))


@dataclass(frozen=True)
class TargetWindowConfig:
    titleHint: str
    pid: Optional[int] = None
    windowId: Optional[int] = None


@dataclass(frozen=True)
class GlobalConfig:
    enableHotkey: str = "F8"
    panicHotkey: str = "F12"
    cameraLockKey: str = "CapsLock"
    backpackKey: str = "Tab"
    rrandDefaultPx: float = 0.0


@dataclass(frozen=True)
class JoystickConfig:
    radiusPx: float = 120.0
    tauMs: float = 60.0
    rrandPx: Optional[float] = None


@dataclass(frozen=True)
class CameraConfig:
    tcamPx: float = 3.0
    radiusPx: float = 80.0
    invertY: bool = False
    sensitivity: float = 1.0
    rrandPx: Optional[float] = None


@dataclass(frozen=True)
class ActionConfig:
    mode: Literal["tap"] = "tap"
    tapHoldMs: int = 30
    rrandPx: Optional[float] = None


@dataclass(frozen=True)
class WheelConfig:
    enabled: bool = True
    dPx: float = 8.0
    stopMs: int = 120
    invert: bool = False
    rrandPx: Optional[float] = None


@dataclass(frozen=True)
class SchedulerConfig:
    tickHz: int = 120
    cameraMinHz: int = 50
    joystickMinHz: int = 20
    cameraBudgetMs: int = 6
    maxStepPx: float = 6.0


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    points: dict[str, Point]
    joystick: JoystickConfig
    camera: CameraConfig
    fire: ActionConfig
    scope: ActionConfig
    wheel: WheelConfig
    scheduler: SchedulerConfig


CustomType = Literal["tap"]


@dataclass(frozen=True)
class CustomMapping:
    name: str
    key: str
    type: CustomType
    point: Point
    tapHoldMs: int = 30
    rrandPx: Optional[float] = None


@dataclass(frozen=True)
class AppConfig:
    version: int
    targetWindow: TargetWindowConfig
    global_: GlobalConfig
    profiles: list[ProfileConfig]
    customMappings: list[CustomMapping]


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是 JSON Object")

    version = int(data.get("version", 0))
    if version != 1:
        raise ValueError(f"暂不支持的配置版本: {version}（仅支持 1）")

    tw = data.get("targetWindow") or {}
    if not isinstance(tw, dict):
        raise ValueError("targetWindow 必须是对象")
    title_hint = str(tw.get("titleHint") or "iPhone Mirroring")
    pid = tw.get("pid", None)
    pid_i = int(pid) if isinstance(pid, int) else None
    wid = tw.get("windowId", None)
    wid_i = int(wid) if isinstance(wid, int) else None
    target = TargetWindowConfig(titleHint=title_hint, pid=pid_i, windowId=wid_i)

    g = data.get("global") or {}
    if not isinstance(g, dict):
        raise ValueError("global 必须是对象")
    global_cfg = GlobalConfig(
        enableHotkey=str(g.get("enableHotkey") or "F8"),
        panicHotkey=str(g.get("panicHotkey") or "F12"),
        cameraLockKey=str(g.get("cameraLockKey") or "CapsLock"),
        backpackKey=str(g.get("backpackKey") or "Tab"),
        rrandDefaultPx=float(g.get("rrandDefaultPx") or 0.0),
    )

    profiles_raw = data.get("profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise ValueError("profiles 必须是非空数组")

    profiles: list[ProfileConfig] = []
    for i, pr in enumerate(profiles_raw):
        if not isinstance(pr, dict):
            raise ValueError(f"profiles[{i}] 必须是对象")
        name = str(pr.get("name") or f"Profile {i+1}")

        points_raw = pr.get("points") or {}
        if not isinstance(points_raw, dict):
            raise ValueError(f"profiles[{i}].points 必须是对象")
        points: dict[str, Point] = {}
        for k in ("C", "A", "F", "S", "I"):
            if k in points_raw:
                points[k] = _as_point(points_raw[k], field=f"profiles[{i}].points.{k}")
        missing = [k for k in ("C", "A", "F", "S", "I") if k not in points]
        if missing:
            raise ValueError(f"profiles[{i}].points 缺少点位: {', '.join(missing)}")

        joystick_raw = pr.get("joystick") or {}
        camera_raw = pr.get("camera") or {}
        fire_raw = pr.get("fire") or {}
        scope_raw = pr.get("scope") or {}
        wheel_raw = pr.get("wheel") or {}
        sched_raw = pr.get("scheduler") or {}

        joystick = JoystickConfig(
            radiusPx=float(joystick_raw.get("radiusPx") or 120.0),
            tauMs=float(joystick_raw.get("tauMs") or 60.0),
            rrandPx=(float(joystick_raw["rrandPx"]) if joystick_raw.get("rrandPx") is not None else None),
        )
        camera = CameraConfig(
            tcamPx=float(camera_raw.get("tcamPx") or 3.0),
            radiusPx=float(camera_raw.get("radiusPx") or 80.0),
            invertY=bool(camera_raw.get("invertY") or False),
            sensitivity=float(camera_raw.get("sensitivity") or 1.0),
            rrandPx=(float(camera_raw["rrandPx"]) if camera_raw.get("rrandPx") is not None else None),
        )
        fire = ActionConfig(
            mode="tap",
            tapHoldMs=int(fire_raw.get("tapHoldMs") or 30),
            rrandPx=(float(fire_raw["rrandPx"]) if fire_raw.get("rrandPx") is not None else None),
        )
        scope = ActionConfig(
            mode="tap",
            tapHoldMs=int(scope_raw.get("tapHoldMs") or 30),
            rrandPx=(float(scope_raw["rrandPx"]) if scope_raw.get("rrandPx") is not None else None),
        )
        wheel = WheelConfig(
            enabled=bool(wheel_raw.get("enabled") if wheel_raw.get("enabled") is not None else True),
            dPx=float(wheel_raw.get("dPx") or 8.0),
            stopMs=int(wheel_raw.get("stopMs") or 120),
            invert=bool(wheel_raw.get("invert") or False),
            rrandPx=(float(wheel_raw["rrandPx"]) if wheel_raw.get("rrandPx") is not None else None),
        )
        scheduler = SchedulerConfig(
            tickHz=int(sched_raw.get("tickHz") or 120),
            cameraMinHz=int(sched_raw.get("cameraMinHz") or 50),
            joystickMinHz=int(sched_raw.get("joystickMinHz") or 20),
            cameraBudgetMs=int(sched_raw.get("cameraBudgetMs") or 6),
            maxStepPx=float(sched_raw.get("maxStepPx") or 6.0),
        )

        profiles.append(
            ProfileConfig(
                name=name,
                points=points,
                joystick=joystick,
                camera=camera,
                fire=fire,
                scope=scope,
                wheel=wheel,
                scheduler=scheduler,
            )
        )

    custom_raw = data.get("customMappings") or []
    if not isinstance(custom_raw, list):
        raise ValueError("customMappings 必须是数组")
    custom: list[CustomMapping] = []
    for i, cm in enumerate(custom_raw):
        if not isinstance(cm, dict):
            raise ValueError(f"customMappings[{i}] 必须是对象")
        typ = str(cm.get("type") or "tap")
        if typ != "tap":
            raise ValueError(f"customMappings[{i}].type 仅支持 tap（当前: {typ}）")
        custom.append(
            CustomMapping(
                name=str(cm.get("name") or f"Custom {i+1}"),
                key=str(cm.get("key") or ""),
                type="tap",
                point=_as_point(cm.get("point"), field=f"customMappings[{i}].point"),
                tapHoldMs=int(cm.get("tapHoldMs") or 30),
                rrandPx=(float(cm["rrandPx"]) if cm.get("rrandPx") is not None else None),
            )
        )

    return AppConfig(
        version=version,
        targetWindow=target,
        global_=global_cfg,
        profiles=profiles,
        customMappings=custom,
    )


def select_profile(cfg: AppConfig, name: Optional[str]) -> ProfileConfig:
    if not name:
        return cfg.profiles[0]
    for p in cfg.profiles:
        if p.name == name:
            return p
    raise ValueError(f"未找到 profile: {name}")

