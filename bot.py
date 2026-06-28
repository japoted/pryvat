import logging
import asyncio
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
import json

# ─────────────────────────────────────────
#  КОНФИГУРАЦИЯ (из переменных окружения)
# ─────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8866545536:AAFe0qMT-k42TgooUtI6CaQ4hdEhs1lWPjI")
CRYPTOBOT_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "601949:AA1o0cCR506fhZIEcAYWshHf9g5XDsUNSDS")
CRYPTOBOT_API = "https://pay.crypt.bot/api"

ADMIN_ID = int(os.environ.get("ADMIN_ID", "8325037674"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-1004304646007"))

# Реквизиты для оплаты картой
CARD_NUMBER = "2202206747886708"
CARD_PHONE = "+79024295270"
CARD_NAME = "Кирилл С."
CARD_BANK = "Сбербанк"

# Тарифы: (название, рубли, USDT, месяцев)
PLANS = {
    "1m":  {"name": "1 месяц",   "rub": 300,  "usdt": 3.20,  "months": 1},
    "3m":  {"name": "3 месяца",  "rub": 450,  "usdt": 4.80,  "months": 3},
    "6m":  {"name": "6 месяцев", "rub": 550,  "usdt": 5.90,  "months": 6},
    "12m": {"name": "1 год",     "rub": 1100, "usdt": 11.80, "months": 12},
}

# ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Хранилище ожидающих оплат {invoice_id: {user_id, plan_key, method}}
pending_payments: dict = {}
# Ожидают подтверждения вручную {user_id: plan_key}
pending_manual: dict = {}


class PayState(StatesGroup):
    waiting_screenshot = State()


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
        [InlineKeyboardButton(text="🪙 Крипто (USDT/BTC/ETH)", callback_data=f"pay_crypto:{plan_key}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_plans")],
    ])


def kb_admin_approve(user_id: int, plan_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{user_id}:{plan_key}"),
            InlineKeyboardButton(text="❌ Отклонить",   callback_data=f"reject:{user_id}"),
        ]
    ])


# ─────────────────────────────────────────
#  /start
# ─────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    await msg.answer(
        "👋 Привет! Это бот для доступа к приватному каналу.\n\n"
        "Выбери тариф подписки:",
        reply_markup=kb_plans()
    )


# ─────────────────────────────────────────
#  Выбор тарифа
# ─────────────────────────────────────────
@dp.callback_query(F.data == "back_plans")
async def cb_back(call: CallbackQuery):
    await call.message.edit_text("Выбери тариф подписки:", reply_markup=kb_plans())


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
    data = await state.get_data()
    plan_key = data.get("plan_key", "?")
    p = PLANS.get(plan_key, {})
    user = msg.from_user

    # Уведомить админа
    caption = (
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
#  Оплата криптой (CryptoBot)
# ─────────────────────────────────────────
async def create_cryptobot_invoice(amount_usdt: float, plan_key: str, user_id: int) -> dict | None:
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
        "user_id": call.from_user.id,
        "plan_key": plan_key,
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💸 Оплатить {p['usdt']} USDT", url=invoice["pay_url"])],
    ])
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
#  Вебхук от CryptoBot (polling fallback)
# ─────────────────────────────────────────
@dp.message(Command("check_crypto"))
async def check_crypto(msg: types.Message):
    """Ручная проверка — пользователь пишет /check_crypto после оплаты"""
    if not pending_payments:
        await msg.answer("Нет ожидающих криптоплатежей.")
        return
    await msg.answer("🔍 Проверяю оплату...")
    await poll_cryptobot()


async def poll_cryptobot():
    """Проверяем оплаченные инвойсы через API"""
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
                    await grant_access(info["user_id"], info["plan_key"], method="crypto")


# ─────────────────────────────────────────
#  Подтверждение/отклонение (Админ)
# ─────────────────────────────────────────
@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    _, user_id, plan_key = call.data.split(":")
    await grant_access(int(user_id), plan_key, method="card")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.reply(f"✅ Доступ выдан пользователю {user_id}")


@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    user_id = int(call.data.split(":")[1])
    await bot.send_message(user_id,
        "❌ Оплата не подтверждена. Если ты уже перевёл средства — свяжись с поддержкой.")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.reply(f"❌ Платёж пользователя {user_id} отклонён")


# ─────────────────────────────────────────
#  Выдача доступа
# ─────────────────────────────────────────
async def grant_access(user_id: int, plan_key: str, method: str):
    p = PLANS[plan_key]
    try:
        link = await bot.create_chat_invite_link(
            CHANNEL_ID,
            member_limit=1,
            name=f"user_{user_id}_{plan_key}"
        )
        method_str = "💳 карта" if method == "card" else "🪙 крипта"
        await bot.send_message(
            user_id,
            f"🎉 <b>Оплата подтверждена!</b>\n\n"
            f"📦 Тариф: <b>{p['name']}</b>\n"
            f"💰 Способ: {method_str}\n\n"
            f"🔗 Твоя одноразовая ссылка для вступления в канал:\n"
            f"{link.invite_link}\n\n"
            f"⚠️ Ссылка одноразовая — не передавай её другим!\n"
            f"✅ После вступления ссылка станет недействительной.",
            parse_mode="HTML"
        )
        # Уведомить админа
        await bot.send_message(
            ADMIN_ID,
            f"✅ Доступ выдан\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"📦 Тариф: {p['name']}\n"
            f"💰 Метод: {method_str}",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка при выдаче доступа: {e}")
        await bot.send_message(user_id, "⚠️ Ошибка при создании ссылки. Напиши администратору.")
        await bot.send_message(ADMIN_ID, f"❗ Ошибка выдачи доступа user {user_id}: {e}")


# ─────────────────────────────────────────
#  WEBHOOK от CryptoBot (мгновенное уведомление об оплате)
# ─────────────────────────────────────────
from aiohttp import web as aio_web

async def cryptobot_webhook(request: aio_web.Request):
    """Принимает POST от CryptoBot при оплате — ссылка выдаётся мгновенно"""
    try:
        token_header = request.headers.get("crypto-pay-api-token", "")
        if token_header != CRYPTOBOT_TOKEN:
            return aio_web.Response(status=403, text="Forbidden")

        data = await request.json()
        if data.get("update_type") != "invoice_paid":
            return aio_web.Response(text="ok")

        inv = data.get("payload", {})
        invoice_id = inv.get("invoice_id")
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
    site = aio_web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logging.info("✅ Webhook server: http://0.0.0.0:8080/cryptobot")


# ─────────────────────────────────────────
#  Фоновый поллинг CryptoBot каждые 60 сек (резерв)
# ─────────────────────────────────────────
async def crypto_poller():
    while True:
        await asyncio.sleep(60)
        try:
            await poll_cryptobot()
        except Exception as e:
            logging.error(f"Poller error: {e}")


# ─────────────────────────────────────────
#  Команды для админа
# ─────────────────────────────────────────
@dp.message(Command("admin"))
async def cmd_admin(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        "🛠 <b>Панель администратора</b>\n\n"
        f"⏳ Ожидают крипто-оплаты: {len(pending_payments)}\n"
        f"⏳ Ожидают ручного подтверждения: {len(pending_manual)}\n\n"
        "Команды:\n"
        "/check_crypto — проверить оплаченные крипто-счета\n"
        "/admin — эта панель",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────
async def main():
    await start_webhook_server()          # вебхук для мгновенной крипто-оплаты
    asyncio.create_task(crypto_poller())  # резервный поллинг раз в 60 сек
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
