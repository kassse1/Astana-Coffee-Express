"""Генерация и распознавание штрихкодов (Code128)."""
import io
import re

import barcode
from barcode.writer import ImageWriter
from PIL import Image

CARD_RE = re.compile(r"^ACE\d{6}$")


def make_barcode_png(code: str) -> bytes:
    buf = io.BytesIO()
    barcode.get("code128", code, writer=ImageWriter()).write(
        buf, options={"module_width": 0.4, "module_height": 18, "quiet_zone": 4, "font_size": 10}
    )
    return buf.getvalue()


def read_barcode(image_bytes: bytes) -> str | None:
    """Возвращает код карты с фото или None, если не распознан."""
    import zxingcpp

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    for result in zxingcpp.read_barcodes(img):
        text = result.text.strip().upper()
        if CARD_RE.match(text):
            return text
    return None


def normalize_code(text: str) -> str | None:
    """Принимает 'ACE000012', 'ace000012' или просто '12'."""
    text = text.strip().upper()
    if text.isdigit() and len(text) <= 6:
        text = f"ACE{int(text):06d}"
    return text if CARD_RE.match(text) else None
