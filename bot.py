import logging
import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
from aiohttp import web as aio_web

# ─────────────────────────────────────────
#  КОНФИГУРАЦИЯ
# ─────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN",        "8866545536:AAFe0qMT-k42TgooUtI6CaQ4hdEhs1lWPjI")
CRYPTOBOT_TOKEN= os.environ.get("CRYPTO_PAY_TOKEN", "601949:AA1o0cCR506fhZIEcAYWshHf9g5XDsUNSDS")
CRYPTOBOT_API  = "https://pay.crypt.bot/api"
ADMIN_ID       = int(os.environ.get("ADMIN_ID",   "8325037674"))
CHANNEL_ID     = int(os.environ.get("CHANNEL_ID", "-1004304646007"))

CARD_NUMBER = "2202206747886708"
CARD_PHONE  = "+79024295270"
CARD_NAME   = "Кирилл С."
CARD_BANK   = "Сбербанк"

PLANS = {
    "1m":  {"name": "1 месяц",   "rub": 300,  "usdt": 3.20,  "months": 1},
    "3m":  {"name": "3 месяца",  "rub": 450,  "usdt": 4.80,  "months": 3},
    "6m":  {"name": "6 месяцев", "rub": 550,  "usdt": 5.90,  "months": 6},
    "12m": {"name": "1 год",     "rub": 1100, "usdt": 11.80, "months": 12},
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

pending_payments: dict = {}
pending_manual:   dict = {}


# ─────────────────────────────────────────
#  БАЗА ДАННЫХ
# ─────────────────────────────────────────
def db_init():
    conn = sqlite3.connect("subscriptions.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            plan_key   TEXT,
            expires_at TEXT,
            active     INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

def db_save(user_id: int, username: str, plan_key: str, months: int) -> datetime:
    expires = datetime.now() + timedelta(days=30 * months)
    conn = sqlite3.connect("subscriptions.db")
    conn.execute("""
        INSERT INTO subscriptions (user_id, username, plan_key, expires_at, active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            plan_key=excluded.plan_key,
            expires_at=excluded.expires_at,
            active=1
    """, (user_id, username or "", plan_key, expires.isoformat()))
    conn.commit()
    conn.close()
    return expires

def db_get_expired():
    conn = sqlite3.connect("subscriptions.db")
    rows = conn.execute("""
        SELECT user_id, username FROM subscriptions
        WHERE active=1 AND expires_at < ?
    """, (datetime.now().isoformat(),)).fetchall()
    conn.close()
    return rows

def db_deactivate(user_id: int):
    conn = sqlite3.connect("subscriptions.db")
    conn.execute("UPDATE subscriptions SET active=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def db_get_user(user_id: int):
    conn = sqlite3.connect("subscriptions.db")
    row = conn.execute(
        "SELECT plan_key, expires_at, active FROM subscriptions WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row

def db_get_all():
    conn = sqlite3.connect("subscriptions.db")
    rows = conn.execute(
        "SELECT user_id, username, plan_key, expires_at, active FROM subscriptions ORDER BY expires_at DESC"
    ).fetchall()
    conn.close()
    return rows


# ─────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────
def kb_plans():
    buttons = []
    for key, p in PLANS.items():
        buttons.append([InlineKeyboardButton(
            text=f"{p['name']} — {p['rub']}₽ / {p['usdt']}$",
            callback_data=f"plan:{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_pay_method(plan_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта СБП (Сбербанк)", callback_data=f"pay_card:{plan_key}")],
        [InlineKeyboardButton(text="🪙 Крипто (USDT)", callback_data=f"pay_crypto:{plan_key}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_plans")],
    ])

def kb_admin_approve(user_id: int, plan_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{user_id}:{plan_key}"),
        InlineKeyboardButton(text="❌ Отклонить",   callback_data=f"reject:{user_id}"),
    ]])


# ─────────────────────────────────────────
#  /start
# ─────────────────────────────────────────
class PayState(StatesGroup):
    waiting_screenshot = State()

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    await msg.answer(
        "👋 Привет! Ты покупаешь приватку <b>Lolly Pornogames</b> 🎮🔞\n\n"
        "Здесь ты получишь доступ к эксклюзивному закрытому каналу.\n\n"
        "📦 Тарифы находятся ниже — выбирай подходящий:",
        reply_markup=kb_plans(),
        parse_mode="HTML"
    )


# ─────────────────────────────────────────
#  Выбор тарифа
# ─────────────────────────────────────────
@dp.callback_query(F.data == "back_plans")
async def cb_back(call: CallbackQuery):
    await call.message.edit_text("📦 Выбери тариф подписки:", reply_markup=kb_plans())

@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery):
    plan_key = call.data.split(":")[1]
    p = PLANS[plan_key]
    await call.message.edit_text(
        f"📦 <b>{p['name']}</b>\n"
        f"💰 Цена: <b>{p['rub']}₽</b> / <b>{p['usdt']} USDT</b>\n\n"
        f"Выбери способ оплаты:",
        reply_markup=kb_pay_method(plan_key),
        parse_mode="HTML"
    )


# ─────────────────────────────────────────
#  Оплата картой
# ─────────────────────────────────────────
@dp.callback_query(F.data.startswith("pay_card:"))
async def cb_pay_card(call: CallbackQuery, state: FSMContext):
    plan_key = call.data.split(":")[1]
    p = PLANS[plan_key]
    pending_manual[call.from_user.id] = plan_key
    await call.message.edit_text(
        f"💳 <b>Оплата картой СБП</b>\n\n"
        f"Переведи <b>{p['rub']}₽</b> на карту:\n\n"
        f"🏦 Банк: <b>{CARD_BANK}</b>\n"
        f"💳 Карта: <code>{CARD_NUMBER}</code>\n"
        f"📱 Телефон СБП: <code>{CARD_PHONE}</code>\n"
        f"👤 Получатель: <b>{CARD_NAME}</b>\n\n"
        f"После оплаты отправь скриншот чека сюда 👇",
        parse_mode="HTML"
    )
    await state.set_state(PayState.waiting_screenshot)
    await state.update_data(plan_key=plan_key)

@dp.message(PayState.waiting_screenshot, F.photo | F.document)
async def got_screenshot(msg: types.Message, state: FSMContext):
    data     = await state.get_data()
    plan_key = data.get("plan_key", "?")
    p        = PLANS.get(plan_key, {})
    user     = msg.from_user
    caption  = (
        f"💳 <b>Новый платёж (карта)</b>\n"
        f"👤 {user.full_name} (@{user.username or '—'}) | ID: <code>{user.id}</code>\n"
        f"📦 Тариф: {p.get('name','?')} — {p.get('rub','?')}₽\n\n"
        f"Проверь перевод и подтверди:"
    )
    if msg.photo:
        await bot.send_photo(ADMIN_ID, msg.photo[-1].file_id, caption=caption,
                             reply_markup=kb_admin_approve(user.id, plan_key), parse_mode="HTML")
    else:
        await bot.send_document(ADMIN_ID, msg.document.file_id, caption=caption,
                                reply_markup=kb_admin_approve(user.id, plan_key), parse_mode="HTML")
    await msg.answer("✅ Чек получен! Ожидай подтверждения (обычно до 15 минут).")
    await state.clear()


# ─────────────────────────────────────────
#  Оплата криптой
# ─────────────────────────────────────────
async def create_cryptobot_invoice(amount_usdt: float, plan_key: str, user_id: int):
    payload = {
        "asset": "USDT",
        "amount": str(amount_usdt),
        "description": f"Подписка: {PLANS[plan_key]['name']}",
        "payload": f"{user_id}:{plan_key}",
        "paid_btn_name": "callback",
        "paid_btn_url": f"https://t.me/{(await bot.get_me()).username}",
        "allow_comments": False,
        "allow_anonymous": False,
    }
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{CRYPTOBOT_API}/createInvoice", json=payload, headers=headers) as r:
            data = await r.json()
            if data.get("ok"):
                return data["result"]
    return None

@dp.callback_query(F.data.startswith("pay_crypto:"))
async def cb_pay_crypto(call: CallbackQuery):
    plan_key = call.data.split(":")[1]
    p = PLANS[plan_key]
    await call.message.edit_text("⏳ Создаю счёт в CryptoBot...")
    invoice = await create_cryptobot_invoice(p["usdt"], plan_key, call.from_user.id)
    if not invoice:
        await call.message.edit_text("❌ Ошибка при создании счёта. Попробуй позже или выбери оплату картой.")
        return
    pending_payments[invoice["invoice_id"]] = {
        "user_id":  call.from_user.id,
        "plan_key": plan_key,
        "username": call.from_user.username or "",
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💸 Оплатить {p['usdt']} USDT", url=invoice["pay_url"])
    ]])
    await call.message.edit_text(
        f"🪙 <b>Оплата криптой</b>\n\n"
        f"Тариф: <b>{p['name']}</b>\n"
        f"Сумма: <b>{p['usdt']} USDT</b>\n\n"
        f"Нажми кнопку ниже для оплаты через CryptoBot.\n"
        f"После оплаты ссылка придёт автоматически! ✅",
        reply_markup=kb,
        parse_mode="HTML"
    )


# ─────────────────────────────────────────
#  Поллинг CryptoBot
# ─────────────────────────────────────────
async def poll_cryptobot():
    if not pending_payments:
        return
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    ids = list(pending_payments.keys())
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{CRYPTOBOT_API}/getInvoices",
            params={"invoice_ids": ",".join(map(str, ids)), "status": "paid"},
            headers=headers
        ) as r:
            data = await r.json()
            if not data.get("ok"):
                return
            for inv in data["result"].get("items", []):
                inv_id = inv["invoice_id"]
                if inv_id in pending_payments:
                    info = pending_payments.pop(inv_id)
                    await grant_access(info["user_id"], info["plan_key"],
                                       method="crypto", username=info.get("username", ""))

@dp.message(Command("check_crypto"))
async def check_crypto(msg: types.Message):
    await msg.answer("Проверяю...")
    await poll_cryptobot()


# ─────────────────────────────────────────
#  Подтверждение / отклонение (Админ)
# ─────────────────────────────────────────
@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    _, user_id, plan_key = call.data.split(":")
    await grant_access(int(user_id), plan_key, method="card",
                       username=call.from_user.username or "")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.reply(f"✅ Доступ выдан пользователю {user_id}")

@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    user_id = int(call.data.split(":")[1])
    await bot.send_message(user_id,
        "❌ Оплата не подтверждена. Если уже перевёл средства — свяжись с поддержкой.")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.reply(f"❌ Платёж пользователя {user_id} отклонён")


# ─────────────────────────────────────────
#  Выдача доступа
# ─────────────────────────────────────────
async def grant_access(user_id: int, plan_key: str, method: str, username: str = ""):
    p = PLANS[plan_key]
    try:
        link    = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1,
                                                    name=f"user_{user_id}_{plan_key}")
        expires = db_save(user_id, username, plan_key, p["months"])
        exp_str = expires.strftime("%d.%m.%Y")
        method_str = "💳 карта" if method == "card" else "🪙 крипта"

        await bot.send_message(
            user_id,
            f"🎉 <b>Оплата подтверждена!</b>\n\n"
            f"📦 Тариф: <b>{p['name']}</b>\n"
            f"💰 Способ: {method_str}\n"
            f"📅 Подписка до: <b>{exp_str}</b>\n\n"
            f"🔗 Твоя одноразовая ссылка:\n{link.invite_link}\n\n"
            f"⚠️ Ссылка одноразовая — не передавай её другим!\n"
            f"✅ После вступления ссылка станет недействительной.",
            parse_mode="HTML"
        )
        await bot.send_message(
            ADMIN_ID,
            f"✅ Доступ выдан\n"
            f"👤 @{username or '—'} | ID: <code>{user_id}</code>\n"
            f"📦 {p['name']} | {method_str}\n"
            f"📅 До: {exp_str}",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка выдачи доступа: {e}")
        await bot.send_message(user_id, "⚠️ Ошибка при создании ссылки. Напиши администратору.")
        await bot.send_message(ADMIN_ID, f"❗ Ошибка выдачи доступа user {user_id}: {e}")


# ─────────────────────────────────────────
#  WEBHOOK CryptoBot
# ─────────────────────────────────────────
async def cryptobot_webhook(request: aio_web.Request):
    try:
        if request.headers.get("crypto-pay-api-token", "") != CRYPTOBOT_TOKEN:
            return aio_web.Response(status=403, text="Forbidden")
        data = await request.json()
        if data.get("update_type") != "invoice_paid":
            return aio_web.Response(text="ok")
        inv         = data.get("payload", {})
        invoice_id  = inv.get("invoice_id")
        payload_str = inv.get("payload", "")
        if ":" in payload_str:
            user_id_str, plan_key = payload_str.split(":", 1)
            pending_payments.pop(invoice_id, None)
            await grant_access(int(user_id_str), plan_key, method="crypto")
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return aio_web.Response(text="ok")

async def start_webhook_server():
    app = aio_web.Application()
    app.router.add_post("/cryptobot", cryptobot_webhook)
    runner = aio_web.AppRunner(app)
    await runner.setup()
    await aio_web.TCPSite(runner, "0.0.0.0", 8080).start()
    logging.info("Webhook: :8080/cryptobot")


# ─────────────────────────────────────────
#  АВТОКИК — проверка истёкших подписок
# ─────────────────────────────────────────
async def subscription_checker():
    while True:
        await asyncio.sleep(3600)  # раз в час
        try:
            expired = db_get_expired()
            for user_id, username in expired:
                try:
                    await bot.ban_chat_member(CHANNEL_ID, user_id)
                    await bot.unban_chat_member(CHANNEL_ID, user_id)
                    db_deactivate(user_id)
                    await bot.send_message(
                        user_id,
                        "⏰ <b>Твоя подписка истекла!</b>\n\n"
                        "Доступ к каналу закрыт.\n"
                        "Чтобы продолжить — выбери новый тариф:",
                        reply_markup=kb_plans(),
                        parse_mode="HTML"
                    )
                    logging.info(f"Кикнут {user_id} @{username}")
                except Exception as e:
                    logging.error(f"Ошибка кика {user_id}: {e}")
                    db_deactivate(user_id)
            if expired:
                await bot.send_message(
                    ADMIN_ID,
                    f"🔔 Автокик: {len(expired)} подписок истекло\n"
                    + "\n".join(f"• @{u} ({uid})" for uid, u in expired),
                    parse_mode="HTML"
                )
        except Exception as e:
            logging.error(f"Checker error: {e}")


# ─────────────────────────────────────────
#  Фоновый поллинг CryptoBot (резерв)
# ─────────────────────────────────────────
async def crypto_poller():
    while True:
        await asyncio.sleep(60)
        try:
            await poll_cryptobot()
        except Exception as e:
            logging.error(f"Poller error: {e}")


# ─────────────────────────────────────────
#  Команды
# ─────────────────────────────────────────
@dp.message(Command("mysub"))
async def cmd_mysub(msg: types.Message):
    row = db_get_user(msg.from_user.id)
    if not row:
        await msg.answer("У тебя нет активной подписки.\n\nВыбери тариф:", reply_markup=kb_plans())
        return
    plan_key, expires_at, active = row
    p        = PLANS.get(plan_key, {})
    expires  = datetime.fromisoformat(expires_at)
    days_left = (expires - datetime.now()).days
    status   = "✅ Активна" if active and days_left >= 0 else "❌ Истекла"
    await msg.answer(
        f"📋 <b>Твоя подписка</b>\n\n"
        f"📦 Тариф: {p.get('name', plan_key)}\n"
        f"📅 До: {expires.strftime('%d.%m.%Y')}\n"
        f"⏳ Осталось: {max(0, days_left)} дн.\n"
        f"Статус: {status}",
        parse_mode="HTML"
    )

@dp.message(Command("admin"))
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        "🛠 <b>Панель администратора</b>\n\n"
        f"⏳ Крипто ожидают: {len(pending_payments)}\n"
        f"⏳ Ручных ожидают: {len(pending_manual)}\n\n"
        "Команды:\n"
        "/subs — список всех подписчиков\n"
        "/check_crypto — проверить крипто-оплаты\n"
        "/admin — эта панель",
        parse_mode="HTML"
    )

@dp.message(Command("subs"))
async def cmd_subs(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    rows = db_get_all()
    if not rows:
        await msg.answer("Подписчиков пока нет.")
        return
    lines = ["📊 <b>Все подписки:</b>\n"]
    for user_id, username, plan_key, expires_at, active in rows:
        p         = PLANS.get(plan_key, {})
        expires   = datetime.fromisoformat(expires_at)
        days_left = (expires - datetime.now()).days
        icon      = "✅" if active and days_left >= 0 else "❌"
        lines.append(
            f"{icon} @{username or '—'} ({user_id}) — "
            f"{p.get('name', plan_key)}, до {expires.strftime('%d.%m.%Y')} ({max(0,days_left)} дн.)"
        )
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ─────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────
async def main():
    db_init()
    await start_webhook_server()
    asyncio.create_task(crypto_poller())
    asyncio.create_task(subscription_checker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
