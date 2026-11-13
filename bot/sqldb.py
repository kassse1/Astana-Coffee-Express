"""База в PostgreSQL (или SQLite) — для бесплатных облачных сервисов вроде Neon.

Интерфейс тот же, что у ExcelDB; Excel-файл собирается по запросу в export_xlsx().
"""
import io
import threading
from collections import Counter, defaultdict

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import (BigInteger, Column, ForeignKey, Integer, MetaData, String, Table, Text,
                        create_engine, delete, func, insert, select, update)
from sqlalchemy.exc import IntegrityError

import secrets

from .db import (HEADERS, S_DRINKS, S_LOG, S_PROMO, S_USERS, WIDTHS, DBError, User, _check, _hash,
                 _now, _safe, validate_credentials)

meta = MetaData()
users = Table(
    "users", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("login", String(20), nullable=False),
    Column("login_lower", String(20), nullable=False, unique=True),
    Column("password_hash", String(100), nullable=False),
    Column("tg_id", BigInteger, index=True),
    Column("registered", String(19), nullable=False),
)
purchases = Table(
    "purchases", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(19), nullable=False),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False, index=True),
    Column("drink", String(100), nullable=False),
)
promos = Table(
    "promos", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(19), nullable=False),
    Column("text", Text, nullable=False),
    Column("recipients", Integer, nullable=False),
)


def normalize_url(url: str) -> str:
    """Neon/Supabase/Render дают postgres://… — SQLAlchemy нужен явный драйвер."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class SqlDB:
    def __init__(self, url: str):
        # pre_ping/recycle: бесплатные базы усыпляют соединения при простое
        self.engine = create_engine(normalize_url(url), pool_pre_ping=True, pool_recycle=300)
        self._lock = threading.Lock()
        meta.create_all(self.engine)

    # ---------- служебное ----------
    def _counts(self, conn) -> dict[int, Counter]:
        result: dict[int, Counter] = defaultdict(Counter)
        # min(id) — чтобы при равенстве побеждал напиток, взятый раньше (как в Excel-версии)
        q = (select(purchases.c.user_id, purchases.c.drink, func.count(), func.min(purchases.c.id))
             .group_by(purchases.c.user_id, purchases.c.drink).order_by(func.min(purchases.c.id)))
        for uid, drink, n, _ in conn.execute(q):
            result[uid][drink] = n
        return result

    @staticmethod
    def _to_user(row, counts: Counter | None) -> User:
        counts = counts or Counter()
        return User(id=row.id, login=row.login, tg_id=row.tg_id, card=f"ACE{row.id:06d}",
                    registered=row.registered, total=sum(counts.values()),
                    favorite=counts.most_common(1)[0][0] if counts else None)

    def _one(self, conn, where) -> User | None:
        row = conn.execute(select(users).where(where)).first()
        return self._to_user(row, self._counts(conn).get(row.id)) if row else None

    # ---------- пользователи ----------
    def register(self, login: str, password: str, tg_id: int) -> User:
        login = validate_credentials(login, password)
        with self._lock, self.engine.begin() as conn:
            try:
                conn.execute(update(users).where(users.c.tg_id == tg_id).values(tg_id=None))
                new_id = conn.execute(insert(users).values(
                    login=login, login_lower=login.lower(), password_hash=_hash(password),
                    tg_id=tg_id, registered=_now())).inserted_primary_key[0]
            except IntegrityError:
                raise DBError("Такой логин уже занят.") from None
            return self._one(conn, users.c.id == new_id)

    def login(self, login: str, password: str, tg_id: int) -> User:
        with self._lock, self.engine.begin() as conn:
            row = conn.execute(select(users).where(users.c.login_lower == login.strip().lower())).first()
            if not row or not _check(password, row.password_hash):
                raise DBError("Неверный логин или пароль.")
            conn.execute(update(users).where(users.c.tg_id == tg_id).values(tg_id=None))
            conn.execute(update(users).where(users.c.id == row.id).values(tg_id=tg_id))
            return self._one(conn, users.c.id == row.id)

    def logout(self, tg_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(users).where(users.c.tg_id == tg_id).values(tg_id=None))

    def by_tg(self, tg_id: int) -> User | None:
        with self.engine.connect() as conn:
            return self._one(conn, users.c.tg_id == tg_id)

    def by_card(self, card: str) -> User | None:
        digits = card.strip().upper().removeprefix("ACE")
        if not digits.isdigit():
            return None
        with self.engine.connect() as conn:
            return self._one(conn, users.c.id == int(digits))

    def all_users(self) -> list[User]:
        with self.engine.connect() as conn:
            counts = self._counts(conn)
            return [self._to_user(r, counts.get(r.id)) for r in conn.execute(select(users).order_by(users.c.id))]

    # ---------- кофе ----------
    def add_coffee(self, user_id: int, drink: str) -> User:
        with self.engine.begin() as conn:
            if not conn.execute(select(users.c.id).where(users.c.id == user_id)).first():
                raise DBError("Пользователь не найден.")
            conn.execute(insert(purchases).values(ts=_now(), user_id=user_id, drink=drink))
            return self._one(conn, users.c.id == user_id)

    def purchases(self) -> list[tuple[str, int, str]]:
        """Журнал покупок: (дата-время, id гостя, напиток)."""
        with self.engine.connect() as conn:
            q = select(purchases.c.ts, purchases.c.user_id, purchases.c.drink).order_by(purchases.c.id)
            return [tuple(r) for r in conn.execute(q)]

    def user_stats(self, user_id: int) -> list[tuple[str, int]]:
        with self.engine.connect() as conn:
            return self._counts(conn).get(user_id, Counter()).most_common()

    def drink_stats(self) -> list[tuple[str, int]]:
        with self.engine.connect() as conn:
            total = Counter()
            q = select(purchases.c.drink).order_by(purchases.c.id)
            for (drink,) in conn.execute(q):
                total[drink] += 1
            return total.most_common()

    # ---------- акции ----------
    def log_promo(self, text: str, recipients: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(promos).values(ts=_now(), text=text, recipients=recipients))

    # ---------- демо-гости ----------
    def seed_demo(self, guests) -> int:
        """guests: [(логин, [(дата-время, напиток), ...]), ...]. Возвращает число созданных гостей."""
        created = 0
        with self._lock, self.engine.begin() as conn:
            taken = {r[0] for r in conn.execute(select(users.c.login_lower))}
            for login, rows in guests:
                if login.lower() in taken:
                    continue
                first = min((ts for ts, _ in rows), default=_now())
                new_id = conn.execute(insert(users).values(
                    login=login, login_lower=login.lower(), password_hash=_hash(secrets.token_hex(16)),
                    tg_id=None, registered=first)).inserted_primary_key[0]
                if rows:
                    conn.execute(insert(purchases), [{"ts": ts, "user_id": new_id, "drink": d} for ts, d in rows])
                created += 1
        return created

    def clear_demo(self, prefix: str) -> int:
        """Удаляет гостей с логином на prefix и их покупки. Возвращает число удалённых гостей."""
        with self._lock, self.engine.begin() as conn:
            ids = [r[0] for r in conn.execute(select(users.c.id).where(users.c.login_lower.startswith(prefix.lower())))]
            if ids:
                conn.execute(delete(purchases).where(purchases.c.user_id.in_(ids)))
                conn.execute(delete(users).where(users.c.id.in_(ids)))
            return len(ids)

    # ---------- выгрузка в Excel ----------
    def export_xlsx(self) -> bytes:
        all_users = self.all_users()
        logins = {u.id: u.login for u in all_users}
        with self.engine.connect() as conn:
            log = conn.execute(select(purchases).order_by(purchases.c.id)).all()
            promo_rows = conn.execute(select(promos).order_by(promos.c.id)).all()
            hashes = dict(conn.execute(select(users.c.id, users.c.password_hash)).all())
        rows = {
            S_USERS: [[u.id, u.login, hashes[u.id], u.tg_id, u.card, u.registered, u.total, u.favorite]
                      for u in all_users],
            S_LOG: [[p.ts, p.user_id, logins.get(p.user_id), p.drink] for p in log],
            S_DRINKS: [list(x) for x in self.drink_stats()],
            S_PROMO: [[p.ts, _safe(p.text), p.recipients] for p in promo_rows],
        }
        wb = Workbook()
        wb.remove(wb.active)
        for name, header in HEADERS.items():
            ws = wb.create_sheet(name)
            ws.append(header)
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for i, width in enumerate(WIDTHS[name]):
                ws.column_dimensions[chr(ord("A") + i)].width = width
            ws.freeze_panes = "A2"
            for row in rows[name]:
                ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
