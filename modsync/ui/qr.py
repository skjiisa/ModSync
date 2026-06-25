"""Render a pairing code to a QR QPixmap. Returns None if qrcode isn't available."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap


def pairing_pixmap(text: str, size: int = 240) -> QPixmap | None:
    try:
        import qrcode
    except ImportError:
        return None

    qr = qrcode.QRCode(border=2)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    if n == 0:
        return None

    image = QImage(n, n, QImage.Format.Format_RGB32)
    black = QColor(0, 0, 0)
    white = QColor(255, 255, 255)
    for y, row in enumerate(matrix):
        for x, filled in enumerate(row):
            image.setPixelColor(x, y, black if filled else white)

    return QPixmap.fromImage(image).scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
