"""Генерация и распознавание штрихкодов (Code128)."""
import io
import re
from pathlib import Path

import barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

CARD_RE = re.compile(r"^ACE\d{6}$")


def make_barcode_png(code: str) -> bytes:
    buf = io.BytesIO()
    barcode.get("code128", code, writer=ImageWriter()).write(
        buf, options={"module_width": 0.4, "module_height": 18, "quiet_zone": 4, "font_size": 10}
    )
    return buf.getvalue()


SITE_DIR = Path(__file__).resolve().parent.parent / "site"
FONT = Path(barcode.__file__).parent / "fonts" / "DejaVuSansMono.ttf"


def make_card_png(code: str, login: str, design: int = 1) -> bytes:
    """Карта гостя как на сайте: выбранный фон, имя и штрихкод. Нет картинки фона — просто штрихкод."""
    try:
        photo = Image.open(SITE_DIR / "cards" / f"card-{design}.webp").convert("RGB")
    except OSError:
        return make_barcode_png(code)
    w, h = 960, 540
    scale = max(w / photo.width, h / photo.height)
    photo = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.LANCZOS)
    left, top = (photo.width - w) // 2, (photo.height - h) // 2
    photo = photo.crop((left, top, left + w, top + h))
    shade = Image.new("L", (1, h))  # затемнение снизу, чтобы подпись читалась на любом фоне
    shade.putdata([int(235 * max(0, (y - h * .45) / (h * .55)) ** 1.3) for y in range(h)])
    photo.paste(Image.new("RGB", (w, h), "#12100e"), mask=shade.resize((w, h)))

    card = Image.new("RGB", (w, h + 330), "#ffffff")
    card.paste(photo, (0, 0))
    draw = ImageDraw.Draw(card)
    x = 40
    try:
        icon = Image.open(SITE_DIR / "icon.png").convert("RGBA").resize((88, 88), Image.LANCZOS)
        card.paste(icon, (x, h - 128), icon)
        x += 108
    except OSError:
        pass
    draw.text((x, h - 126), "Astana Coffee Express", font=ImageFont.truetype(str(FONT), 36), fill="#ffffff")
    draw.text((x, h - 80), f"Карта гостя · {login}"[:40], font=ImageFont.truetype(str(FONT), 28), fill="#e0b56a")

    bars = Image.open(io.BytesIO(make_barcode_png(code))).convert("RGB")
    bars = bars.resize((round(bars.width * 290 / bars.height), 290), Image.LANCZOS)
    if bars.width > w - 40:
        bars = bars.resize((w - 40, round(bars.height * (w - 40) / bars.width)), Image.LANCZOS)
    card.paste(bars, ((w - bars.width) // 2, h + (330 - bars.height) // 2))
    buf = io.BytesIO()
    card.save(buf, "PNG")
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
