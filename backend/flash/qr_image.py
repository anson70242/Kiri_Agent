"""The flash account's QR code drawn as an image the 通知 page can show.

The flash service gives the QR code as text; the owner scans the drawn code with the phone.
The text is a login: it is drawn here and written to no log line.
"""

from __future__ import annotations

import segno


def qr_image(text: str) -> str:
    """`text` drawn as a QR code: an SVG as a `data:` URI, for an `<img>`'s src."""
    # Black on white with the standard quiet zone: what a phone's camera reads best.
    return segno.make(text, error="m").svg_data_uri(scale=6, border=4, dark="#000", light="#fff")
