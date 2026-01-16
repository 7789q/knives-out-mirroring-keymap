from __future__ import annotations

import sys


def require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("该功能仅支持 macOS（Darwin）")

