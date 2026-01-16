from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import AppConfig, ProfileConfig, load_config, select_profile


def _is_macos() -> bool:
    return sys.platform == "darwin"


@dataclass
class Runtime:
    cfg_path: Path
    cfg: AppConfig
    profile: ProfileConfig
    injector: object
    engine: object
    capture: object


class UIApp:
    def __init__(self) -> None:
        self._log = logging.getLogger("mirroring_keymap.ui")
        self._runtime: Optional[Runtime] = None

    # --------------------
    # Public entry
    # --------------------

    def run(self) -> None:
        if not _is_macos():
            raise RuntimeError("UI 仅支持 macOS（Darwin）")

        # 延迟导入，避免非 macOS 环境 import AppKit 失败
        from AppKit import NSApplication

        app = NSApplication.sharedApplication()

        from .ui_cocoa import AppDelegate

        delegate = AppDelegate.alloc().initWithApp_(self)
        app.setDelegate_(delegate)
        app.run()

    # --------------------
    # Runtime control (called by Cocoa delegate)
    # --------------------

    def load_config(self, path: str) -> tuple[AppConfig, list[str]]:
        cfg = load_config(path)
        profile_names = [p.name for p in cfg.profiles]
        return cfg, profile_names

    def start(self, cfg_path: str, profile_name: Optional[str]) -> None:
        if self._runtime is not None:
            self.stop()

        cfg_path_p = Path(cfg_path).expanduser()
        cfg = load_config(cfg_path_p)
        profile = select_profile(cfg, profile_name)

        from .engine import Engine
        from .macos.injector import Injector
        from .macos.input_capture import InputCapture

        inj = Injector()
        engine = Engine(cfg, profile, inj, logger=self._log)
        capture = InputCapture(engine.handle_event, ignore_user_data_tag=inj.user_data_tag, logger=self._log)

        engine.start()
        capture.start()

        self._runtime = Runtime(
            cfg_path=cfg_path_p,
            cfg=cfg,
            profile=profile,
            injector=inj,
            engine=engine,
            capture=capture,
        )

    def stop(self) -> None:
        rt = self._runtime
        self._runtime = None
        if rt is None:
            return
        try:
            rt.capture.stop()
        except Exception:
            pass
        try:
            rt.engine.stop()
        except Exception:
            pass

    def is_running(self) -> bool:
        return self._runtime is not None

    def snapshot(self) -> dict[str, object]:
        rt = self._runtime
        if rt is None:
            return {"running": False}
        snap = rt.engine.snapshot()
        snap.update(
            {
                "running": True,
                "profile": rt.profile.name,
                "config": str(rt.cfg_path),
            }
        )
        return snap

    def set_mapping_enabled(self, enabled: bool) -> None:
        rt = self._runtime
        if rt is None:
            return
        rt.engine.set_mapping_enabled(enabled)

    def set_camera_lock(self, enabled: bool) -> None:
        rt = self._runtime
        if rt is None:
            return
        rt.engine.set_camera_lock(enabled)

    def toggle_backpack(self) -> None:
        rt = self._runtime
        if rt is None:
            return
        rt.engine.toggle_backpack()

    def panic(self) -> None:
        rt = self._runtime
        if rt is None:
            return
        rt.engine.panic()

    def open_in_editor(self, path: str) -> None:
        p = str(Path(path).expanduser())
        os.system(f"open {json.dumps(p)}")
