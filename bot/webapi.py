"""HTTP API для сайта: гость сам регистрируется и видит свою карту со штрихкодом.

Работает в том же aiohttp-приложении, что и вебхук бота. Логин и пароль общие с Telegram-ботом.
"""
import asyncio
import base64
import hashlib
import hmac
import time
from collections import defaultdict, deque

from aiohttp import web

from .barcodes import make_barcode_png
from .config import Config
from .db import DBError, User

LIMIT, WINDOW = 20, 300  # не больше 20 попыток регистрации/входа с одного адреса за 5 минут


def setup_api(app: web.Application, db, cfg: Config) -> None:
    secret = hashlib.sha256(("site-token:" + cfg.token).encode()).digest()
    attempts: dict[str, deque] = defaultdict(deque)

    def make_token(user_id: int) -> str:
        sig = hmac.new(secret, str(user_id).encode(), hashlib.sha256).hexdigest()[:32]
        return f"{user_id}.{sig}"

    def read_token(request: web.Request) -> int | None:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        user_id, _, sig = token.partition(".")
        if user_id.isdigit() and hmac.compare_digest(make_token(int(user_id)), token):
            return int(user_id)
        return None

    def too_many(request: web.Request) -> bool:
        ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
        now, q = time.monotonic(), attempts[ip]
        while q and now - q[0] > WINDOW:
            q.popleft()
        q.append(now)
        if len(attempts) > 5000:  # не даём словарю расти бесконечно
            for key in [k for k, v in attempts.items() if not v or now - v[-1] > WINDOW]:
                del attempts[key]
        return len(q) > LIMIT

    def card_json(user: User) -> dict:
        n = cfg.free_every
        data = {"login": user.login, "card": user.card, "total": user.total, "favorite": user.favorite,
                "registered": user.registered[:10], "in_telegram": bool(user.tg_id), "free_every": n, "design": user.design,
                "barcode": "data:image/png;base64," + base64.b64encode(make_barcode_png(user.card)).decode()}
        if n >= 2:
            data["done"] = user.total % n
            data["next_free"] = user.total % n == n - 1
        return data

    async def credentials(request: web.Request) -> tuple[str, str]:
        try:
            body = await request.json()
            return str(body["login"]), str(body["password"])
        except Exception:
            raise web.HTTPBadRequest(text='{"error": "Введите логин и пароль."}',
                                     content_type="application/json") from None

    def fail(status: int, message: str) -> web.Response:
        return web.json_response({"error": message}, status=status)

    async def auth(request: web.Request, action) -> web.Response:
        if too_many(request):
            return fail(429, "Слишком много попыток. Подождите пару минут.")
        login, password = await credentials(request)
        try:
            user = await asyncio.to_thread(action, login, password)
        except DBError as e:
            return fail(400, str(e))
        return web.json_response({"token": make_token(user.id), "card": card_json(user)})

    async def register(request: web.Request) -> web.Response:
        return await auth(request, lambda login, password: db.register(login, password, None))

    async def login(request: web.Request) -> web.Response:
        return await auth(request, db.verify)

    async def me(request: web.Request) -> web.Response:
        user_id = read_token(request)
        user = await asyncio.to_thread(db.by_id, user_id) if user_id else None
        if not user:
            return fail(401, "Войдите заново.")
        return web.json_response({"card": card_json(user)})

    async def design(request: web.Request) -> web.Response:
        """Гость выбрал фон карты на сайте — запоминаем, чтобы бот присылал такую же карту."""
        user_id = read_token(request)
        if not user_id:
            return fail(401, "Войдите заново.")
        try:
            number = int((await request.json())["design"])
            user = await asyncio.to_thread(db.set_design, user_id, number)
        except DBError as e:
            return fail(400, str(e))
        except Exception:
            return fail(400, "Такого дизайна нет.")
        return web.json_response({"card": card_json(user)})

    @web.middleware
    async def cors(request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            try:
                response = await handler(request)
            except web.HTTPException as e:
                response = e
        response.headers["Access-Control-Allow-Origin"] = cfg.site_origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "86400"
        return response

    app.middlewares.append(cors)
    app.router.add_post("/api/register", register)
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/me", me)
    app.router.add_post("/api/design", design)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda _: web.Response(status=204))
