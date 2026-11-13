"""Демо-гости: чтобы показать, как работают список гостей, статистика и Excel-выгрузка."""
import random
from datetime import datetime, timedelta

from .db import ASTANA_TZ

PREFIX = "demo_"
NAMES = ["aigerim", "daniyar", "madina", "timur", "aliya", "arman",
         "dana", "yerlan", "kamila", "nurlan", "saule", "ruslan"]
# сколько чашек у каждого: есть и новички с одной покупкой, и постоянные гости
CUPS = [14, 11, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]


def build_demo(drinks: list[str], now: datetime | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """Возвращает [(логин, [(дата-время, напиток), ...]), ...] за последние 30 дней."""
    now = now or datetime.now(ASTANA_TZ)
    rnd = random.Random(2027)  # одинаковый набор при каждом запуске
    guests = []
    for name, cups in zip(NAMES, CUPS):
        favorite = rnd.choice(drinks)
        rows = []
        for i in range(cups):
            # первая чашка у половины гостей — сегодня, остальные разбросаны по месяцу
            days_ago = 0 if i == 0 and rnd.random() < .5 else rnd.randint(0, 29)
            moment = (now - timedelta(days=days_ago)).replace(hour=rnd.randint(7, 22), minute=rnd.randint(0, 59),
                                                             second=rnd.randint(0, 59))
            if moment > now:
                moment = now - timedelta(minutes=rnd.randint(1, 90))
            drink = favorite if rnd.random() < .6 else rnd.choice(drinks)
            rows.append((moment.strftime("%Y-%m-%d %H:%M:%S"), drink))
        guests.append((PREFIX + name, sorted(rows)))
    return guests
