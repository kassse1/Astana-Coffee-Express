import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

DEFAULT_DRINKS = ["Эспрессо", "Американо", "Капучино", "Латте", "Мокко", "Горячий шоколад"]


@dataclass
class Config:
    token: str
    admin_ids: set[int] = field(default_factory=set)
    db_path: str = "data/coffee.xlsx"
    database_url: str = ""
    webhook_url: str = ""
    port: int = 10000
    site_origin: str = "*"  # с какого адреса сайту разрешено обращаться к API
    free_every: int = 6  # каждая N-я чашка бесплатно; 0 — программа выключена
    drinks: list[str] = field(default_factory=lambda: list(DEFAULT_DRINKS))


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и заполните.")
    admins = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
    drinks = [d.strip() for d in os.getenv("DRINKS", "").split(",") if d.strip()]
    return Config(
        token=token,
        admin_ids=admins,
        db_path=os.getenv("DB_PATH", "data/coffee.xlsx"),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        # Render сам задаёт RENDER_EXTERNAL_URL и PORT для Web Service
        webhook_url=(os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/"),
        port=int(os.getenv("PORT", "10000")),
        site_origin=os.getenv("SITE_ORIGIN", "*").strip().rstrip("/") or "*",
        free_every=max(0, int(os.getenv("FREE_EVERY", "6"))),
        drinks=drinks or list(DEFAULT_DRINKS),
    )
