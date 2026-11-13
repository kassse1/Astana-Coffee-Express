import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

DEFAULT_DRINKS = ["Эспрессо", "Американо", "Капучино", "Латте", "Мокко", "Горячий шоколад"]


@dataclass
class Config:
    token: str
    admin_ids: set[int] = field(default_factory=set)
    db_path: str = "data/coffee.xlsx"
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
        drinks=drinks or list(DEFAULT_DRINKS),
    )
