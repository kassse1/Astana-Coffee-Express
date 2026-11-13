"""Telegram-бот сети кофе-аппаратов Astana Coffee Express: карта гостя, отметка кофе, админка, рассылки."""
import asyncio
import hashlib
import html
import io
import logging
from collections import Counter
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)

from .barcodes import make_barcode_png, normalize_code, read_barcode
from .config import Config, load_config
from .db import ASTANA_TZ, DBError, User, open_db
from .demo import PREFIX as DEMO_PREFIX, build_demo
from .webapi import setup_api

log = logging.getLogger("coffee-bot")

B_REG, B_LOGIN = "📝 Регистрация", "🔑 Войти"
B_CARD, B_STATS, B_LOGOUT = "🎫 Мой штрихкод", "☕ Моя статистика", "🚪 Выйти"
B_MARK, B_USERS, B_PROMO, B_FILE = "✅ Отметить кофе", "👥 Гости", "📣 Акция", "📊 Excel-база"
B_KPI = "📈 Статистика"
B_CANCEL = "❌ Отмена"
ALL_BUTTONS = {B_REG, B_LOGIN, B_CARD, B_STATS, B_LOGOUT, B_MARK, B_USERS, B_PROMO, B_FILE, B_KPI,
               B_CANCEL}


class Auth(StatesGroup):
    reg_login = State()
    reg_password = State()
    login = State()
    password = State()


class Admin(StatesGroup):
    scan = State()
    promo_text = State()
    promo_confirm = State()


def build_router(db, cfg: Config) -> Router:
    r = Router()

    typed = F.text & ~F.text.in_(ALL_BUTTONS)  # обычный текст, а не кнопка меню

    def is_admin(tg_id: int) -> bool:
        return tg_id in cfg.admin_ids

    def menu(tg_id: int) -> ReplyKeyboardMarkup:
        rows = ([[B_CARD, B_STATS], [B_LOGOUT]] if db.by_tg(tg_id) else [[B_REG, B_LOGIN]])
        if is_admin(tg_id):
            rows += [[B_MARK, B_USERS], [B_KPI, B_PROMO], [B_FILE]]
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True)

    cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_CANCEL)]], resize_keyboard=True)

    def drinks_kb(user_id: int) -> InlineKeyboardMarkup:
        btns = [InlineKeyboardButton(text=d, callback_data=f"mark:{user_id}:{i}")
                for i, d in enumerate(cfg.drinks)]
        return InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)])

    def loyalty(total: int) -> str:
        """Строка про прогресс до бесплатной чашки (пустая, если программа выключена)."""
        n = cfg.free_every
        if n < 2:
            return ""
        done = total % n  # сколько чашек уже в текущем круге
        if done == n - 1:
            return "🎁 Следующая чашка — <b>бесплатно</b>!"
        bar = "☕" * done + "▫️" * (n - 1 - done) + "🎁"
        return f"{bar}\nДо бесплатной чашки: ещё <b>{n - 1 - done}</b>"

    def next_is_free(total: int) -> bool:
        return cfg.free_every >= 2 and total % cfg.free_every == cfg.free_every - 1

    async def send_card(message: Message, user: User) -> None:
        await message.answer_photo(
            BufferedInputFile(make_barcode_png(user.card), filename=f"{user.card}.png"),
            caption=(f"Ваша карта <b>{user.card}</b>\nСканируйте штрихкод у кофе-аппарата — "
                     f"чашка попадёт в вашу статистику.\n\n{loyalty(user.total)}").strip(),
            reply_markup=menu(message.from_user.id))

    # ---------- общее ----------
    @r.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        user = db.by_tg(message.from_user.id)
        text = (f"С возвращением, <b>{html.escape(user.login)}</b>! ☕" if user else
                "Привет! Это <b>Astana Coffee Express</b> — умные кофе-аппараты в Астане ☕\n"
                "Кофе за 45 секунд, 24/7, оплата одним сканом Kaspi QR.\n\n"
                "Зарегистрируйтесь, получите личный штрихкод и сканируйте его при каждой покупке — "
                "мы запомним, что вы любите, и будем присылать акции.")
        if is_admin(message.from_user.id):
            text += "\n\n🛠 Вам доступно меню администратора."
        await message.answer(text, reply_markup=menu(message.from_user.id))

    @r.message(Command("cancel"))
    @r.message(F.text == B_CANCEL)
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Отменено.", reply_markup=menu(message.from_user.id))

    @r.message(Command("help"))
    async def help_cmd(message: Message):
        text = ("<b>Как это работает</b>\n"
                "1. Зарегистрируйтесь — придумайте логин и пароль.\n"
                "2. Нажмите «Мой штрихкод» и сканируйте его у кофе-аппарата при покупке.\n"
                "3. В «Моя статистика» — сколько чашек выпито и ваш любимый напиток.\n")
        if cfg.free_every >= 2:
            text += f"\n🎁 Каждая {cfg.free_every}-я чашка — бесплатно.\n"
        text += "\nКоманды: /start — меню, /cancel — отменить действие, /id — ваш Telegram ID."
        if is_admin(message.from_user.id):
            text += "\n\n🛠 Админу: /demo — добавить демо-гостей для показа, /demo_clear — убрать их."
        await message.answer(text, reply_markup=menu(message.from_user.id))

    # ---------- админ: демо-гости ----------
    @r.message(Command("demo"), F.from_user.id.in_(cfg.admin_ids))
    async def demo(message: Message):
        removed = await asyncio.to_thread(db.clear_demo, DEMO_PREFIX)
        created = await asyncio.to_thread(db.seed_demo, build_demo(cfg.drinks))
        await message.answer(
            f"🧪 Добавлено демо-гостей: <b>{created}</b>" + (f" (старых удалено: {removed})" if removed else "")
            + ".\nПосмотрите «Гости», «Статистика» и «Excel-база».\n"
              f"Их логины начинаются с <code>{DEMO_PREFIX}</code>. Убрать: /demo_clear",
            reply_markup=menu(message.from_user.id))

    @r.message(Command("demo_clear"), F.from_user.id.in_(cfg.admin_ids))
    async def demo_clear(message: Message):
        removed = await asyncio.to_thread(db.clear_demo, DEMO_PREFIX)
        await message.answer(f"Демо-гостей удалено: <b>{removed}</b>. Настоящие гости не затронуты.",
                             reply_markup=menu(message.from_user.id))

    @r.message(Command("id"))
    async def my_id(message: Message):
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")

    # ---------- регистрация и вход ----------
    @r.message(F.text == B_REG)
    async def reg(message: Message, state: FSMContext):
        await state.set_state(Auth.reg_login)
        await message.answer("Придумайте логин (3–20 символов: буквы, цифры, _):", reply_markup=cancel_kb)

    @r.message(F.text == B_LOGIN)
    async def login(message: Message, state: FSMContext):
        await state.set_state(Auth.login)
        await message.answer("Введите логин:", reply_markup=cancel_kb)

    @r.message(Auth.reg_login, typed)
    @r.message(Auth.login, typed)
    async def got_login(message: Message, state: FSMContext):
        registering = await state.get_state() == Auth.reg_login.state
        await state.update_data(login=message.text.strip())
        await state.set_state(Auth.reg_password if registering else Auth.password)
        await message.answer("Придумайте пароль (минимум 4 символа):" if registering else "Введите пароль:")

    @r.message(Auth.reg_password, typed)
    @r.message(Auth.password, typed)
    async def got_password(message: Message, state: FSMContext):
        registering = await state.get_state() == Auth.reg_password.state
        data = await state.get_data()
        try:  # пароль не должен оставаться в переписке
            await message.delete()
        except TelegramAPIError:
            pass
        try:
            action = db.register if registering else db.login
            user = action(data["login"], message.text, message.from_user.id)
        except DBError as e:
            await state.set_state(Auth.reg_login if registering else Auth.login)
            await message.answer(f"⚠️ {e}\nВведите логин ещё раз:", reply_markup=cancel_kb)
            return
        await state.clear()
        await message.answer("Готово, вы зарегистрированы! 🎉" if registering else "Вы вошли ✅")
        await send_card(message, user)

    @r.message(F.text == B_LOGOUT)
    async def logout(message: Message, state: FSMContext):
        await state.clear()
        db.logout(message.from_user.id)
        await message.answer("Вы вышли из аккаунта.", reply_markup=menu(message.from_user.id))

    # ---------- гость ----------
    @r.message(F.text == B_CARD)
    async def card(message: Message):
        user = db.by_tg(message.from_user.id)
        if not user:
            return await message.answer("Сначала войдите.", reply_markup=menu(message.from_user.id))
        await send_card(message, user)

    @r.message(F.text == B_STATS)
    async def stats(message: Message):
        user = db.by_tg(message.from_user.id)
        if not user:
            return await message.answer("Сначала войдите.", reply_markup=menu(message.from_user.id))
        rows = db.user_stats(user.id)
        if not rows:
            return await message.answer(("Пока ни одной чашки. Сканируйте штрихкод у кофе-аппарата "
                                         f"при покупке ☕\n\n{loyalty(0)}").strip())
        lines = "\n".join(f"• {html.escape(d)} — {n}" for d, n in rows)
        await message.answer(f"Всего чашек: <b>{user.total}</b>\n"
                             f"Любимый напиток: <b>{html.escape(user.favorite)}</b> ❤️\n\n{lines}"
                             f"\n\n{loyalty(user.total)}".rstrip())

    # ---------- админ: отметка кофе ----------
    @r.message(F.text == B_MARK, F.from_user.id.in_(cfg.admin_ids))
    async def mark(message: Message, state: FSMContext):
        await state.set_state(Admin.scan)
        await message.answer("Пришлите фото штрихкода гостя или введите код карты "
                             "(например <code>ACE000012</code> или просто <code>12</code>).\n"
                             "Гостей можно сканировать подряд, в конце нажмите «Отмена».",
                             reply_markup=cancel_kb)

    @r.message(Admin.scan, F.photo | typed)
    async def scanned(message: Message, state: FSMContext, bot: Bot):
        if message.photo:
            buf = io.BytesIO()
            await bot.download(message.photo[-1], destination=buf)
            code = await asyncio.to_thread(read_barcode, buf.getvalue())
            if not code:
                return await message.answer("Не вижу штрихкод на фото. Снимите ближе и ровнее "
                                            "или введите код вручную.")
        else:
            code = normalize_code(message.text)
            if not code:
                return await message.answer("Не похоже на код карты. Пример: ACE000012")
        user = db.by_card(code)
        if not user:
            return await message.answer(f"Карта {code} не найдена.")
        await message.answer(
            f"Гость: <b>{html.escape(user.login)}</b> ({user.card})\n"
            f"Чашек: {user.total}, любит: {html.escape(user.favorite or '—')}\n"
            + ("🎁 <b>Эта чашка — бесплатная</b>\n" if next_is_free(user.total) else "")
            + "\nЧто берёт?",
            reply_markup=drinks_kb(user.id))

    @r.callback_query(F.data.startswith("mark:"), F.from_user.id.in_(cfg.admin_ids))
    async def marked(call: CallbackQuery, bot: Bot):
        _, user_id, idx = call.data.split(":")
        try:
            drink = cfg.drinks[int(idx)]
            user = db.add_coffee(int(user_id), drink)
        except (DBError, IndexError) as e:
            return await call.answer(str(e) or "Ошибка", show_alert=True)
        was_free = cfg.free_every >= 2 and user.total % cfg.free_every == 0
        await call.message.edit_text(
            f"✅ {html.escape(user.login)}: {html.escape(drink)} отмечен"
            f"{' 🎁 (бесплатная)' if was_free else ''}. Всего чашек: {user.total}")
        await call.answer("Отмечено")
        if user.tg_id:
            note = "🎁 Эта чашка была бесплатной — спасибо, что вы с нами!\n\n" if was_free else ""
            try:
                await bot.send_message(
                    user.tg_id, f"☕ Отмечен <b>{html.escape(drink)}</b>. Всего чашек: <b>{user.total}</b>\n\n"
                                f"{note}{loyalty(user.total)}".rstrip())
            except TelegramAPIError:
                pass

    # ---------- админ: гости и база ----------
    @r.message(F.text == B_USERS, F.from_user.id.in_(cfg.admin_ids))
    async def users(message: Message):
        all_users = sorted(db.all_users(), key=lambda u: -u.total)
        if not all_users:
            return await message.answer("Пока никто не зарегистрировался.")
        top = db.drink_stats()
        head = f"👥 Гостей: <b>{len(all_users)}</b>, чашек всего: <b>{sum(n for _, n in top)}</b>\n"
        if top:
            head += "🏆 Топ напитков: " + ", ".join(f"{html.escape(d)} ({n})" for d, n in top[:3]) + "\n"
        lines = [f"{i}. <b>{html.escape(u.login)}</b> · {u.card} · {u.total} ☕ · "
                 f"любит: {html.escape(u.favorite or '—')}" for i, u in enumerate(all_users, 1)]
        chunk = head
        for line in lines:  # лимит Telegram — 4096 символов на сообщение
            if len(chunk) + len(line) > 3800:
                await message.answer(chunk)
                chunk = ""
            chunk += "\n" + line
        await message.answer(chunk)

    @r.message(F.text == B_KPI, F.from_user.id.in_(cfg.admin_ids))
    async def kpi(message: Message):
        log_rows = db.purchases()
        all_users = db.all_users()
        now = datetime.now(ASTANA_TZ)
        today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        week_ago = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        n_today = sum(ts.startswith(today) for ts, _, _ in log_rows)
        n_week = sum(ts[:10] >= week_ago for ts, _, _ in log_rows)
        in_month = [(uid, drink) for ts, uid, drink in log_rows if ts.startswith(month)]
        per_user = Counter(uid for uid, _ in in_month)
        active, repeat = len(per_user), sum(1 for n in per_user.values() if n >= 2)
        text = (f"📈 <b>Статистика</b> на {now:%d.%m.%Y %H:%M}\n\n"
                f"👥 Гостей: <b>{len(all_users)}</b> (в боте сейчас: {sum(1 for u in all_users if u.tg_id)})\n\n"
                f"☕ Чашек сегодня: <b>{n_today}</b>\n"
                f"☕ За 7 дней: <b>{n_week}</b> (в среднем {n_week / 7:.1f} в день)\n"
                f"☕ За этот месяц: <b>{len(in_month)}</b>\n"
                f"☕ За всё время: <b>{len(log_rows)}</b>\n")
        if cfg.free_every >= 2:
            free = sum(u.total // cfg.free_every for u in all_users)
            text += f"🎁 Бесплатных чашек выдано: <b>{free}</b> (каждая {cfg.free_every}-я)\n"
        text += (f"\n🔁 Активных гостей за месяц: <b>{active}</b>\n"
                 f"🔁 Из них с 2+ покупками: <b>{repeat}</b>\n"
                 f"🔁 Доля повторных покупателей: <b>{repeat / active * 100 if active else 0:.0f}%</b>\n")
        top = Counter(drink for _, drink in in_month).most_common(3)
        if top:
            text += "\n🏆 Топ месяца: " + ", ".join(f"{html.escape(d)} ({n})" for d, n in top)
        await message.answer(text)

    @r.message(F.text == B_FILE, F.from_user.id.in_(cfg.admin_ids))
    async def send_file(message: Message):
        data = await asyncio.to_thread(db.export_xlsx)
        await message.answer_document(BufferedInputFile(data, filename="coffee.xlsx"),
                                      caption="Актуальная база")

    # ---------- админ: рассылка ----------
    @r.message(F.text == B_PROMO, F.from_user.id.in_(cfg.admin_ids))
    async def promo(message: Message, state: FSMContext):
        await state.set_state(Admin.promo_text)
        await message.answer("Напишите текст акции — он придёт всем гостям, вошедшим в бота.",
                             reply_markup=cancel_kb)

    @r.message(Admin.promo_text, typed)
    async def promo_text(message: Message, state: FSMContext):
        await state.update_data(text=message.text)
        await state.set_state(Admin.promo_confirm)
        count = sum(1 for u in db.all_users() if u.tg_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"Отправить ({count})", callback_data="promo:send"),
            InlineKeyboardButton(text="Отмена", callback_data="promo:cancel")]])
        await message.answer(f"Так увидят гости:\n\n📣 {html.escape(message.text)}", reply_markup=kb)

    @r.callback_query(Admin.promo_confirm, F.data.startswith("promo:"))
    async def promo_send(call: CallbackQuery, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await state.clear()
        await call.message.edit_reply_markup()
        await call.answer()
        if call.data == "promo:cancel":
            return await call.message.answer("Рассылка отменена.", reply_markup=menu(call.from_user.id))
        text = f"📣 {html.escape(data['text'])}"
        sent = failed = 0
        for user in db.all_users():
            if not user.tg_id:
                continue
            try:
                try:
                    await bot.send_message(user.tg_id, text)
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    await bot.send_message(user.tg_id, text)
                sent += 1
            except TelegramAPIError:
                failed += 1  # гость заблокировал бота и т.п.
            await asyncio.sleep(0.05)
        db.log_promo(data["text"], sent)
        await call.message.answer(f"Рассылка завершена: доставлено {sent}, не доставлено {failed}.",
                                  reply_markup=menu(call.from_user.id))

    # ---------- всё остальное ----------
    @r.message()
    async def fallback(message: Message):
        await message.answer("Выберите действие в меню 👇", reply_markup=menu(message.from_user.id))

    return r


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not cfg.admin_ids:
        log.warning("ADMIN_IDS пуст — админ-меню никому не доступно. Узнать ID: команда /id в боте.")
    db = open_db(cfg)
    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(build_router(db, cfg))
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню"),
                               BotCommand(command="help", description="Как это работает"),
                               BotCommand(command="cancel", description="Отменить действие")])
    log.info("Бот запущен, база: %s", "облачная (DATABASE_URL)" if cfg.database_url else cfg.db_path)
    if cfg.webhook_url:
        await run_webhook(bot, dp, cfg)
    else:
        await bot.delete_webhook()
        await dp.start_polling(bot)


async def run_webhook(bot: Bot, dp: Dispatcher, cfg: Config) -> None:
    """Режим для бесплатного Web Service: Telegram сам стучится к нам и будит службу."""
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    secret = hashlib.sha256(cfg.token.encode()).hexdigest()[:32]
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="Astana Coffee Express bot is running"))
    SimpleRequestHandler(dp, bot, secret_token=secret).register(app, path="/webhook")
    setup_api(app, db, cfg)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    await bot.set_webhook(f"{cfg.webhook_url}/webhook", secret_token=secret)
    log.info("Вебхук: %s/webhook, порт %s", cfg.webhook_url, cfg.port)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
