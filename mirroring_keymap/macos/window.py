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
    - 兼容：若 pid 不匹配但 titleHint 仍能匹配，则视为命中（便于 iPhone Mirroring 重启后 pid 变化）
    """
    require_macos()
    front = get_frontmost()

    title_hint = (cfg.titleHint or "").strip().lower()
    if cfg.pid is not None and front.pid == cfg.pid:
        return True

    import Quartz

    # 若配置了 pid 但不匹配：允许用 titleHint 做 fallback（更稳的体验）
    if cfg.pid is not None and not title_hint:
        return False
    if not title_hint:
        return False

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


def get_frontmost_debug(max_windows: int = 10) -> dict[str, object]:
    """
    返回前台应用与其窗口标题（用于 UI 调试）。
    不保证窗口标题一定存在（很多窗口 kCGWindowName 为空）。
    """
    require_macos()
    front = get_frontmost()

    titles: list[str] = []
    try:
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
                title = str(w.get("kCGWindowName") or "").strip()
                if not title:
                    continue
                titles.append(title)
                if len(titles) >= max_windows:
                    break
            except Exception:
                continue
    except Exception:
        titles = []

    return {"pid": front.pid, "name": front.name, "windows": titles}
