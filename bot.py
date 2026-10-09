import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
from sqlalchemy import (
    Column, Integer, String, BigInteger, Boolean, DateTime,
    select, func, delete, or_,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------------- CONFIG ----------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", 9091))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/v1/otp/deliveries")
OTP_WEBHOOK_SECRET = os.getenv("OTP_WEBHOOK_SECRET", "")

USER_LIMIT = 2
AUTO_GEN_ON_DEMAND = True
AUTO_GEN_COUNT = 1

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shuzabot")

# ---------------- DB ----------------
Base = declarative_base()


class PhoneNumber(Base):
    __tablename__ = "bot_phone_numbers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    number = Column(String, unique=True, nullable=False)
    zone = Column(String, nullable=False, index=True)
    is_busy = Column(Boolean, default=False, index=True)
    user_id = Column(BigInteger, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    bound_at = Column(DateTime, nullable=True)


class User(Base):
    __tablename__ = "bot_users"
    user_id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    phone_count = Column(Integer, default=0)
    first_seen = Column(DateTime, default=datetime.utcnow)


engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("DB initialized")


# ---------------- COUNTRY CODES ----------------
COUNTRY_CODES = {
    "380": ("🇺🇦", 12, ""),
    "375": ("🇧🇾", 12, ""),
    "373": ("🇲🇩", 11, ""),
    "370": ("🇱🇹", 11, ""),
    "371": ("🇱🇻", 11, ""),
    "372": ("🇪🇪", 11, ""),
    "880": ("🇧🇩", 13, "1"),
    "888": ("🌐", 11, ""),
    "995": ("🇬🇪", 12, ""),
    "994": ("🇦🇿", 12, ""),
    "998": ("🇺🇿", 12, ""),
    "996": ("🇰🇬", 12, ""),
    "992": ("🇹🇯", 12, ""),
    "993": ("🇹🇲", 11, ""),
    "44":  ("🇬🇧", 12, ""),
    "49":  ("🇩🇪", 12, ""),
    "48":  ("🇵🇱", 11, ""),
    "39":  ("🇮🇹", 11, ""),
    "33":  ("🇫🇷", 11, ""),
    "34":  ("🇪🇸", 11, ""),
    "31":  ("🇳🇱", 11, ""),
    "32":  ("🇧🇪", 11, ""),
    "36":  ("🇭🇺", 11, ""),
    "40":  ("🇷🇴", 11, ""),
    "41":  ("🇨🇭", 11, ""),
    "43":  ("🇦🇹", 11, ""),
    "45":  ("🇩🇰", 10, ""),
    "46":  ("🇸🇪", 11, ""),
    "47":  ("🇳🇴", 10, ""),
    "51":  ("🇵🇪", 11, ""),
    "52":  ("🇲🇽", 12, ""),
    "54":  ("🇦🇷", 12, ""),
    "55":  ("🇧🇷", 12, ""),
    "56":  ("🇨🇱", 11, ""),
    "57":  ("🇨🇴", 12, ""),
    "60":  ("🇲🇾", 11, ""),
    "61":  ("🇦🇺", 11, ""),
    "62":  ("🇮🇩", 12, ""),
    "63":  ("🇵🇭", 12, ""),
    "64":  ("🇳🇿", 11, ""),
    "65":  ("🇸🇬", 10, ""),
    "66":  ("🇹🇭", 11, ""),
    "81":  ("🇯🇵", 11, ""),
    "82":  ("🇰🇷", 11, ""),
    "84":  ("🇻🇳", 11, ""),
    "86":  ("🇨🇳", 13, ""),
    "90":  ("🇹🇷", 12, ""),
    "91":  ("🇮🇳", 12, ""),
    "92":  ("🇵🇰", 12, ""),
    "93":  ("🇦🇫", 11, ""),
    "94":  ("🇱🇰", 11, ""),
    "95":  ("🇲🇲", 11, ""),
    "98":  ("🇮🇷", 12, ""),
    "1":   ("🇺🇸", 11, ""),
    "7":   ("🇷🇺", 11, "9"),
}

# Зоны, которые всегда показываются кнопками (даже если пустые)
DEFAULT_ZONES = ["+7", "+1", "+49", "+380"]

# Скрытые зоны: не показываются в меню, нельзя взять кнопкой.
# Доступны только через /num (админ забирает конкретный номер).
HIDDEN_ZONES = {"+888"}


def detect_zone(number_clean: str) -> str | None:
    """Определяет зону по справочнику стран."""
    if not number_clean.startswith("+"):
        return None
    digits = number_clean[1:]
    for length in (3, 2, 1):
        if len(digits) >= length:
            code = digits[:length]
            if code in COUNTRY_CODES:
                return "+" + code
    return None


async def guess_zone(session, number_clean: str) -> str | None:
    """Справочник → зоны в БД → эвристика."""
    if not number_clean.startswith("+"):
        return None

    z = detect_zone(number_clean)
    if z:
        return z

    digits = number_clean[1:]

    r = await session.execute(select(PhoneNumber.zone).group_by(PhoneNumber.zone))
    db_zones = [row[0] for row in r.all()]
    for zone in sorted(db_zones, key=lambda x: -len(x)):
        if number_clean.startswith(zone):
            return zone

    for length in (3, 2, 1):
        if len(digits) >= length:
            candidate = "+" + digits[:length]
            if candidate not in ("+00", "+0"):
                return candidate
    return None


def zone_flag(zone: str) -> str:
    code = zone.lstrip("+")
    if code in COUNTRY_CODES:
        return COUNTRY_CODES[code][0]
    return "🌐"


def normalize_number(raw: str) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    return "+" + digits


def generate_random_number(zone: str) -> str:
    code = zone.lstrip("+")
    if code in COUNTRY_CODES:
        _, total_digits, prefix_after = COUNTRY_CODES[code]
    else:
        total_digits, prefix_after = 11, ""

    digits_needed = total_digits - len(code) - len(prefix_after)
    if digits_needed < 1:
        digits_needed = 7

    first = random.choice("123456789")
    rest = "".join(random.choice("0123456789") for _ in range(digits_needed - 1))
    return f"+{code}{prefix_after}{first}{rest}"


async def generate_unique_number(session, zone: str, attempts: int = 50) -> str | None:
    for _ in range(attempts):
        num = generate_random_number(zone)
        r = await session.execute(select(PhoneNumber).where(PhoneNumber.number == num))
        if not r.scalar_one_or_none():
            return num
    return None


# ---------------- BOT ----------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def zone_keyboard():
    async with async_session() as s:
        r = await s.execute(
            select(PhoneNumber.zone, func.count(PhoneNumber.id))
            .where(PhoneNumber.is_busy == False)
            .group_by(PhoneNumber.zone)
        )
        free_by_zone = dict(r.all())
        r2 = await s.execute(select(PhoneNumber.zone).group_by(PhoneNumber.zone))
        all_zones = [row[0] for row in r2.all()]

    zones = sorted(set(DEFAULT_ZONES + all_zones) - HIDDEN_ZONES)

    rows, buf = [], []
    for z in zones:
        flag = zone_flag(z)
        free = free_by_zone.get(z, 0)
        label = f"{flag} {z}"
        if free > 0:
            label += f" ({free})"
        buf.append(InlineKeyboardButton(text=label, callback_data=f"zone_{z}"))
        if len(buf) == 3:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton(text="📱 Мои номера", callback_data="my_numbers")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats"),
         InlineKeyboardButton(text="🌍 Зоны", callback_data="adm_zones")],
        [InlineKeyboardButton(text="📋 Список номеров", callback_data="adm_list"),
         InlineKeyboardButton(text="👥 Юзеры", callback_data="adm_users")],
        [InlineKeyboardButton(text="➕ Генерация", callback_data="adm_gen_hint"),
         InlineKeyboardButton(text="❌ Удалить зону", callback_data="adm_delzone_hint")],
        [InlineKeyboardButton(text="📖 Помощь по командам", callback_data="adm_help")],
    ])


# ---------- User handlers ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    async with async_session() as s:
        u = await s.get(User, message.from_user.id)
        if not u:
            s.add(User(user_id=message.from_user.id, username=message.from_user.username))
        elif message.from_user.username and u.username != message.from_user.username:
            u.username = message.from_user.username
        await s.commit()

    await message.answer(
        f"Привет, {message.from_user.full_name}!\n"
        f"Выбери зону, чтобы получить номер.\n"
        f"Лимит: {USER_LIMIT} номера на пользователя.",
        reply_markup=await zone_keyboard(),
    )


@dp.message(Command("my"))
async def cmd_my(message: types.Message):
    await show_my_numbers(message.from_user.id, message)


async def show_my_numbers(user_id: int, target):
    async with async_session() as s:
        r = await s.execute(select(PhoneNumber).where(PhoneNumber.user_id == user_id))
        nums = r.scalars().all()
    if not nums:
        text = "У тебя пока нет номеров."
    else:
        text = "📱 *Твои номера:*\n" + "\n".join(
            f"• `{n.number}` (зона {n.zone})" for n in nums
        )
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode="Markdown")
    else:
        await target.answer(text, parse_mode="Markdown")


@dp.callback_query(F.data == "my_numbers")
async def cb_my(cb: CallbackQuery):
    await show_my_numbers(cb.from_user.id, cb)
    await cb.answer()


@dp.callback_query(F.data.startswith("zone_"))
async def cb_zone(cb: CallbackQuery):
    zone = cb.data.split("_", 1)[1]
    user_id = cb.from_user.id

    if zone in HIDDEN_ZONES:
        await cb.answer("Эта зона недоступна.", show_alert=True)
        return

    async with async_session() as s:
        if not is_admin(user_id):
            u = await s.get(User, user_id)
            if u and u.phone_count >= USER_LIMIT:
                await cb.answer(f"Лимит исчерпан ({USER_LIMIT} номера).", show_alert=True)
                return

        r = await s.execute(
            select(PhoneNumber)
            .where(PhoneNumber.zone == zone, PhoneNumber.is_busy == False)
            .limit(1)
        )
        phone = r.scalar_one_or_none()

        if not phone and AUTO_GEN_ON_DEMAND:
            num = await generate_unique_number(s, zone)
            if num:
                phone = PhoneNumber(number=num, zone=zone, is_busy=False)
                s.add(phone)
                await s.flush()

        if not phone:
            await cb.answer(f"Не удалось получить номер в зоне {zone}.", show_alert=True)
            return

        phone.is_busy = True
        phone.user_id = user_id
        phone.bound_at = datetime.utcnow()

        if not is_admin(user_id):
            u = await s.get(User, user_id)
            if u:
                u.phone_count += 1
            else:
                s.add(User(user_id=user_id, phone_count=1, username=cb.from_user.username))

        await s.commit()

    await cb.message.answer(
        f"✅ Твой номер: `{phone.number}`\n"
        f"Зона: {zone}\n\n"
        f"Открой клиент Telegram и запроси код — он придёт сюда.",
        parse_mode="Markdown",
    )
    await cb.answer()


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 *Пользовательские команды:*\n"
        "/start — меню зон\n"
        "/my — мои номера\n"
        "/help — эта справка\n"
    )
    if is_admin(message.from_user.id):
        text += "\n👑 *Админ:* `/admin` — меню, `/adminhelp` — справка."
    await message.answer(text, parse_mode="Markdown")


# ---------- ADMIN ----------
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "👑 *Админ-панель*\n\nВыбери действие или используй команды из /adminhelp",
        parse_mode="Markdown",
        reply_markup=admin_keyboard(),
    )


ADMINHELP_TEXT = (
    "👑 *Админ-команды:*\n\n"
    "*Номера:*\n"
    "`/num <номер>` — добавить + закрепить одной командой\n"
    "`/take <номер>` — привязать существующий номер к себе\n"
    "`/add <зона> <номер1> [номер2 …]` — добавить вручную\n"
    "`/addzone <номер>` — автоопределение зоны\n"
    "`/addzone <зона> <номер>` — явно указать зону\n"
    "`/gen <зона> <кол-во>` — сгенерировать случайные\n"
    "`/del <номер>` — удалить номер\n"
    "`/free <номер>` — освободить (без сброса счётчика)\n"
    "`/unbind <номер>` — отвязать + уменьшить счётчик юзера\n\n"
    "*Зоны:*\n"
    "`/zones` — список всех зон\n"
    "`/delzone <зона>` — удалить ВСЕ номера в зоне\n\n"
    "*Юзеры:*\n"
    "`/users` — список юзеров\n"
    "`/user <id>` — инфо о юзере\n"
    "`/reset <id>` — сбросить счётчик юзера\n\n"
    "*Прочее:*\n"
    "`/stats` — статистика по зонам\n"
    "`/list [зона]` — список номеров с юзерами\n"
)


async def send_adminhelp(target):
    if isinstance(target, CallbackQuery):
        await target.message.answer(ADMINHELP_TEXT, parse_mode="Markdown")
    else:
        await target.answer(ADMINHELP_TEXT, parse_mode="Markdown")


@dp.message(Command("adminhelp"))
async def cmd_adminhelp(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await send_adminhelp(message)


async def send_stats(target):
    async with async_session() as s:
        r = await s.execute(
            select(
                PhoneNumber.zone,
                func.count(PhoneNumber.id),
                func.sum(func.cast(PhoneNumber.is_busy, Integer)),
            ).group_by(PhoneNumber.zone).order_by(PhoneNumber.zone)
        )
        rows = r.all()
        r2 = await s.execute(select(func.count(PhoneNumber.id)).where(PhoneNumber.is_busy == True))
        total_busy = r2.scalar() or 0

    if not rows:
        text = "База пуста."
    else:
        text = f"📊 *Статистика* (всего занято: {total_busy})\n\n"
        for zone, total, busy in rows:
            busy = busy or 0
            free = total - busy
            text += f"• `{zone}` {zone_flag(zone)} — {free} своб. / {busy} занято (всего {total})\n"

    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode="Markdown")
    else:
        await target.answer(text, parse_mode="Markdown")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await send_stats(message)


# ---------- Admin: /add ----------
@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "Использование:\n"
            "`/add <зона> <номер1> [номер2 …]`\n"
            "Пример: `/add +7 +79991234567 +79997654321`",
            parse_mode="Markdown",
        )
        return
    zone = args[1].strip()
    if not re.match(r"^\+\d{1,4}$", zone):
        await message.answer("Зона в формате +7, +1, +49, +380, +888.")
        return

    added, skipped = 0, 0
    async with async_session() as s:
        for raw in args[2].split():
            num = normalize_number(raw)
            if not num or len(num) < 6:
                skipped += 1
                continue
            exists = await s.execute(select(PhoneNumber).where(PhoneNumber.number == num))
            if exists.scalar_one_or_none():
                skipped += 1
                continue
            s.add(PhoneNumber(number=num, zone=zone, is_busy=False))
            added += 1
        await s.commit()
    await message.answer(f"✅ Добавлено: {added}\n⚠️ Пропущено: {skipped}")


# ---------- Admin: /addzone ----------
@dp.message(Command("addzone"))
async def cmd_addzone(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=2)

    if len(args) == 2:
        num = normalize_number(args[1])
        if not num or len(num) < 6:
            await message.answer("Неверный формат номера.")
            return
        async with async_session() as s:
            zone = await guess_zone(s, num)
        if not zone:
            await message.answer(
                "Не смог определить зону.\n"
                "Укажи явно: `/addzone <зона> <номер>`",
                parse_mode="Markdown",
            )
            return
    elif len(args) >= 3:
        zone = args[1].strip()
        if not re.match(r"^\+\d{1,4}$", zone):
            await message.answer("Зона в формате +7, +880, +888.")
            return
        num = normalize_number(args[2])
        if not num:
            await message.answer("Неверный формат номера.")
            return
    else:
        await message.answer(
            "Использование:\n"
            "`/addzone <номер>` — автоопределение\n"
            "`/addzone <зона> <номер>` — явно",
            parse_mode="Markdown",
        )
        return

    async with async_session() as s:
        exists = await s.execute(select(PhoneNumber).where(PhoneNumber.number == num))
        if exists.scalar_one_or_none():
            await message.answer(f"Номер `{num}` уже есть.", parse_mode="Markdown")
            return
        s.add(PhoneNumber(number=num, zone=zone, is_busy=False))
        await s.commit()
    await message.answer(f"✅ Добавлено: `{num}` в зоне `{zone}`", parse_mode="Markdown")


# ---------- Admin: /gen ----------
@dp.message(Command("gen"))
async def cmd_gen(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: `/gen <зона> <кол-во>`", parse_mode="Markdown")
        return
    zone = args[1]
    if not re.match(r"^\+\d{1,4}$", zone):
        await message.answer("Зона в формате +7, +880.")
        return
    try:
        count = int(args[2])
    except ValueError:
        await message.answer("Кол-во — число.")
        return
    if count < 1 or count > 1000:
        await message.answer("От 1 до 1000.")
        return

    added = 0
    async with async_session() as s:
        for _ in range(count):
            num = await generate_unique_number(s, zone)
            if not num:
                break
            s.add(PhoneNumber(number=num, zone=zone, is_busy=False))
            await s.flush()
            added += 1
        await s.commit()
    await message.answer(f"✅ Сгенерировано: {added} в зоне `{zone}`", parse_mode="Markdown")


# ---------- Admin: /del ----------
@dp.message(Command("del"))
async def cmd_del(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/del <номер>`", parse_mode="Markdown")
        return
    num = normalize_number(args[1])
    async with async_session() as s:
        r = await s.execute(select(PhoneNumber).where(PhoneNumber.number == num))
        p = r.scalar_one_or_none()
        if not p:
            await message.answer("Не найден.")
            return
        await s.delete(p)
        await s.commit()
    await message.answer(f"🗑 Удалён: `{num}`", parse_mode="Markdown")


# ---------- Admin: /free ----------
@dp.message(Command("free"))
async def cmd_free(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/free <номер>`", parse_mode="Markdown")
        return
    num = normalize_number(args[1])
    async with async_session() as s:
        r = await s.execute(select(PhoneNumber).where(PhoneNumber.number == num))
        p = r.scalar_one_or_none()
        if not p:
            await message.answer("Не найден.")
            return
        p.is_busy = False
        p.user_id = None
        p.bound_at = None
        await s.commit()
    await message.answer(f"🆓 Освобождён: `{num}`", parse_mode="Markdown")


# ---------- Admin: /unbind ----------
@dp.message(Command("unbind"))
async def cmd_unbind(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/unbind <номер>`", parse_mode="Markdown")
        return
    num = normalize_number(args[1])
    async with async_session() as s:
        r = await s.execute(select(PhoneNumber).where(PhoneNumber.number == num))
        p = r.scalar_one_or_none()
        if not p:
            await message.answer("Не найден.")
            return
        old_user = p.user_id
        p.is_busy = False
        p.user_id = None
        p.bound_at = None
        if old_user:
            u = await s.get(User, old_user)
            if u and u.phone_count > 0:
                u.phone_count -= 1
        await s.commit()
    await message.answer(
        f"🔓 `{num}` отвязан от юзера {old_user}. Счётчик юзера уменьшен.",
        parse_mode="Markdown",
    )


# ---------- Admin: /num ----------
@dp.message(Command("num"))
async def cmd_num(message: types.Message):
    """Добавить номер (если нет) + привязать к админу."""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Использование: `/num <номер>`\n"
            "Пример: `/num +888111111111`",
            parse_mode="Markdown",
        )
        return

    num = normalize_number(args[1])
    if not num or len(num) < 6:
        await message.answer("Неверный формат номера.")
        return

    async with async_session() as s:
        zone = await guess_zone(s, num)
        if not zone:
            await message.answer(
                f"Не смог определить зону для `{num}`.",
                parse_mode="Markdown",
            )
            return

        r = await s.execute(select(PhoneNumber).where(PhoneNumber.number == num))
        p = r.scalar_one_or_none()

        if p is None:
            p = PhoneNumber(
                number=num, zone=zone,
                is_busy=True, user_id=message.from_user.id,
                bound_at=datetime.utcnow(),
            )
            s.add(p)
            action = "добавлен и закреплён"
        elif p.is_busy and p.user_id != message.from_user.id:
            await message.answer(
                f"⚠️ `{num}` уже занят юзером `{p.user_id}`.\n"
                f"Сначала: `/unbind {num}`, потом `/num {num}`",
                parse_mode="Markdown",
            )
            return
        else:
            p.is_busy = True
            p.user_id = message.from_user.id
            p.bound_at = datetime.utcnow()
            action = "закреплён за тобой"

        await s.commit()

    await message.answer(
        f"✅ Номер `{num}` ({zone}) — {action}.",
        parse_mode="Markdown",
    )


# ---------- Admin: /take ----------
@dp.message(Command("take"))
async def cmd_take(message: types.Message):
    """Привязать существующий номер к себе (без добавления)."""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/take <номер>`", parse_mode="Markdown")
        return
    num = normalize_number(args[1])
    if not num:
        await message.answer("Неверный формат.")
        return

    async with async_session() as s:
        r = await s.execute(select(PhoneNumber).where(PhoneNumber.number == num))
        p = r.scalar_one_or_none()
        if not p:
            await message.answer(
                f"Номер `{num}` не найден. Используй `/num {num}` — добавит и закрепит.",
                parse_mode="Markdown",
            )
            return
        if p.is_busy and p.user_id != message.from_user.id:
            await message.answer(
                f"Номер `{num}` уже занят юзером `{p.user_id}`.",
                parse_mode="Markdown",
            )
            return
        p.is_busy = True
        p.user_id = message.from_user.id
        p.bound_at = datetime.utcnow()
        await s.commit()
    await message.answer(f"✅ Номер `{num}` закреплён за тобой.", parse_mode="Markdown")


# ---------- Admin: /zones ----------
async def send_zones(target):
    async with async_session() as s:
        r = await s.execute(
            select(
                PhoneNumber.zone,
                func.count(PhoneNumber.id),
                func.sum(func.cast(PhoneNumber.is_busy, Integer)),
            ).group_by(PhoneNumber.zone).order_by(PhoneNumber.zone)
        )
        rows = r.all()
    if not rows:
        text = "Зон в БД нет."
    else:
        text = "🌍 *Зоны в БД:*\n\n"
        for zone, total, busy in rows:
            busy = busy or 0
            mark = " 🔒" if zone in HIDDEN_ZONES else ""
            text += f"• `{zone}`{mark} {zone_flag(zone)} — {total - busy} своб. / {busy} занято\n"
        text += "\n🔒 — скрытая зона\nЧтобы удалить: `/delzone <зона>`"
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode="Markdown")
    else:
        await target.answer(text, parse_mode="Markdown")


@dp.message(Command("zones"))
async def cmd_zones(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await send_zones(message)


# ---------- Admin: /delzone ----------
@dp.message(Command("delzone"))
async def cmd_delzone(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/delzone <зона>`", parse_mode="Markdown")
        return
    zone = args[1].strip()
    if not re.match(r"^\+\d{1,4}$", zone):
        await message.answer("Зона в формате +7, +380.")
        return

    async with async_session() as s:
        r = await s.execute(select(func.count(PhoneNumber.id)).where(PhoneNumber.zone == zone))
        count = r.scalar() or 0
        if count == 0:
            await message.answer(f"В зоне `{zone}` нет номеров.", parse_mode="Markdown")
            return
        r2 = await s.execute(
            select(func.count(PhoneNumber.id))
            .where(PhoneNumber.zone == zone, PhoneNumber.is_busy == True)
        )
        busy = r2.scalar() or 0

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ Удалить {count}", callback_data=f"delzone_yes_{zone}")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="delzone_no")],
    ])
    await message.answer(
        f"⚠️ *Удалить ВСЕ номера в зоне `{zone}`?*\n\n"
        f"Всего: {count}, занято: {busy}.",
        parse_mode="Markdown",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("delzone_yes_"))
async def cb_delzone_yes(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Не админ.", show_alert=True)
        return
    zone = cb.data[len("delzone_yes_"):]
    async with async_session() as s:
        r = await s.execute(
            select(PhoneNumber.user_id, func.count(PhoneNumber.id))
            .where(PhoneNumber.zone == zone, PhoneNumber.user_id.isnot(None))
            .group_by(PhoneNumber.user_id)
        )
        per_user = r.all()
        for uid, cnt in per_user:
            u = await s.get(User, uid)
            if u and u.phone_count > 0:
                u.phone_count = max(0, u.phone_count - cnt)

        r2 = await s.execute(delete(PhoneNumber).where(PhoneNumber.zone == zone))
        deleted = r2.rowcount or 0
        await s.commit()

    await cb.message.edit_text(f"🗑 Удалено {deleted} номеров в зоне `{zone}`.", parse_mode="Markdown")
    await cb.answer()


@dp.callback_query(F.data == "delzone_no")
async def cb_delzone_no(cb: CallbackQuery):
    await cb.message.edit_text("Отменено.")
    await cb.answer()


# ---------- Admin: /list ----------
async def send_list(target, zone: str | None = None):
    async with async_session() as s:
        stmt = select(PhoneNumber).order_by(PhoneNumber.zone, PhoneNumber.id)
        if zone:
            stmt = stmt.where(PhoneNumber.zone == zone)
        stmt = stmt.limit(100)
        r = await s.execute(stmt)
        rows = r.scalars().all()

    if not rows:
        text = "Пусто."
    else:
        text = f"📋 *Номера* (показано {len(rows)}):\n\n"
        for p in rows:
            mark = "🔴" if p.is_busy else "🟢"
            text += f"{mark} `{p.number}` — "
            text += f"юзер {p.user_id}" if p.user_id else "свободен"
            text += "\n"
        if len(text) > 4000:
            text = text[:4000] + "\n… (обрезано)"

    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode="Markdown")
    else:
        await target.answer(text, parse_mode="Markdown")


@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    zone = args[1].strip() if len(args) > 1 else None
    await send_list(message, zone)


# ---------- Admin: /users ----------
async def send_users(target):
    async with async_session() as s:
        r = await s.execute(select(User).order_by(User.first_seen.desc()).limit(50))
        rows = r.scalars().all()
    if not rows:
        text = "Юзеров нет."
    else:
        text = "👥 *Юзеры* (до 50):\n\n"
        for u in rows:
            uname = f"@{u.username}" if u.username else "—"
            text += f"• `{u.user_id}` {uname} — {u.phone_count} номеров\n"
        if len(text) > 4000:
            text = text[:4000] + "\n… (обрезано)"
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode="Markdown")
    else:
        await target.answer(text, parse_mode="Markdown")


@dp.message(Command("users"))
async def cmd_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await send_users(message)


# ---------- Admin: /user ----------
@dp.message(Command("user"))
async def cmd_user(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer("Использование: `/user <id>`", parse_mode="Markdown")
        return
    uid = int(args[1].strip())
    async with async_session() as s:
        u = await s.get(User, uid)
        r = await s.execute(select(PhoneNumber).where(PhoneNumber.user_id == uid))
        nums = r.scalars().all()

    if not u and not nums:
        await message.answer("Не найден.")
        return

    text = f"👤 *Юзер* `{uid}`\n"
    if u:
        text += f"Username: @{u.username or '—'}\n"
        text += f"Счётчик: {u.phone_count}\n"
        text += f"Первый заход: {u.first_seen}\n"
    text += f"\n📱 Номера ({len(nums)}):\n"
    for n in nums:
        text += f"• `{n.number}`\n"
    if not nums:
        text += "— нет"
    await message.answer(text, parse_mode="Markdown")


# ---------- Admin: /reset ----------
@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.answer("Использование: `/reset <id>`", parse_mode="Markdown")
        return
    uid = int(args[1].strip())
    async with async_session() as s:
        u = await s.get(User, uid)
        if not u:
            await message.answer("Юзер не найден.")
            return
        u.phone_count = 0
        await s.commit()
    await message.answer(f"♻️ Счётчик юзера `{uid}` сброшен.", parse_mode="Markdown")


# ---------- Admin inline callbacks ----------
@dp.callback_query(F.data == "adm_stats")
async def adm_stats_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Не админ.", show_alert=True)
        return
    await send_stats(cb)
    await cb.answer()


@dp.callback_query(F.data == "adm_zones")
async def adm_zones_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Не админ.", show_alert=True)
        return
    await send_zones(cb)
    await cb.answer()


@dp.callback_query(F.data == "adm_list")
async def adm_list_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Не админ.", show_alert=True)
        return
    await send_list(cb)
    await cb.answer()


@dp.callback_query(F.data == "adm_users")
async def adm_users_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Не админ.", show_alert=True)
        return
    await send_users(cb)
    await cb.answer()


@dp.callback_query(F.data == "adm_gen_hint")
async def adm_gen_hint_cb(cb: CallbackQuery):
    await cb.message.answer(
        "Для генерации введи:\n`/gen <зона> <кол-во>`\nНапример: `/gen +380 50`",
        parse_mode="Markdown",
    )
    await cb.answer()


@dp.callback_query(F.data == "adm_delzone_hint")
async def adm_delzone_hint_cb(cb: CallbackQuery):
    await cb.message.answer(
        "Чтобы удалить зону:\n`/delzone <зона>`\nНапример: `/delzone +88`",
        parse_mode="Markdown",
    )
    await cb.answer()


@dp.callback_query(F.data == "adm_help")
async def adm_help_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Не админ.", show_alert=True)
        return
    await send_adminhelp(cb)
    await cb.answer()


# ---------------- OTP DELIVERY ----------------
async def send_otp_to_user(recipient: str, code: str) -> bool:
    normalized = normalize_number(recipient)
    log.info(f"OTP lookup: raw={recipient} normalized={normalized} code={code}")

    async with async_session() as s:
        r = await s.execute(
            select(PhoneNumber).where(
                or_(PhoneNumber.number == normalized, PhoneNumber.number == recipient)
            )
        )
        phone = r.scalar_one_or_none()
        if not phone or not phone.user_id:
            log.warning(f"No bound user for number {recipient}")
            return False
        try:
            await bot.send_message(
                phone.user_id,
                f"🔑 Код подтверждения для `{phone.number}`:\n\n`{code}`",
                parse_mode="Markdown",
            )
            return True
        except Exception as e:
            log.error(f"Failed to send OTP to {phone.user_id}: {e}")
            return False


async def handle_otp(request: web.Request) -> web.Response:
    body = await request.read()

    if OTP_WEBHOOK_SECRET:
        sig = request.headers.get("X-Telesrv-Signature", "")
        expected = hmac.new(OTP_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, f"sha256={expected}"):
            log.warning("Invalid webhook signature")
            return web.json_response(
                {"accepted": False, "error_code": "INVALID_SIGNATURE"}, status=401
            )

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return web.json_response(
            {"accepted": False, "error_code": "INVALID_JSON"}, status=400
        )

    log.info(f"Webhook payload: {data}")

    code = data.get("code")
    recipient = data.get("recipient")
    if not code or not recipient:
        return web.json_response(
            {"accepted": False, "error_code": "MISSING_FIELDS"}, status=400
        )

    ok = await send_otp_to_user(recipient, code)
    return web.json_response({"accepted": ok, "message_id": "tg-bot"})


# ---------------- MAIN ----------------
async def main():
    await init_db()

    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_otp)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    log.info(f"Webhook: http://{WEBHOOK_HOST}:{WEBHOOK_PORT}{WEBHOOK_PATH}")

    log.info("Bot polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())