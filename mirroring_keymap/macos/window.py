from __future__ import annotations

from dataclasses import dataclass

from ..config import TargetWindowConfig
from . import require_macos


@dataclass(frozen=True)
class FrontmostInfo:
    pid: int
    name: str


def get_frontmost() -> FrontmostInfo:
    require_macos()
    from AppKit import NSWorkspace  # pyobjc

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return FrontmostInfo(pid=-1, name="")
    return FrontmostInfo(pid=int(app.processIdentifier()), name=str(app.localizedName() or ""))


def is_target_frontmost(cfg: TargetWindowConfig) -> bool:
    """
    MVP 策略：
    - 如果配置了 pid：要求前台 pid 匹配
    - 否则：前台应用存在且其窗口列表中存在包含 titleHint 的窗口名
    """
    require_macos()
    front = get_frontmost()

    if cfg.pid is not None:
        return front.pid == cfg.pid

    title_hint = (cfg.titleHint or "").strip().lower()
    if not title_hint:
        return False

    import Quartz

    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for w in wins:
        try:
            owner_pid = int(w.get("kCGWindowOwnerPID", -1))
            if owner_pid != front.pid:
                continue
            name = str(w.get("kCGWindowName") or "")
            if title_hint in name.lower():
                return True
        except Exception:
            continue
    return False

