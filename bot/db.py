"""Excel-база (один .xlsx файл).

Листы:
  Пользователи — кто зарегистрирован, сколько выпил и любимый напиток
  Покупки      — журнал каждой отмеченной чашки
  Напитки      — общий рейтинг напитков
  Акции        — история рассылок
"""
import hashlib
import hmac
import os
import secrets
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

S_USERS, S_LOG, S_DRINKS, S_PROMO = "Пользователи", "Покупки", "Напитки", "Акции"
HEADERS = {
    S_USERS: ["ID", "Логин", "Хеш пароля", "Telegram ID", "Код карты", "Дата регистрации",
              "Всего кофе", "Любимый напиток", "Дизайн карты"],
    S_LOG: ["Дата", "ID пользователя", "Логин", "Напиток"],
    S_DRINKS: ["Напиток", "Сколько раз взяли"],
    S_PROMO: ["Дата", "Текст", "Получателей"],
}
WIDTHS = {S_USERS: [6, 20, 16, 16, 14, 20, 12, 20, 14], S_LOG: [20, 16, 20, 20],
          S_DRINKS: [22, 20], S_PROMO: [20, 70, 14]}


class DBError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


@dataclass
class User:
    id: int
    login: str
    tg_id: int | None
    card: str
    registered: str
    total: int
    favorite: str | None
    design: int = 1  # какой фон карты выбрал гость (1–6)


DESIGNS = range(1, 7)


ASTANA_TZ = timezone(timedelta(hours=5))  # сервер живёт в UTC, а время пишем астанинское


def _now() -> str:
    return datetime.now(ASTANA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def validate_credentials(login: str, password: str) -> str:
    login = login.strip()
    if not (3 <= len(login) <= 20) or not login.replace("_", "").isalnum():
        raise DBError("Логин: 3–20 символов, только буквы, цифры и _.")
    if len(password) < 4:
        raise DBError("Пароль слишком короткий (минимум 4 символа).")
    return login


def _hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${digest}"


def _check(password: str, stored: str) -> bool:
    salt = str(stored).split("$", 1)[0]
    return hmac.compare_digest(_hash(password, salt), str(stored))


def _safe(text: str) -> str:
    """Чтобы Excel не принял пользовательский текст за формулу."""
    return "'" + text if text[:1] in "=+-@" else text


class ExcelDB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        if os.path.exists(path):
            self.wb = load_workbook(path)
            for name in HEADERS:
                if name not in self.wb.sheetnames:
                    self._make_sheet(name)
            head = self.wb[S_USERS].cell(row=1, column=9)  # файл от старой версии: добавляем колонку
            if not head.value:
                head.value, head.font = HEADERS[S_USERS][8], Font(bold=True)
        else:
            self.wb = Workbook()
            self.wb.remove(self.wb.active)
            for name in HEADERS:
                self._make_sheet(name)
            self._save()

    # ---------- служебное ----------
    def _make_sheet(self, name: str) -> None:
        ws = self.wb.create_sheet(name)
        ws.append(HEADERS[name])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for i, width in enumerate(WIDTHS[name]):
            ws.column_dimensions[chr(ord("A") + i)].width = width
        ws.freeze_panes = "A2"

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        self.wb.save(tmp)
        os.replace(tmp, self.path)  # файл не побьётся, если бот упадёт во время записи

    def _user_rows(self):
        return [r for r in self.wb[S_USERS].iter_rows(min_row=2) if r[0].value is not None]

    @staticmethod
    def _to_user(row) -> User:
        v = [c.value for c in row]
        return User(id=int(v[0]), login=str(v[1]), tg_id=int(v[3]) if v[3] else None, card=str(v[4]),
                    registered=str(v[5]), total=int(v[6] or 0), favorite=v[7] or None,
                    design=int(v[8]) if len(v) > 8 and v[8] else 1)

    def _find(self, col: int, value):
        for row in self._user_rows():
            cell = row[col].value
            if cell is not None and str(cell).lower() == str(value).lower():
                return row
        return None

    def _unlink_tg(self, tg_id: int) -> None:
        for row in self._user_rows():
            if row[3].value and int(row[3].value) == tg_id:
                row[3].value = None

    # ---------- пользователи ----------
    def register(self, login: str, password: str, tg_id: int | None) -> User:
        login = validate_credentials(login, password)
        with self._lock:
            if self._find(1, login):
                raise DBError("Такой логин уже занят.")
            if tg_id is not None:
                self._unlink_tg(tg_id)
            new_id = max((int(r[0].value) for r in self._user_rows()), default=0) + 1
            self.wb[S_USERS].append(
                [new_id, login, _hash(password), tg_id, f"ACE{new_id:06d}", _now(), 0, None])
            self._save()
            return self._to_user(self._find(0, new_id))

    def login(self, login: str, password: str, tg_id: int) -> User:
        with self._lock:
            row = self._find(1, login.strip())
            if not row or not _check(password, row[2].value):
                raise DBError("Неверный логин или пароль.")
            self._unlink_tg(tg_id)
            row[3].value = tg_id
            self._save()
            return self._to_user(row)

    def verify(self, login: str, password: str) -> User:
        """Проверка логина и пароля без привязки к Telegram (для входа с сайта)."""
        with self._lock:
            row = self._find(1, login.strip())
            if not row or not _check(password, row[2].value):
                raise DBError("Неверный логин или пароль.")
            return self._to_user(row)

    def by_id(self, user_id: int) -> User | None:
        with self._lock:
            row = self._find(0, user_id)
            return self._to_user(row) if row else None

    def set_design(self, user_id: int, design: int) -> User:
        if design not in DESIGNS:
            raise DBError("Такого дизайна нет.")
        with self._lock:
            row = self._find(0, user_id)
            if not row:
                raise DBError("Пользователь не найден.")
            self.wb[S_USERS].cell(row=row[0].row, column=9).value = design
            self._save()
            return self._to_user(self._find(0, user_id))

    def logout(self, tg_id: int) -> None:
        with self._lock:
            self._unlink_tg(tg_id)
            self._save()

    def by_tg(self, tg_id: int) -> User | None:
        with self._lock:
            row = self._find(3, tg_id)
            return self._to_user(row) if row else None

    def by_card(self, card: str) -> User | None:
        with self._lock:
            row = self._find(4, card)
            return self._to_user(row) if row else None

    def all_users(self) -> list[User]:
        with self._lock:
            return [self._to_user(r) for r in self._user_rows()]

    # ---------- кофе ----------
    def add_coffee(self, user_id: int, drink: str) -> User:
        with self._lock:
            row = self._find(0, user_id)
            if not row:
                raise DBError("Пользователь не найден.")
            self.wb[S_LOG].append([_now(), user_id, row[1].value, drink])
            counts = self._counts(user_id)
            row[6].value = sum(counts.values())
            row[7].value = counts.most_common(1)[0][0]
            self._rebuild_drinks()
            self._save()
            return self._to_user(row)

    def _counts(self, user_id: int | None = None) -> Counter:
        c = Counter()
        for _, uid, _, drink in self.wb[S_LOG].iter_rows(min_row=2, max_col=4, values_only=True):
            if drink and (user_id is None or uid == user_id):
                c[drink] += 1
        return c

    def _rebuild_drinks(self) -> None:
        ws = self.wb[S_DRINKS]
        if ws.max_row > 1:
            ws.delete_rows(2, ws.max_row - 1)
        for drink, n in self._counts().most_common():
            ws.append([drink, n])

    def purchases(self) -> list[tuple[str, int, str]]:
        """Журнал покупок: (дата-время, id гостя, напиток)."""
        with self._lock:
            return [(str(ts), int(uid), drink) for ts, uid, _, drink in
                    self.wb[S_LOG].iter_rows(min_row=2, max_col=4, values_only=True) if drink]

    def user_stats(self, user_id: int) -> list[tuple[str, int]]:
        with self._lock:
            return self._counts(user_id).most_common()

    def drink_stats(self) -> list[tuple[str, int]]:
        with self._lock:
            return self._counts().most_common()

    # ---------- акции ----------
    def log_promo(self, text: str, recipients: int) -> None:
        with self._lock:
            self.wb[S_PROMO].append([_now(), _safe(text), recipients])
            self._save()

    # ---------- демо-гости ----------
    def seed_demo(self, guests) -> int:
        """guests: [(логин, [(дата-время, напиток), ...]), ...]. Возвращает число созданных гостей."""
        with self._lock:
            new_id = max((int(r[0].value) for r in self._user_rows()), default=0)
            log_rows = []
            for login, rows in guests:
                if self._find(1, login):
                    continue
                new_id += 1
                counts = Counter(drink for _, drink in rows)
                first = min((ts for ts, _ in rows), default=_now())
                self.wb[S_USERS].append([new_id, login, _hash(secrets.token_hex(16)), None, f"ACE{new_id:06d}",
                                         first, len(rows), counts.most_common(1)[0][0] if rows else None])
                log_rows += [(ts, new_id, login, drink) for ts, drink in rows]
            for row in sorted(log_rows):
                self.wb[S_LOG].append(list(row))
            self._rebuild_drinks()
            self._save()
            return len({r[1] for r in log_rows})

    def clear_demo(self, prefix: str) -> int:
        """Удаляет гостей с логином на prefix и их покупки. Возвращает число удалённых гостей."""
        with self._lock:
            ids = {int(r[0].value) for r in self._user_rows() if str(r[1].value).startswith(prefix)}
            for name, col in ((S_USERS, 0), (S_LOG, 1)):
                ws = self.wb[name]
                for idx in range(ws.max_row, 1, -1):
                    value = ws.cell(row=idx, column=col + 1).value
                    if value is not None and int(value) in ids:
                        ws.delete_rows(idx)
            self._rebuild_drinks()
            self._save()
            return len(ids)

    # ---------- выгрузка ----------
    def export_xlsx(self) -> bytes:
        with self._lock, open(self.path, "rb") as f:
            return f.read()


def open_db(cfg):
    """DATABASE_URL задан — облачная база (Postgres), иначе локальный Excel-файл."""
    if cfg.database_url:
        from .sqldb import SqlDB
        return SqlDB(cfg.database_url)
    return ExcelDB(cfg.db_path)
