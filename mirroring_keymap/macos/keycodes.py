from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyDef:
    name: str
    keycode: int


# 仅覆盖 MVP 需要的按键；后续可扩展/改为动态布局映射。
KEYCODES: dict[str, int] = {
    # letters
    "A": 0,
    "S": 1,
    "D": 2,
    "W": 13,
    "E": 14,
    # special
    "Tab": 48,
    "CapsLock": 57,
    "F8": 100,
    "F12": 111,
}


def keycode_for(name: str) -> int:
    key = name.strip()
    if len(key) == 1:
        key = key.upper()
    if key not in KEYCODES:
        raise ValueError(f"未知按键: {name!r}（当前仅支持: {', '.join(sorted(KEYCODES))}）")
    return KEYCODES[key]

