import asyncio
import datetime as dt
import io

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, SendDocument, SendPhoto
from aiogram.types import Chat, Message, PhotoSize, Update, User
from openpyxl import load_workbook

from bot.barcodes import make_barcode_png, normalize_code, read_barcode
from bot.config import Config
from bot.demo import PREFIX, build_demo
from bot.db import DBError, ExcelDB, open_db
from bot.sqldb import SqlDB, normalize_url
from bot.main import B_KPI
from bot.main import (B_CANCEL, B_FILE, B_LOGIN, B_LOGOUT, B_MARK, B_PROMO, B_REG, B_STATS, B_USERS,
                      build_router)

ADMIN, GUEST = 1, 2


# ---------- база ----------
@pytest.fixture(params=["excel", "sql"])
def backend(request, tmp_path):
    """Фабрика: каждый вызов заново открывает ту же базу (как перезапуск бота)."""
    path = str(tmp_path / "d" / "coffee.xlsx")
    if request.param == "excel":
        return lambda: ExcelDB(path)
    url = f"sqlite:///{tmp_path / 'coffee.db'}"
    return lambda: SqlDB(url)


def test_db_flow(backend):
    db = backend()
    u = db.register("aida", "pass1", 10)
    assert u.card == "ACE000001" and u.total == 0
    with pytest.raises(DBError):
        db.register("AIDA", "pass2", 11)
    with pytest.raises(DBError):
        db.register("x", "pass2", 11)
    with pytest.raises(DBError):
        db.login("aida", "wrong", 11)
    for d in ["Латте", "Раф", "Латте"]:
        u = db.add_coffee(u.id, d)
    assert (u.total, u.favorite) == (3, "Латте")
    db.add_coffee(db.register("bek", "pass", 20).id, "Раф")
    db.log_promo("=1+1", 2)

    db = backend()  # перечитываем с диска
    assert db.by_card("ace000001").login == "aida"
    assert db.login("aida", "pass1", 99).tg_id == 99 and db.by_tg(10) is None
    assert db.user_stats(1) == [("Латте", 2), ("Раф", 1)]
    assert [(u.login, u.total, u.favorite) for u in db.all_users()] == [("aida", 3, "Латте"), ("bek", 1, "Раф")]
    assert db.drink_stats() == [("Латте", 2), ("Раф", 2)]
    assert db.by_card("ACE000099") is None
    # демо-гости добавляются, не трогая настоящих, и убираются без следа
    demo = build_demo(["Латте", "Раф", "Мокко"])
    assert db.seed_demo(demo) == 12 and db.seed_demo(demo) == 0
    assert len(db.all_users()) == 14 and len(db.purchases()) == 4 + sum(len(r) for _, r in demo)
    top = max(db.all_users(), key=lambda u: u.total)
    assert top.login == PREFIX + "aigerim" and top.total == 14 and top.favorite in ("Латте", "Раф", "Мокко")
    assert load_workbook(io.BytesIO(db.export_xlsx()))["Пользователи"].max_row == 15
    assert db.clear_demo(PREFIX) == 12 and db.clear_demo(PREFIX) == 0
    assert [u.login for u in db.all_users()] == ["aida", "bek"]
    assert db.drink_stats() == [("Латте", 2), ("Раф", 2)]
    assert db.register("cara", "pass", 30).login == "cara"
    assert [(u, d) for _, u, d in db.purchases()] == [(1, "Латте"), (1, "Раф"), (1, "Латте"), (2, "Раф")]
    wb = load_workbook(io.BytesIO(db.export_xlsx()))
    assert wb["Пользователи"]["H2"].value == "Латте" and wb["Покупки"].max_row == 5
    assert [r for r in wb["Напитки"].iter_rows(min_row=2, values_only=True)] == [("Латте", 2), ("Раф", 2)]
    assert "pass1" not in str(wb["Пользователи"]["C2"].value)
    assert wb["Акции"]["B2"].value == "'=1+1"


def test_barcode_roundtrip():
    assert read_barcode(make_barcode_png("ACE000042")) == "ACE000042"
    assert normalize_code("42") == normalize_code(" ace000042 ") == "ACE000042"
    assert normalize_code("привет") is None


# ---------- бот с поддельным Telegram ----------
class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.photo = b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetFile):
            from aiogram.types import File
            return File(file_id="f", file_unique_id="f", file_path="p")
        if method.__api_method__ in ("deleteMessage", "answerCallbackQuery"):
            return True
        return Message(message_id=1, date=dt.datetime.now(), chat=Chat(id=1, type="private"))

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield self.photo

    async def close(self):
        pass


class Harness:
    def __init__(self, tmp_path, sql):
        self.cfg = Config(token="1:x", admin_ids={ADMIN}, db_path=str(tmp_path / "coffee.xlsx"),
                          database_url=f"sqlite:///{tmp_path / 'coffee.db'}" if sql else "", free_every=3)
        self.db = open_db(self.cfg)
        self.session = FakeSession()
        self.bot = Bot("123456:TEST", session=self.session)
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.db, self.cfg))
        self.n = 0

    def _upd(self, **kw):
        self.n += 1
        return Update(update_id=self.n, **kw)

    async def say(self, uid, text=None, photo=None):
        self.session.calls.clear()
        msg = Message(message_id=self.n + 1, date=dt.datetime.now(), chat=Chat(id=uid, type="private"),
                      from_user=User(id=uid, is_bot=False, first_name="u"), text=text,
                      photo=[PhotoSize(file_id="f", file_unique_id="f", width=1, height=1)] if photo else None)
        await self.dp.feed_update(self.bot, self._upd(message=msg))
        return self.session.calls

    async def click(self, uid, data):
        from aiogram.types import CallbackQuery
        self.session.calls.clear()
        msg = Message(message_id=5, date=dt.datetime.now(), chat=Chat(id=uid, type="private"), text="x")
        cq = CallbackQuery(id="1", from_user=User(id=uid, is_bot=False, first_name="u"),
                           chat_instance="c", message=msg, data=data)
        await self.dp.feed_update(self.bot, self._upd(callback_query=cq))
        return self.session.calls


def texts(calls):
    return " | ".join(str(getattr(c, "text", None) or getattr(c, "caption", "")) for c in calls)


def test_site_api(tmp_path):
    """Сайт: гость регистрируется, получает карту со штрихкодом, входит заново и видит свежие данные."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from bot.webapi import setup_api

    async def run():
        cfg = Config(token="1:x", free_every=3, site_origin="https://site.example")
        db = SqlDB(f"sqlite:///{tmp_path / 'api.db'}")
        app = web.Application()
        setup_api(app, db, cfg)
        async with TestClient(TestServer(app)) as c:
            r = await c.options("/api/register", headers={"Origin": "https://site.example"})
            assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "https://site.example"
            assert (await c.post("/api/register", data="oops")).status == 400
            assert (await c.post("/api/register", json={"login": "x", "password": "pass"})).status == 400
            r = await c.post("/api/register", json={"login": "Aida", "password": "pass1"})
            data = await r.json()
            assert r.status == 200 and data["card"]["card"] == "ACE000001" and data["card"]["in_telegram"] is False
            assert data["card"]["barcode"].startswith("data:image/png;base64,") and data["card"]["done"] == 0
            token = data["token"]
            assert (await c.post("/api/register", json={"login": "aida", "password": "zzzz"})).status == 400
            assert (await c.post("/api/login", json={"login": "aida", "password": "wrong"})).status == 400
            assert (await c.get("/api/me")).status == 401
            assert (await c.get("/api/me", headers={"Authorization": "Bearer 1.deadbeef"})).status == 401
            db.add_coffee(1, "Латте"); db.add_coffee(1, "Латте")
            r = await c.get("/api/me", headers={"Authorization": f"Bearer {token}"})
            card = (await r.json())["card"]
            assert card["total"] == 2 and card["favorite"] == "Латте" and card["next_free"] is True
            r = await c.post("/api/login", json={"login": "AIDA", "password": "pass1"})
            assert r.status == 200 and (await r.json())["token"] == token
            # аккаунт с сайта подходит и для входа в Telegram-боте
            assert db.login("aida", "pass1", 77).tg_id == 77
            for _ in range(25):
                r = await c.post("/api/login", json={"login": "aida", "password": "wrong"})
            assert r.status == 429

    asyncio.run(run())


def test_normalize_url():
    assert normalize_url("postgres://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert normalize_url("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"


@pytest.mark.parametrize("sql", [False, True])
def test_full_scenario(tmp_path, sql):
    async def run():
        h = Harness(tmp_path, sql)
        assert "Astana Coffee Express" in texts(await h.say(GUEST, "/start"))

        # регистрация: пароль удаляется, приходит штрихкод
        await h.say(GUEST, B_REG)
        await h.say(GUEST, "aida")
        calls = await h.say(GUEST, "secret")
        assert calls[0].__api_method__ == "deleteMessage"
        assert any(isinstance(c, SendPhoto) for c in calls)
        assert h.db.by_tg(GUEST).card == "ACE000001"

        # гость не видит админку
        assert "меню" in texts(await h.say(GUEST, B_USERS))
        assert "меню" in texts(await h.say(GUEST, B_MARK))
        await h.click(GUEST, "mark:1:0")
        assert h.db.by_tg(GUEST).total == 0

        # админ отмечает кофе по фото штрихкода и по коду
        await h.say(ADMIN, B_MARK)
        h.session.photo = make_barcode_png("ACE000001")
        assert "aida" in texts(await h.say(ADMIN, photo=True))
        calls = await h.click(ADMIN, f"mark:1:{h.cfg.drinks.index('Латте')}")
        assert any(getattr(c, "chat_id", None) == GUEST for c in calls)  # гостю пришло уведомление
        assert "aida" in texts(await h.say(ADMIN, "1"))
        assert "До бесплатной чашки: ещё <b>1</b>" in texts(await h.say(GUEST, B_STATS))
        calls = await h.click(ADMIN, f"mark:1:{h.cfg.drinks.index('Латте')}")
        assert "Следующая чашка — <b>бесплатно</b>" in texts(calls)
        assert "Эта чашка — бесплатная" in texts(await h.say(ADMIN, "1"))
        assert "не найдена" in texts(await h.say(ADMIN, "777"))
        # кнопка меню во время сканирования работает как кнопка
        assert "Гостей: <b>1</b>" in texts(await h.say(ADMIN, B_USERS))
        await h.say(ADMIN, B_CANCEL)
        calls = await h.click(ADMIN, f"mark:1:{h.cfg.drinks.index('Мокко')}")
        assert "(бесплатная)" in texts(calls) and "была бесплатной" in texts(calls)
        t = texts(await h.say(ADMIN, B_KPI))
        assert "Чашек сегодня: <b>3</b>" in t and "выдано: <b>1</b>" in t
        assert "с 2+ покупками: <b>1</b>" in t and "<b>100%</b>" in t and "Латте (2)" in t
        assert "меню" in texts(await h.say(GUEST, B_KPI))
        assert "меню" in texts(await h.say(GUEST, "/demo"))
        assert "демо-гостей: <b>12</b>" in texts(await h.say(ADMIN, "/demo"))
        assert "Гостей: <b>13</b>" in texts(await h.say(ADMIN, B_USERS))
        assert "Гостей: <b>13</b>" in texts(await h.say(ADMIN, B_KPI))
        assert "удалено: <b>12</b>" in texts(await h.say(ADMIN, "/demo_clear"))
        assert "Гостей: <b>1</b>" in texts(await h.say(ADMIN, B_USERS))
        assert "Каждая 3-я чашка" in texts(await h.say(GUEST, "/help"))

        t = texts(await h.say(GUEST, B_STATS))
        assert "Всего чашек: <b>3</b>" in t and "Любимый напиток: <b>Латте</b>" in t
        assert any(isinstance(c, SendDocument) for c in await h.say(ADMIN, B_FILE))

        # рассылка
        await h.say(ADMIN, B_PROMO)
        assert "Отправить (1)" in str((await h.say(ADMIN, "Скидка <20%>"))[0].reply_markup)
        calls = await h.click(ADMIN, "promo:send")
        assert [c.text for c in calls if getattr(c, "chat_id", None) == GUEST] == ["📣 Скидка &lt;20%&gt;"]
        assert "доставлено 1" in texts(calls)

        # выход и вход
        await h.say(GUEST, B_LOGOUT)
        assert h.db.by_tg(GUEST) is None
        await h.say(GUEST, B_LOGIN)
        await h.say(GUEST, "aida")
        assert "Неверный" in texts(await h.say(GUEST, "nope"))
        await h.say(GUEST, "aida")
        await h.say(GUEST, "secret")
        assert h.db.by_tg(GUEST).total == 3

    asyncio.run(run())
