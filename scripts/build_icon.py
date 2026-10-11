"""从程序的 SVG 图标生成 Windows 多尺寸 ICO 与 PNG 预览。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import struct

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication


def main():
    app = QApplication([])
    folder = Path(__file__).resolve().parent.parent / "voice_workbench" / "assets"
    renderer = QSvgRenderer(str(folder / "app.svg"))
    if not renderer.isValid():
        raise ValueError("程序图标 SVG 无法读取")
    images = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise RuntimeError("无法生成图标 PNG")
        images.append((size, bytes(buffer.data())))
        if size == 256:
            image.save(str(folder / "app.png"))
    offset = 6 + 16 * len(images)
    header = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for size, data in images:
        header.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    (folder / "app.ico").write_bytes(header + b"".join(data for _, data in images))
    print("Generated app.ico (16–256 px) and app.png")


if __name__ == "__main__":
    main()
