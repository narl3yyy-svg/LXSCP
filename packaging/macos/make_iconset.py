"""Render lxscp.svg into a macOS .iconset folder (turned into .icns by `iconutil`).

usage: python make_iconset.py lxscp.svg out/lxscp.iconset
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PyQt6.QtSvg import QSvgRenderer  # noqa: E402


def main(svg: str, out_dir: str) -> None:
    app = QGuiApplication(sys.argv[:1])  # noqa: F841 - needed for painting
    renderer = QSvgRenderer(svg)
    if not renderer.isValid():
        raise SystemExit(f"cannot read {svg}")
    os.makedirs(out_dir, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            image = QImage(pixels, pixels, QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            renderer.render(painter)
            painter.end()
            suffix = "@2x" if scale == 2 else ""
            image.save(os.path.join(out_dir, f"icon_{size}x{size}{suffix}.png"))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
