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
from bot.db import DBError, ExcelDB
from bot.main import (B_CANCEL, B_FILE, B_LOGIN, B_LOGOUT, B_MARK, B_PROMO, B_REG, B_STATS, B_USERS,
                      build_router)

ADMIN, GUEST = 1, 2


# ---------- база ----------
def test_db_flow(tmp_path):
    path = str(tmp_path / "d" / "coffee.xlsx")
    db = ExcelDB(path)
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

    db = ExcelDB(path)  # перечитываем с диска
    assert db.by_card("ace000001").login == "aida"
    assert db.login("aida", "pass1", 99).tg_id == 99 and db.by_tg(10) is None
    assert db.user_stats(1) == [("Латте", 2), ("Раф", 1)]
    wb = load_workbook(path)
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
    def __init__(self, tmp_path):
        self.cfg = Config(token="1:x", admin_ids={ADMIN}, db_path=str(tmp_path / "coffee.xlsx"))
        self.db = ExcelDB(self.cfg.db_path)
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


def test_full_scenario(tmp_path):
    async def run():
        h = Harness(tmp_path)
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
        await h.click(ADMIN, f"mark:1:{h.cfg.drinks.index('Латте')}")
        assert "не найдена" in texts(await h.say(ADMIN, "777"))
        # кнопка меню во время сканирования работает как кнопка
        assert "Гостей: <b>1</b>" in texts(await h.say(ADMIN, B_USERS))
        await h.say(ADMIN, B_CANCEL)
        await h.click(ADMIN, f"mark:1:{h.cfg.drinks.index('Мокко')}")

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
