from __future__ import annotations

import json
import logging
from collections import deque
from logging.handlers import RotatingFileHandler
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

from .config import AppConfig, ProfileConfig, load_config, select_profile
from .default_config import DEFAULT_CONFIG_JSON


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


class _RingBufferHandler(logging.Handler):
    def __init__(self, buf: deque[str], lock: Lock) -> None:
        super().__init__()
        self._buf = buf
        self._lock = lock

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with self._lock:
            self._buf.append(msg)


class UIApp:
    def __init__(self) -> None:
        self._log = logging.getLogger("mirroring_keymap.ui")
        self._runtime: Optional[Runtime] = None

        # 供 UI 展示的最近日志（不依赖控制台）
        self._log_buf: deque[str] = deque(maxlen=400)
        self._log_lock = Lock()
        self._log_path = self._default_log_path()
        self._file_handler: Optional[RotatingFileHandler] = None
        self._ensure_logging()

    def _default_log_path(self) -> Path:
        return Path.home() / "Library" / "Logs" / "MirroringKeymap" / "mirroring_keymap.log"

    def log_path(self) -> str:
        return str(self._log_path)

    def _ensure_logging(self) -> None:
        root = logging.getLogger()

        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

        # File handler
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                str(self._log_path),
                maxBytes=512 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            root.addHandler(fh)
            self._file_handler = fh
        except Exception:
            # 不阻塞启动（例如无权限/路径不可写）
            pass

        # Ring buffer handler (for in-app view)
        bh = _RingBufferHandler(self._log_buf, self._log_lock)
        bh.setLevel(logging.DEBUG)
        bh.setFormatter(fmt)
        root.addHandler(bh)

        # 默认 INFO；用户可在 UI 中切换到 DEBUG
        if root.level == logging.NOTSET:
            root.setLevel(logging.INFO)

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

    def load_config_dict(self, path: str) -> dict:
        p = Path(path).expanduser()
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是 JSON Object")
        return data

    def save_config_dict(self, path: str, data: dict) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def get_recent_logs(self, max_lines: int = 200) -> str:
        with self._log_lock:
            lines = list(self._log_buf)[-max_lines:]
        return "\n".join(lines)

    def clear_logs(self) -> None:
        with self._log_lock:
            self._log_buf.clear()
        # 尽量原地截断（避免 file handler 仍持有旧 fd 导致写入偏移异常）
        try:
            fh = self._file_handler
            if fh is not None:
                try:
                    fh.acquire()
                    if fh.stream is None:
                        fh.stream = fh._open()  # type: ignore[attr-defined]
                    fh.stream.seek(0)
                    fh.stream.truncate(0)
                    fh.stream.flush()
                finally:
                    fh.release()
            else:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_path.write_text("", encoding="utf-8")
        except Exception:
            pass

    def default_config_path(self) -> str:
        base = Path.home() / "Library" / "Application Support" / "MirroringKeymap"
        return str(base / "config.json")

    def ensure_default_config_exists(self, path: str) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(DEFAULT_CONFIG_JSON, encoding="utf-8")

    def start(self, cfg_path: str, profile_name: Optional[str]) -> None:
        if self._runtime is not None:
            self.stop()
        # 用户诉求：每次启动服务都清空旧日志，便于排查本次问题
        self.clear_logs()

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

    def click_markers(self) -> list[dict[str, object]]:
        rt = self._runtime
        if rt is None:
            return []
        try:
            return rt.engine.click_markers()
        except Exception:
            return []

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
