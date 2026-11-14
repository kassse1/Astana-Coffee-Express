"""Админ-панель: аппараты, продажи, заявки, цена и акция.

Данные панели лежат в той же облачной базе, что и гости (DATABASE_URL), а при локальном запуске
с Excel — в файле admin.db рядом с ним. Вход в панель — по ссылке, которую бот выдаёт админу
командой /admin (отдельный пароль не нужен).
"""
import asyncio
import hashlib
import hmac
import os
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta

from aiohttp import web
from sqlalchemy import (Column, Integer, MetaData, String, Table, Text, create_engine, func, insert,
                        select, update)

from .config import Config
from .db import ASTANA_TZ, _now

TOKEN_TTL = 12 * 3600  # ссылка в панель живёт 12 часов
KINDS = {"placement": "Заявка на установку", "complaint": "Жалоба", "refund": "Возврат"}

meta = MetaData()
machines = Table(
    "machines", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(80), nullable=False),
    Column("address", String(200), nullable=False, default=""),
    Column("kind", String(40), nullable=False, default=""),
    Column("active", Integer, nullable=False, default=1),
    Column("created", String(19), nullable=False),
)
sales = Table(
    "sales", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(19), nullable=False, index=True),
    Column("user_id", Integer, nullable=False),
    Column("drink", String(100), nullable=False),
    Column("machine_id", Integer),
    Column("price", Integer, nullable=False),
    Column("free", Integer, nullable=False, default=0),
)
requests = Table(
    "requests", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(19), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("name", String(80), nullable=False),
    Column("contact", String(120), nullable=False),
    Column("text", Text, nullable=False),
    Column("machine_id", Integer),
    Column("status", String(10), nullable=False, default="new"),
    Column("note", Text, nullable=False, default=""),
)
settings = Table("settings", meta, Column("key", String(40), primary_key=True), Column("value", Text, nullable=False))


class AdminError(Exception):
    """Ошибка с текстом, который можно показать в панели."""


def _clean(value, limit: int, required: bool = False) -> str:
    text = " ".join(str(value or "").split())[:limit]
    if required and not text:
        raise AdminError("Заполните обязательные поля.")
    return text


class AdminStore:
    def __init__(self, cfg: Config):
        if cfg.database_url:
            from .sqldb import normalize_url
            url = normalize_url(cfg.database_url)
        else:
            folder = os.path.dirname(os.path.abspath(cfg.db_path))
            os.makedirs(folder, exist_ok=True)
            url = f"sqlite:///{os.path.join(folder, 'admin.db')}"
        self.engine = create_engine(url, pool_pre_ping=True, pool_recycle=300)
        meta.create_all(self.engine)

    # ---------- настройки ----------
    def get(self, key: str, default: str = "") -> str:
        with self.engine.connect() as conn:
            row = conn.execute(select(settings.c.value).where(settings.c.key == key)).first()
            return row[0] if row else default

    def put(self, key: str, value: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(update(settings).where(settings.c.key == key).values(value=value)).rowcount:
                conn.execute(insert(settings).values(key=key, value=value))

    def price(self) -> int:
        return int(self.get("price", "600"))

    # ---------- аппараты ----------
    def machines(self, only_active: bool = False) -> list[dict]:
        q = select(machines).order_by(machines.c.id)
        if only_active:
            q = q.where(machines.c.active == 1)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def save_machine(self, data: dict) -> int:
        values = {"name": _clean(data.get("name"), 80, required=True), "address": _clean(data.get("address"), 200),
                  "kind": _clean(data.get("kind"), 40), "active": 1 if data.get("active", True) else 0}
        with self.engine.begin() as conn:
            if data.get("id"):
                if not conn.execute(update(machines).where(machines.c.id == int(data["id"])).values(**values)).rowcount:
                    raise AdminError("Аппарат не найден.")
                return int(data["id"])
            return conn.execute(insert(machines).values(created=_now(), **values)).inserted_primary_key[0]

    # ---------- продажи ----------
    def add_sale(self, user_id: int, drink: str, machine_id: int | None, free: bool, ts: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(sales).values(ts=ts or _now(), user_id=user_id, drink=drink, machine_id=machine_id,
                                              price=self.price(), free=1 if free else 0))

    def backfill(self, purchases: list[tuple[str, int, str]], free_every: int) -> int:
        """Первый запуск панели: переносим уже отмеченные чашки в журнал продаж (без аппарата)."""
        with self.engine.begin() as conn:
            if conn.execute(select(func.count()).select_from(sales)).scalar():
                return 0
            price, seen, rows = self.price(), Counter(), []
            for ts, user_id, drink in purchases:
                seen[user_id] += 1
                free = free_every >= 2 and seen[user_id] % free_every == 0
                rows.append({"ts": ts, "user_id": user_id, "drink": drink, "machine_id": None,
                             "price": price, "free": 1 if free else 0})
            if rows:
                conn.execute(insert(sales), rows)
            return len(rows)

    def summary(self) -> dict:
        now = datetime.now(ASTANA_TZ)
        day = now.strftime("%Y-%m-%d")
        week = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        month = (now - timedelta(days=29)).strftime("%Y-%m-%d")
        with self.engine.connect() as conn:
            rows = conn.execute(select(sales).where(sales.c.ts >= month)).all()
            total = conn.execute(select(func.count(), func.coalesce(func.sum(sales.c.free), 0)).select_from(sales)).first()
            new_requests = conn.execute(select(func.count()).select_from(requests)
                                        .where(requests.c.status == "new")).scalar()

        def block(since: str) -> dict:
            part = [r for r in rows if r.ts[:10] >= since]
            return {"cups": len(part), "free": sum(r.free for r in part),
                    "revenue": sum(r.price for r in part if not r.free)}

        by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        by_machine: dict[int | None, list[int]] = defaultdict(lambda: [0, 0])
        drinks = Counter()
        for r in rows:
            paid = 0 if r.free else r.price
            by_day[r.ts[:10]][0] += 1
            by_day[r.ts[:10]][1] += paid
            by_machine[r.machine_id][0] += 1
            by_machine[r.machine_id][1] += paid
            drinks[r.drink] += 1
        days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
        machine_rows = [dict(m, cups=by_machine[m["id"]][0], revenue=by_machine[m["id"]][1]) for m in self.machines()]
        return {
            "price": self.price(), "promo": self.get("promo"),
            "sales": {"day": block(day), "week": block(week), "month": block(month)},
            "days": [{"date": d, "cups": by_day[d][0], "revenue": by_day[d][1]} for d in days],
            "drinks": drinks.most_common(),
            "machines": sorted(machine_rows, key=lambda m: -m["revenue"]),
            "no_machine": {"cups": by_machine[None][0], "revenue": by_machine[None][1]},
            "all_time": {"cups": total[0], "free": int(total[1])},
            "new_requests": new_requests,
        }

    # ---------- заявки и обращения ----------
    def add_request(self, data: dict) -> dict:
        kind = data.get("kind")
        if kind not in KINDS:
            raise AdminError("Неизвестный тип обращения.")
        values = {"kind": kind, "name": _clean(data.get("name"), 80, required=True),
                  "contact": _clean(data.get("contact"), 120, required=True),
                  "text": str(data.get("text") or "").strip()[:2000],
                  "machine_id": int(data["machine_id"]) if str(data.get("machine_id") or "").isdigit() else None}
        if kind != "placement" and not values["text"]:
            raise AdminError("Опишите, что случилось.")
        with self.engine.begin() as conn:
            new_id = conn.execute(insert(requests).values(ts=_now(), status="new", note="", **values)).inserted_primary_key[0]
        return dict(values, id=new_id)

    def list_requests(self) -> list[dict]:
        with self.engine.connect() as conn:
            q = select(requests).order_by(requests.c.status.desc(), requests.c.id.desc()).limit(300)
            return [dict(r._mapping) for r in conn.execute(q)]

    def update_request(self, request_id: int, status: str, note: str) -> None:
        if status not in ("new", "done"):
            raise AdminError("Неизвестный статус.")
        with self.engine.begin() as conn:
            if not conn.execute(update(requests).where(requests.c.id == request_id)
                                .values(status=status, note=str(note or "").strip()[:1000])).rowcount:
                raise AdminError("Обращение не найдено.")


# ---------- вход в панель ----------
def _secret(cfg: Config) -> bytes:
    return hashlib.sha256(("admin-token:" + cfg.token).encode()).digest()


def make_admin_token(cfg: Config, tg_id: int) -> str:
    body = f"{tg_id}.{int(time.time()) + TOKEN_TTL}"
    return body + "." + hmac.new(_secret(cfg), body.encode(), hashlib.sha256).hexdigest()[:40]


def check_admin_token(cfg: Config, token: str) -> int | None:
    body, _, sig = token.rpartition(".")
    tg_id, _, expires = body.partition(".")
    good = hmac.new(_secret(cfg), body.encode(), hashlib.sha256).hexdigest()[:40]
    if not (tg_id.isdigit() and expires.isdigit() and hmac.compare_digest(good, sig)):
        return None
    if int(expires) < time.time() or int(tg_id) not in cfg.admin_ids:
        return None
    return int(tg_id)


# ---------- HTTP ----------
def setup_admin_api(app: web.Application, db, store: AdminStore, cfg: Config, bot=None) -> None:
    attempts: dict[str, deque] = defaultdict(deque)

    def fail(status: int, message: str) -> web.Response:
        return web.json_response({"error": message}, status=status)

    def admin(handler):
        async def wrapped(request: web.Request) -> web.Response:
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not check_admin_token(cfg, token):
                return fail(401, "Ссылка устарела. Отправьте боту команду /admin и откройте новую.")
            try:
                return await handler(request)
            except AdminError as e:
                return fail(400, str(e))
            except (ValueError, KeyError, TypeError):
                return fail(400, "Неверные данные.")
        return wrapped

    async def body(request: web.Request) -> dict:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
        return data

    # --- открытая часть: что сайту можно знать без входа ---
    async def public(_: web.Request) -> web.Response:
        def read():
            return {"price": store.price(), "promo": store.get("promo"), "free_every": cfg.free_every,
                    "machines": [{k: m[k] for k in ("id", "name", "address", "kind")}
                                 for m in store.machines(only_active=True)]}
        return web.json_response(await asyncio.to_thread(read))

    async def new_request(request: web.Request) -> web.Response:
        ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
        now, q = time.monotonic(), attempts[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        q.append(now)
        if len(q) > 5:
            return fail(429, "Слишком много обращений. Попробуйте позже.")
        try:
            saved = await asyncio.to_thread(store.add_request, await body(request))
        except AdminError as e:
            return fail(400, str(e))
        except Exception:
            return fail(400, "Неверные данные.")
        if bot:  # сразу сообщаем админам в Telegram
            import html
            text = (f"🔔 <b>{KINDS[saved['kind']]}</b> №{saved['id']}\n{html.escape(saved['name'])}, "
                    f"{html.escape(saved['contact'])}\n\n{html.escape(saved['text'][:700])}").strip()
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(admin_id, text)
                except Exception:
                    pass
        return web.json_response({"ok": True, "id": saved["id"]})

    # --- панель ---
    @admin
    async def summary(_: web.Request) -> web.Response:
        def read():
            data = store.summary()
            users = db.all_users()
            n = cfg.free_every
            data["users"] = {"total": len(users), "telegram": sum(1 for u in users if u.tg_id),
                             "with_cups": sum(1 for u in users if u.total)}
            data["loyalty"] = {"free_every": n,
                               "next_free": sorted(u.login for u in users if n >= 2 and u.total % n == n - 1)[:50],
                               "top": [{"login": u.login, "total": u.total, "favorite": u.favorite}
                                       for u in sorted(users, key=lambda u: -u.total)[:10] if u.total]}
            return data
        return web.json_response(await asyncio.to_thread(read))

    @admin
    async def requests_list(_: web.Request) -> web.Response:
        return web.json_response({"requests": await asyncio.to_thread(store.list_requests)})

    @admin
    async def request_update(request: web.Request) -> web.Response:
        data = await body(request)
        await asyncio.to_thread(store.update_request, int(request.match_info["id"]), data["status"], data.get("note", ""))
        return web.json_response({"ok": True})

    @admin
    async def machine_save(request: web.Request) -> web.Response:
        return web.json_response({"id": await asyncio.to_thread(store.save_machine, await body(request))})

    @admin
    async def settings_save(request: web.Request) -> web.Response:
        data = await body(request)
        if "price" in data:
            price = int(data["price"])
            if not 1 <= price <= 100000:
                raise AdminError("Цена должна быть от 1 до 100 000 ₸.")
            await asyncio.to_thread(store.put, "price", str(price))
        if "promo" in data:
            await asyncio.to_thread(store.put, "promo", str(data["promo"] or "").strip()[:300])
        return web.json_response({"ok": True})

    @admin
    async def promo_send(request: web.Request) -> web.Response:
        text = str((await body(request)).get("text") or "").strip()[:1000]
        if not text:
            raise AdminError("Напишите текст акции.")
        if not bot:
            raise AdminError("Рассылка доступна только на работающем боте.")
        import html
        sent = 0
        for user in await asyncio.to_thread(db.all_users):
            if user.tg_id:
                try:
                    await bot.send_message(user.tg_id, "📣 " + html.escape(text))
                    sent += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        await asyncio.to_thread(db.log_promo, text, sent)
        return web.json_response({"sent": sent})

    app.router.add_get("/api/public", public)
    app.router.add_post("/api/request", new_request)
    app.router.add_get("/api/admin/summary", summary)
    app.router.add_get("/api/admin/requests", requests_list)
    app.router.add_post("/api/admin/requests/{id:\\d+}", request_update)
    app.router.add_post("/api/admin/machines", machine_save)
    app.router.add_post("/api/admin/settings", settings_save)
    app.router.add_post("/api/admin/promo", promo_send)
