from __future__ import annotations

import logging

from mirroring_keymap.ui_app import UIApp


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    UIApp().run()


if __name__ == "__main__":
    main()
