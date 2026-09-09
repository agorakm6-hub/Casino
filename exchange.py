import asyncio
import json
import logging
import os
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import aiohttp
from typing import Callable, Dict, Any, Awaitable

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageActionStarGift, InputSavedStarGiftUser
from telethon.tl.functions.payments import ConvertStarGiftRequest

# ================= НАСТРОЙКИ =================
# ВАЖНО: цветные кнопки (style=...) требуют aiogram >= 3.20 (Bot API 9.4)
BOT_TOKEN = os.environ["BOT_TOKEN"]

# ID администратора (тебе). Можно переопределить через переменную окружения
# ADMIN_CHAT_ID в Render, но по умолчанию уже стоит твой ID.
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "6811074441"))

# Данные для юзербота банка (Telethon). API_ID/API_HASH — с my.telegram.org,
# обязательно ТЕ ЖЕ, которыми была сгенерирована BANK_SESSION_STRING.
TELETHON_API_ID = int(os.environ["TELETHON_API_ID"])
TELETHON_API_HASH = os.environ["TELETHON_API_HASH"]
BANK_SESSION_STRING = os.environ["BANK_SESSION_STRING"]

WEBHOOK_PATH = "/webhook"
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8080))

BANK_USERNAME = "dalscam"
SUPPORT_USERNAME = "atexsupport"
INFO_CHANNEL_URL = "https://t.me/goldchanga"

BONUS_MULTIPLIER = 1.06  # +6% компенсации комиссии Telegram на все пополнения, кроме первого

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
# ============================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

bank_client = TelegramClient(StringSession(BANK_SESSION_STRING), TELETHON_API_ID, TELETHON_API_HASH)

# ================= ХРАНИЛИЩЕ ПОЛЬЗОВАТЕЛЕЙ =================
# Тестовая версия: всё хранится в JSON-файле на диске.
# Для продакшена стоит перейти на нормальную БД (SQLite/Postgres) —
# файл может теряться при пересоздании диска на Render.

def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Не удалось прочитать users.json: {e}")
    return {}

def save_users(data: dict) -> None:
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Не удалось сохранить users.json: {e}")

users = load_users()
# users[user_id_str] = {
#   "username": str | None,
#   "balance": float,
#   "first_deposit_done": bool,
#   "warnings": int,
#   "banned": bool,
#   "ban_reason": str | None,
# }

def get_user(user_id: int) -> dict:
    key = str(user_id)
    if key not in users:
        users[key] = {
            "username": None,
            "balance": 0,
            "first_deposit_done": False,
            "warnings": 0,
            "banned": False,
            "ban_reason": None,
        }
    return users[key]

def register_user(tg_user: types.User) -> None:
    user = get_user(tg_user.id)
    user["username"] = tg_user.username
    save_users(users)

def find_user_id_by_username(username: str) -> int | None:
    username = username.lstrip("@").lower()
    for uid, data in users.items():
        if data.get("username") and data["username"].lower() == username:
            return int(uid)
    return None

def resolve_target(arg: str) -> int | None:
    """Принимает @username или числовой ID, возвращает user_id или None"""
    arg = arg.strip()
    if arg.startswith("@"):
        return find_user_id_by_username(arg)
    if arg.isdigit():
        return int(arg)
    return None

# ================= ОБЩАЯ ЛОГИКА НАЧИСЛЕНИЯ БАЛАНСА =================
async def credit_user(user_id: int, original_amount: float, resold_amount: float, source: str = "авто") -> float:
    """
    Единая точка начисления. Первое пополнение — без комиссии, целиком по номиналу.
    Все следующие — по сумме, полученной при продаже, +6% бонус.
    Используется и автопродажей подарков, и ручной командой /credit.
    """
    user = get_user(user_id)

    if not user["first_deposit_done"]:
        credited = original_amount
        user["first_deposit_done"] = True
        user_msg = (
            f"<b>✅ Вы успешно пополнили баланс на {original_amount:g}⭐={credited:g}🪎 голды</b>\n"
            f"<i>Первое пополнение — без комиссии!</i>"
        )
    else:
        credited = round(resold_amount * BONUS_MULTIPLIER, 2)
        user_msg = f"<b>✅ Вы успешно пополнили баланс на {original_amount:g}⭐={credited:g}🪎 голды</b>"

    user["balance"] += credited
    save_users(users)

    try:
        await bot.send_message(chat_id=user_id, text=user_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🎁 <b>Пополнение ({source})</b>\n"
                f"От: <code>{user_id}</code>\n"
                f"Номинал: {original_amount:g}⭐ → получено {resold_amount:g}⭐ → начислено {credited:g}🪎"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    return credited

# ================= TELETHON: АВТОПРОДАЖА ПОДАРКОВ =================
@bank_client.on(events.NewMessage)
async def handle_incoming_gift(event):
    action = getattr(event.message, "action", None)
    if not isinstance(action, MessageActionStarGift):
        return
    if event.message.out:
        return  # игнорируем свои же исходящие сообщения

    sender_id = event.message.sender_id
    if sender_id is None:
        return

    original_amount = action.gift.stars
    resold_amount = action.convert_stars

    if not resold_amount:
        logger.warning(f"Подарок от {sender_id} нельзя конвертировать (convert_stars=0)")
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"⚠️ <b>Подарок нельзя автоматически продать</b>\n"
                    f"От: <code>{sender_id}</code>, номинал {original_amount:g}⭐\n"
                    f"Скорее всего это коллекционный (NFT) подарок — обработай вручную."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    try:
        await bank_client(ConvertStarGiftRequest(
            stargift=InputSavedStarGiftUser(msg_id=event.message.id)
        ))
    except Exception as e:
        logger.error(f"Не удалось продать подарок от {sender_id}: {e}")
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"❌ <b>Ошибка продажи подарка</b> от <code>{sender_id}</code>: {e}",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    await credit_user(sender_id, original_amount, resold_amount, source="автопродажа подарка")

# Пользователи, которые сейчас должны прислать скриншот для вывода
pending_withdrawals: dict[int, float] = {}

# Ожидание причины бана/предупреждения от админа: {"action": "ban"/"warn", "target_id": int}
pending_admin_reason: dict | None = None

# ================= MIDDLEWARE: РЕГИСТРАЦИЯ + ПРОВЕРКА БАНА =================
class UserGateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        if tg_user.id == ADMIN_CHAT_ID:
            return await handler(event, data)

        register_user(tg_user)

        if get_user(tg_user.id)["banned"]:
            if isinstance(event, CallbackQuery):
                await event.answer()
            return

        return await handler(event, data)

dp.message.outer_middleware(UserGateMiddleware())
dp.callback_query.outer_middleware(UserGateMiddleware())

# ================= КЛАВИАТУРЫ =================
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔄 ОБМЕН", callback_data="exchange", style="primary")
    )
    builder.row(
        InlineKeyboardButton(text="🆘 ПОДДЕРЖКА", url=f"https://t.me/{SUPPORT_USERNAME}", style="primary")
    )
    builder.row(
        InlineKeyboardButton(text="❔ ВАЖНАЯ ИНФОРМАЦИЯ", url=INFO_CHANNEL_URL, style="success")
    )
    return builder.as_markup()

def get_exchange_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📤 ОТПРАВИТЬ ПОДАРОК", url=f"https://t.me/{BANK_USERNAME}", style="primary")
    )
    builder.row(
        InlineKeyboardButton(text="💰 БАЛАНС", callback_data="balance", style="success")
    )
    builder.row(
        InlineKeyboardButton(text="💸 ВЫВЕСТИ", callback_data="withdraw", style="primary")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 НАЗАД", callback_data="back_to_main")
    )
    return builder.as_markup()

def get_balance_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💸 ВЫВЕСТИ", callback_data="withdraw", style="primary")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 НАЗАД", callback_data="exchange")
    )
    return builder.as_markup()

def get_back_to_exchange_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔙 НАЗАД", callback_data="exchange")
    )
    return builder.as_markup()

def get_exchange_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ ПОДТВЕРДИТЬ ОБМЕН", callback_data="confirm_withdraw", style="success")
    )
    builder.row(
        InlineKeyboardButton(text="📤 ОТПРАВИТЬ ЕЩЁ ПОДАРОК", callback_data="exchange_deposit_info", style="primary")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 НАЗАД", callback_data="back_to_main")
    )
    return builder.as_markup()

def get_admin_confirm_keyboard(user_id: int, amount: float) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ СООБЩИТЬ О ПОКУПКЕ СКИНА",
            callback_data=f"confirm_{user_id}_{amount}",
            style="success"
        )
    )
    return builder.as_markup()

# ================= ТЕКСТЫ =================
MAIN_MENU_TEXT = (
    "<b>GOLD EXCHANGE</b> ⭐↔️🪎\n\n"
    "<b>Обменивай Telegram Stars на голду в Standoff 2.</b>"
)

EXCHANGE_WELCOME_TEXT = (
    "<b>ДОБРО ПОЖАЛОВАТЬ</b>\n\n"
    "<b>Здесь вы можете обменять Telegram Stars (Stars) на голду в Standoff 2.</b>\n\n"
    f"<b>Отправьте любой подарок Telegram на наш банк-аккаунт: @{BANK_USERNAME}</b>\n"
    "<b>Баланс пополнится автоматически, как только подарок будет продан.</b>"
)

def exchange_confirm_text(amount: float) -> str:
    return (
        f"<b>У вас на балансе {amount:g} 🪎 голды.</b>\n\n"
        "<b>Обменять её сейчас?</b>"
    )

# ================= ПОКАЗ ЭКРАНОВ =================
async def show_main_menu(message_or_callback):
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(
            MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=get_main_menu_keyboard()
        )
    else:
        await message_or_callback.message.edit_text(
            MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=get_main_menu_keyboard()
        )

async def show_exchange_screen(callback: CallbackQuery):
    await callback.message.edit_text(
        EXCHANGE_WELCOME_TEXT, parse_mode="HTML", reply_markup=get_exchange_keyboard()
    )

async def show_balance_screen(callback: CallbackQuery):
    amount = get_user(callback.from_user.id)["balance"]
    text = f"<b>У вас {amount:g} 🪎 голды</b>"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=get_balance_keyboard()
    )

# ================= /start =================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await show_main_menu(message)

# ================= ГЛАВНОЕ МЕНЮ =================
@dp.callback_query(F.data == "back_to_main")
async def process_back_to_main(callback: CallbackQuery):
    await callback.answer()
    await show_main_menu(callback)

@dp.callback_query(F.data == "exchange")
async def process_exchange(callback: CallbackQuery):
    await callback.answer()
    amount = get_user(callback.from_user.id)["balance"]
    if amount > 0:
        await callback.message.edit_text(
            exchange_confirm_text(amount), parse_mode="HTML", reply_markup=get_exchange_confirm_keyboard()
        )
    else:
        await show_exchange_screen(callback)

@dp.callback_query(F.data == "exchange_deposit_info")
async def process_exchange_deposit_info(callback: CallbackQuery):
    await callback.answer()
    await show_exchange_screen(callback)

@dp.callback_query(F.data == "balance")
async def process_balance(callback: CallbackQuery):
    await callback.answer()
    await show_balance_screen(callback)

# ================= ВЫВОД: ПОДТВЕРЖДЕНИЕ И СТАРТ =================
async def start_withdrawal_flow(callback: CallbackQuery):
    user_id = callback.from_user.id
    amount = get_user(user_id)["balance"]

    if amount <= 0:
        await callback.answer("У вас пока нет голды на балансе", show_alert=True)
        return

    pending_withdrawals[user_id] = amount

    text = (
        "<b>ВЫВОД ГОЛДЫ</b>\n\n"
        f"<b>Поставьте на продажу в Standoff 2 любой скин с паттерном "
        f"на сумму {amount:g} голды.</b>\n\n"
        "<b>Сделайте скриншот, на котором видно скин с паттерном "
        "и вашу игровую аватарку, и пришлите его сюда следующим сообщением.</b>\n\n"
        "<b>Вывод занимает до 24 часов.</b>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=get_back_to_exchange_keyboard()
    )

@dp.callback_query(F.data == "withdraw")
async def process_withdraw_start(callback: CallbackQuery):
    await callback.answer()
    amount = get_user(callback.from_user.id)["balance"]

    if amount <= 0:
        await callback.answer("У вас пока нет голды на балансе", show_alert=True)
        return

    # Кнопка "Вывести" (например, с экрана баланса) сразу ведёт на подтверждение,
    # а не блокирует баланс — блокировка происходит только по нажатию "Подтвердить обмен"
    await callback.message.edit_text(
        exchange_confirm_text(amount), parse_mode="HTML", reply_markup=get_exchange_confirm_keyboard()
    )

@dp.callback_query(F.data == "confirm_withdraw")
async def process_confirm_withdraw(callback: CallbackQuery):
    await callback.answer()
    await start_withdrawal_flow(callback)

# ================= ВЫВОД: ПОЛУЧЕНИЕ СКРИНШОТА =================
@dp.message(F.photo)
async def process_withdraw_screenshot(message: types.Message):
    user_id = message.from_user.id

    if user_id not in pending_withdrawals:
        return

    amount = pending_withdrawals.pop(user_id)

    user = get_user(user_id)
    user["balance"] -= amount
    save_users(users)

    await message.answer(
        f"<b>✅ Заявка на вывод {amount:g} 🪎 принята.</b>\n\n"
        "<b>Баланс списан. Ожидайте подтверждения в течение 24 часов.</b>",
        parse_mode="HTML"
    )

    username = f"@{message.from_user.username}" if message.from_user.username else "без username"
    caption = (
        f"<b>📥 НОВАЯ ЗАЯВКА НА ВЫВОД</b>\n\n"
        f"<b>Пользователь:</b> {username} (ID: <code>{user_id}</code>)\n"
        f"<b>Сумма:</b> {amount:g} 🪎"
    )
    await bot.send_photo(
        chat_id=ADMIN_CHAT_ID,
        photo=message.photo[-1].file_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=get_admin_confirm_keyboard(user_id, amount)
    )

# ================= ВЫВОД: ПОДТВЕРЖДЕНИЕ АДМИНОМ =================
@dp.callback_query(F.data.startswith("confirm_"))
async def process_admin_confirm(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    await callback.answer()
    _, user_id_str, amount_str = callback.data.split("_", 2)
    user_id = int(user_id_str)
    amount = float(amount_str)

    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"<b>✅ Ваш скин был продан за {amount:g} голды. Вывод завершён!</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n<b>✅ ПОДТВЕРЖДЕНО</b>",
        parse_mode="HTML"
    )

# ================= АДМИН: /help =================
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    text = (
        "<b>АДМИН-КОМАНДЫ</b>\n\n"
        "<b>Автопродажа подарков работает сама</b> — баланс начисляется, "
        "как только банк-аккаунт получает подарок. Ниже — команды на случай ручных правок.\n\n"
        "<b>/credit &lt;user_id&gt; &lt;номинал_подарка&gt; &lt;получено_после_продажи&gt;</b>\n"
        "Начислить баланс вручную по логике комиссии (например, если автопродажа не сработала).\n\n"
        "<b>/send &lt;@username|id&gt; &lt;сумма&gt;</b>\n"
        "Начислить голду напрямую, без учёта комиссии/бонуса — просто добавить сумму к балансу "
        "(например, для компенсаций или бонусов).\n\n"
        "<b>/ban &lt;@username|id&gt;</b>\n"
        "Забанить пользователя. Бот спросит причину следующим сообщением.\n\n"
        "<b>/pred &lt;@username|id&gt;</b>\n"
        "Выдать предупреждение. Бот спросит причину. 2 предупреждения = автобан.\n\n"
        "<b>/unban &lt;@username|id&gt;</b>\n"
        "Снять бан и обнулить предупреждения.\n\n"
        "<b>/help</b>\n"
        "Это сообщение."
    )
    await message.answer(text, parse_mode="HTML")

# ================= АДМИН: /credit (ручной фолбэк) =================
@dp.message(Command("credit"))
async def cmd_credit(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    parts = message.text.split()
    if len(parts) != 4:
        await message.answer(
            "Использование:\n/credit <user_id> <номинал_подарка> <получено_после_продажи>\n\n"
            "Пример: /credit 123456789 15 13"
        )
        return

    try:
        target_id = int(parts[1])
        original_amount = float(parts[2])
        resold_amount = float(parts[3])
    except ValueError:
        await message.answer("user_id, номинал и полученная сумма должны быть числами")
        return

    credited = await credit_user(target_id, original_amount, resold_amount, source="ручное начисление")
    await message.answer(f"Начислено {credited:g} пользователю {target_id}.")

# ================= АДМИН: /send (прямое начисление, без логики комиссии) =================
@dp.message(Command("send"))
async def cmd_send(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Использование: /send <@username|user_id> <сумма>\n\n"
            "Пример: /send @ivan 500\nПример: /send 123456789 500"
        )
        return

    target_id = resolve_target(parts[1])
    if target_id is None:
        await message.answer(
            "Не удалось найти пользователя. Если это @username — он должен был "
            "хотя бы раз запускать бота. Иначе используй числовой ID."
        )
        return

    try:
        amount = float(parts[2])
    except ValueError:
        await message.answer("Сумма должна быть числом")
        return

    user = get_user(target_id)
    user["balance"] += amount
    save_users(users)

    await message.answer(f"Начислено {amount:g} 🪎 пользователю {target_id}. Новый баланс: {user['balance']:g}")

    try:
        await bot.send_message(
            chat_id=target_id,
            text=f"<b>🎁 Вы получили {amount:g} 🪎 голды!</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить пользователя {target_id}: {e}")

# ================= АДМИН: /ban и /pred (запуск) =================
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    global pending_admin_reason
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /ban <@username|user_id>")
        return

    target_id = resolve_target(parts[1])
    if target_id is None:
        await message.answer(
            "Не удалось найти пользователя. Если это @username — он должен был "
            "хотя бы раз запускать бота. Иначе используй числовой ID."
        )
        return

    if get_user(target_id)["banned"]:
        await message.answer("Этот пользователь уже забанен.")
        return

    pending_admin_reason = {"action": "ban", "target_id": target_id}
    await message.answer(f"Опишите причину бана для {parts[1]}:")

@dp.message(Command("pred"))
async def cmd_pred(message: types.Message):
    global pending_admin_reason
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /pred <@username|user_id>")
        return

    target_id = resolve_target(parts[1])
    if target_id is None:
        await message.answer(
            "Не удалось найти пользователя. Если это @username — он должен был "
            "хотя бы раз запускать бота. Иначе используй числовой ID."
        )
        return

    if get_user(target_id)["banned"]:
        await message.answer("Этот пользователь уже забанен.")
        return

    pending_admin_reason = {"action": "warn", "target_id": target_id}
    await message.answer(f"Опишите причину предупреждения для {parts[1]}:")

# ================= АДМИН: /unban =================
@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /unban <@username|user_id>")
        return

    target_id = resolve_target(parts[1])
    if target_id is None:
        await message.answer("Не удалось найти пользователя.")
        return

    user = get_user(target_id)
    user["banned"] = False
    user["ban_reason"] = None
    user["warnings"] = 0
    save_users(users)
    await message.answer(f"Пользователь {target_id} разбанен, предупреждения обнулены.")

# ================= АДМИН: ПРИЁМ ПРИЧИНЫ БАНА/ПРЕДУПРЕЖДЕНИЯ =================
@dp.message(F.from_user.id == ADMIN_CHAT_ID, F.text)
async def process_admin_reason(message: types.Message):
    global pending_admin_reason

    if pending_admin_reason is None or message.text.startswith("/"):
        return

    action = pending_admin_reason["action"]
    target_id = pending_admin_reason["target_id"]
    reason = message.text.strip()
    pending_admin_reason = None

    user = get_user(target_id)

    if action == "ban":
        user["banned"] = True
        user["ban_reason"] = reason
        save_users(users)
        await message.answer(f"Пользователь {target_id} забанен. Причина: {reason}")
        try:
            await bot.send_message(
                chat_id=target_id,
                text=f"<b>🚫 Вы больше не можете пользоваться ботом.</b>\nПричина: {reason}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {target_id} о бане: {e}")

    elif action == "warn":
        user["warnings"] += 1
        save_users(users)

        try:
            await bot.send_message(
                chat_id=target_id,
                text=(
                    f"<b>⚠️ Вы получили предупреждение.</b>\n"
                    f"Причина: {reason}\n\n"
                    f"Предупреждений: {user['warnings']}/2"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {target_id} о предупреждении: {e}")

        if user["warnings"] >= 2:
            user["banned"] = True
            user["ban_reason"] = "2 предупреждения"
            save_users(users)
            await message.answer(
                f"Пользователю {target_id} выдано предупреждение ({reason}). "
                f"Это второе предупреждение — пользователь автоматически забанен."
            )
            try:
                await bot.send_message(
                    chat_id=target_id,
                    text="<b>🚫 Вы больше не можете пользоваться ботом.</b>\nПричина: 2 предупреждения",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить пользователя {target_id} о бане: {e}")
        else:
            await message.answer(
                f"Пользователю {target_id} выдано предупреждение ({reason}). "
                f"Всего предупреждений: {user['warnings']}/2"
            )

# ================= WEBHOOK HANDLER ДЛЯ RENDER =================
async def webhook_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_update(bot, update)
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500)

async def health_check(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

async def keep_alive_loop() -> None:
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if not hostname:
        logger.warning("RENDER_EXTERNAL_HOSTNAME не задан — внутренний keep-alive выключен")
        return
    url = f"https://{hostname}/health"
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    logger.info(f"Keep-alive: {resp.status}")
            except Exception as e:
                logger.warning(f"Keep-alive ping не удался: {e}")
            await asyncio.sleep(300)

async def on_startup(app: web.Application) -> None:
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'localhost')}{WEBHOOK_PATH}"
    try:
        await bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook установлен: {webhook_url}")
        me = await bot.get_me()
        logger.info(f"Бот запущен: @{me.username}")
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")

    try:
        await bank_client.start()
        bank_me = await bank_client.get_me()
        logger.info(f"Банк-аккаунт подключен: @{bank_me.username or bank_me.id}")
    except Exception as e:
        logger.error(f"Ошибка подключения банк-аккаунта (Telethon): {e}")

    app["keep_alive_task"] = asyncio.create_task(keep_alive_loop())

async def on_shutdown(app: web.Application) -> None:
    task = app.get("keep_alive_task")
    if task:
        task.cancel()
    try:
        await bot.delete_webhook()
        logger.info("Webhook удален")
    except Exception:
        pass
    try:
        await bank_client.disconnect()
    except Exception:
        pass

# ================= ЗАПУСК =================
async def main() -> None:
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, webhook_handler)
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()

    logger.info(f"Сервер запущен на порту {WEB_SERVER_PORT}")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Остановка...")
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
