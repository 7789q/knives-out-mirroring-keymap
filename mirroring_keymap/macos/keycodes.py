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
    "F": 3,
    "H": 4,
    "G": 5,
    "Z": 6,
    "X": 7,
    "C": 8,
    "V": 9,
    "B": 11,
    "Q": 12,
    "W": 13,
    "E": 14,
    "R": 15,
    "T": 17,
    "Y": 16,
    "U": 32,
    "I": 34,
    "O": 31,
    "P": 35,
    "J": 38,
    "K": 40,
    "L": 37,
    "M": 46,
    "N": 45,
    # special
    "Tab": 48,
    "Space": 49,
    "Escape": 53,
    "CapsLock": 57,
    "Shift": 56,
    "Control": 59,
    "Option": 58,
    "UpArrow": 126,
    "DownArrow": 125,
    "LeftArrow": 123,
    "RightArrow": 124,
    "F8": 100,
    "F1": 122,
    "F2": 120,
    "F3": 99,
    "F4": 118,
    "F5": 96,
    "F6": 97,
    "F7": 98,
    "F9": 101,
    "F10": 109,
    "F11": 103,
    "F12": 111,
    # number row
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
    "0": 29,
}


def keycode_for(name: str) -> int:
    key = name.strip()
    if len(key) == 1:
        key = key.upper()
    if key not in KEYCODES:
        raise ValueError(f"未知按键: {name!r}（当前仅支持: {', '.join(sorted(KEYCODES))}）")
    return KEYCODES[key]
