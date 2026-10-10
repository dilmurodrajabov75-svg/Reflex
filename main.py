"""
Ish e'lonlari boti v4 (aiogram 3.x)
- Bosh admin + qo'shimcha adminlar (bot orqali qo'shiladi)
- Har bir admin o'z kartalarini saqlaydi va e'londa tanlaydi
- Foydalanuvchi balansi (to'ldirish, ishga yozilganda avtomatik yechish)
- Admin bilan bog'lanish, foydalanuvchini qidirish va anketasini ko'rish
requirements.txt:  aiogram
"""
import asyncio
import html
import logging
import re
import sqlite3
import time

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandObject, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InputMediaPhoto, KeyboardButton,
                           Message, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import os

BOT_TOKEN = "8350987756:AAFOms_5ccVJJ873nK7FUwS8xpEyjq5DLkk"
ADMIN_ID = 6996333181          # BOSH ADMIN
CHANNEL_ID = "@reflex_top"

PRICES = [100000, 150000, 180000, 200000, 250000, 300000, 400000]
BOT_USERNAME = ""

# ---------------------------------------------------------------- DB
db = sqlite3.connect("bot.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, surname TEXT, phone TEXT,
  address TEXT, age INTEGER, photo TEXT, banned INTEGER DEFAULT 0,
  username TEXT, balance INTEGER DEFAULT 0, video TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS ads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, kind TEXT,
  price INTEGER, time TEXT, when_ TEXT, lat REAL, lon REAL, extra TEXT,
  status TEXT DEFAULT 'active', msg_id INTEGER, card TEXT DEFAULT '',
  workers INTEGER DEFAULT 1, address TEXT DEFAULT '', phone TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS apps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ad_id INTEGER, user_id INTEGER,
  shot1 TEXT, shot2 TEXT, receipt TEXT, status TEXT DEFAULT 'new',
  paid_by TEXT DEFAULT '', created INTEGER DEFAULT 0, paid_ts INTEGER DEFAULT 0,
  paid_amount INTEGER DEFAULT 0, confirmed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ratings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, app_id INTEGER, role TEXT,
  score INTEGER, ts INTEGER, UNIQUE(app_id, role));
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS admin_cards(
  id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, card TEXT);
CREATE TABLE IF NOT EXISTS topups(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
  status TEXT DEFAULT 'new');
INSERT OR IGNORE INTO settings VALUES('card','Karta raqami kiritilmagan');
INSERT OR IGNORE INTO settings VALUES('fee','10000');
""")
db.commit()
for _stmt in ("ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0",
              "ALTER TABLE ads ADD COLUMN card TEXT DEFAULT ''",
              "ALTER TABLE apps ADD COLUMN paid_by TEXT DEFAULT ''",
              "ALTER TABLE ads ADD COLUMN workers INTEGER DEFAULT 1",
              "ALTER TABLE ads ADD COLUMN address TEXT DEFAULT ''",
              "ALTER TABLE ads ADD COLUMN phone TEXT DEFAULT ''",
              "ALTER TABLE users ADD COLUMN video TEXT DEFAULT ''",
              "ALTER TABLE apps ADD COLUMN created INTEGER DEFAULT 0",
              "ALTER TABLE apps ADD COLUMN paid_ts INTEGER DEFAULT 0",
              "ALTER TABLE apps ADD COLUMN paid_amount INTEGER DEFAULT 0",
              "ALTER TABLE apps ADD COLUMN confirmed INTEGER DEFAULT 0"):
    try:
        db.execute(_stmt)
        db.commit()
    except sqlite3.OperationalError:
        pass


def q(sql, args=(), one=False, commit=False):
    cur = db.execute(sql, args)
    if commit:
        db.commit()
        return cur.lastrowid
    return cur.fetchone() if one else cur.fetchall()


def setting(k):
    return q("SELECT v FROM settings WHERE k=?", (k,), one=True)["v"]


def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def esc(s):
    return html.escape(str(s))


def money(n):
    return f"{int(n):,}".replace(",", " ")


def fee_int():
    d = re.sub(r"\D", "", setting("fee"))
    return int(d) if d else 0


def is_main(uid):
    return uid == ADMIN_ID


def is_admin(uid):
    return uid == ADMIN_ID or q("SELECT 1 FROM admins WHERE id=?", (uid,), one=True) is not None


def handler_for(ad):
    """Ariza va cheklar kimga boradi: e'lon egasi admin bo'lsa o'zi, aks holda bosh admin."""
    return ad["owner"] if is_admin(ad["owner"]) else ADMIN_ID


def can_manage(uid, ad):
    return uid == ADMIN_ID or uid == handler_for(ad)


def admin_ids():
    return [ADMIN_ID] + [r["id"] for r in q("SELECT id FROM admins")]


def hired_count(ad_id):
    return q("SELECT COUNT(*) FROM apps WHERE ad_id=? AND status='paid'", (ad_id,), one=True)[0]


def card_for(ad):
    """Admin e'lonida o'sha adminning tanlagan kartasi, boshqa hamma e'londa BOSH ADMIN kartasi."""
    if is_admin(ad["owner"]) and ad["card"]:
        return ad["card"]
    return setting("card")


def mark_paid(app_id, how, amount):
    q("UPDATE apps SET status='paid', paid_by=?, paid_ts=?, paid_amount=? WHERE id=?",
      (how, int(time.time()), amount, app_id), commit=True)


async def send_profile(bot, chat_id, u, caption=None, reply_markup=None):
    """Foydalanuvchi anketasi: dumaloq video yoki (eski foydalanuvchilar uchun) rasm."""
    if u["video"]:
        await bot.send_video_note(chat_id, u["video"])
        if caption:
            await bot.send_message(chat_id, caption, reply_markup=reply_markup)
    elif u["photo"]:
        await bot.send_photo(chat_id, u["photo"], caption=caption, reply_markup=reply_markup)
    elif caption:
        await bot.send_message(chat_id, caption, reply_markup=reply_markup)


def rating_text():
    r = q("SELECT COUNT(*) AS c, COALESCE(AVG(score),0) AS a FROM ratings", one=True)
    t = "⭐ <b>Xizmatimiz bahosi</b>\n\n"
    if r["c"] == 0:
        return t + "Hali baholar yo'q. Birinchi bo'lib baholang!"
    t += f"Umumiy o'rtacha: <b>{r['a']:.1f} / 5</b> ({r['c']} ta baho)\n\n"
    for i in range(5, 0, -1):
        n = q("SELECT COUNT(*) FROM ratings WHERE score=?", (i,), one=True)[0]
        bar = "█" * round(10 * n / r["c"])
        t += f"{i}⭐ {bar} {n}\n"
    return t


def rate_kb(app_id, role):
    return inline([(f"{i}⭐", f"rate:{app_id}:{role}:{i}") for i in range(1, 6)], 5)


async def after_slot_freed(bot, ad_id):
    """Ishchi bekor bo'lsa joy bo'shaydi: to'lgan e'lon yana ochiladi."""
    ad = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    if ad and ad["status"] == "full" and hired_count(ad_id) < (ad["workers"] or 1):
        q("UPDATE ads SET status='active' WHERE id=?", (ad_id,), commit=True)
    await refresh_post(bot, ad_id)


class IsAdmin(Filter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and is_admin(u.id))


ADM = IsAdmin()
MAIN = F.from_user.id == ADMIN_ID


# ---------------------------------------------------------------- States
class Reg(StatesGroup):
    name = State(); surname = State(); phone = State()
    address = State(); age = State(); photo = State()


class Ad(StatesGroup):
    kind = State(); price = State(); count = State(); time = State(); when = State()
    location = State(); address = State(); extra = State(); phone = State(); card = State(); confirm = State()


class Apply(StatesGroup):
    shot1 = State(); shot2 = State()


class Topup(StatesGroup):
    amount = State(); receipt = State()


class Contact(StatesGroup):
    msg = State()


class Adm(StatesGroup):
    setting = State(); broadcast = State(); ban = State(); unban = State()
    find = State(); bal = State(); addadmin = State(); addcard = State(); reply = State()


router = Router()
pending_apply = {}


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and u.id != ADMIN_ID:
            row = get_user(u.id)
            if row and row["banned"]:
                return
        return await handler(event, data)


# ---------------------------------------------------------------- Klaviaturalar
def main_menu(uid):
    b = ReplyKeyboardBuilder()
    for t in ("📢 E'lon berish", "📋 E'lonlarim", "📝 Arizalarim", "💰 Hisobim",
              "⭐ Baholar", "📞 Admin bilan bog'lanish", "👤 Profilim"):
        b.button(text=t)
    if is_admin(uid):
        b.button(text="🛠 Admin panel")
    b.adjust(2, 2, 2, 2)
    return b.as_markup(resize_keyboard=True)


def cancel_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="❌ Bekor qilish")
    return b.as_markup(resize_keyboard=True)


def inline(rows, width=2):
    b = InlineKeyboardBuilder()
    for text, data in rows:
        b.button(text=text, callback_data=data)
    b.adjust(width)
    return b.as_markup()


# ---------------------------------------------------------------- START / RO'YXAT
@router.message(F.text == "❌ Bekor qilish")
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=main_menu(m.from_user.id))


@router.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("apply_") and arg[6:].isdigit():
        pending_apply[m.from_user.id] = int(arg[6:])
    if not get_user(m.from_user.id):
        await m.answer("Assalomu alaykum! Avval ro'yxatdan o'ting.\n\n✏️ Ismingizni yozing:",
                       reply_markup=ReplyKeyboardRemove())
        await state.set_state(Reg.name)
        return
    if m.from_user.id in pending_apply:
        await begin_apply(m, state, pending_apply.pop(m.from_user.id))
        return
    await m.answer("Asosiy menyu:", reply_markup=main_menu(m.from_user.id))


@router.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await m.answer("Familiyangizni yozing:")
    await state.set_state(Reg.surname)


@router.message(Reg.surname, F.text)
async def reg_surname(m: Message, state: FSMContext):
    await state.update_data(surname=m.text.strip())
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="📞 Raqamni yuborish", request_contact=True))
    await m.answer("Telefon raqamingizni tugma orqali yuboring:",
                   reply_markup=b.as_markup(resize_keyboard=True))
    await state.set_state(Reg.phone)


@router.message(Reg.phone, F.contact)
async def reg_phone(m: Message, state: FSMContext):
    await state.update_data(phone=m.contact.phone_number)
    await m.answer("Yashash manzilingiz (shahar/tuman):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Reg.address)


@router.message(Reg.address, F.text)
async def reg_address(m: Message, state: FSMContext):
    await state.update_data(address=m.text.strip())
    await m.answer("Yoshingiz (raqam bilan):")
    await state.set_state(Reg.age)


VIDEO_HELP = ("🎥 <b>Jonli selfi</b>\n\nOld kamera bilan o'zingizni <b>dumaloq video</b> qilib yuboring:\n"
              "1️⃣ Yozish maydonidagi 🎤 tugmasini bir marta bosib, 🎥 (video) holatiga o'tkazing.\n"
              "2️⃣ Yuzingiz ko'ringan holda 2–3 soniya yozing va yuboring.\n\n"
              "Galereyadan yuklab yoki boshqadan uzatib bo'lmaydi.")


@router.message(Reg.age, F.text)
async def reg_age(m: Message, state: FSMContext):
    if not m.text.isdigit() or not 14 <= int(m.text) <= 80:
        return await m.answer("Yoshni to'g'ri kiriting (14–80).")
    await state.update_data(age=int(m.text))
    await m.answer(VIDEO_HELP)
    await state.set_state(Reg.photo)


@router.message(Reg.photo, F.video_note)
async def reg_video(m: Message, state: FSMContext, bot: Bot):
    if getattr(m, "forward_origin", None):
        return await m.answer("❌ Boshqadan uzatilgan video qabul qilinmaydi. O'zingiz yozib yuboring.")
    d = await state.get_data()
    vid = m.video_note.file_id
    q("INSERT OR REPLACE INTO users(id,name,surname,phone,address,age,photo,username,video) "
      "VALUES(?,?,?,?,?,?,?,?,?)",
      (m.from_user.id, d["name"], d["surname"], d["phone"], d["address"],
       d["age"], "", m.from_user.username or "", vid), commit=True)
    await state.clear()
    try:
        await bot.send_video_note(ADMIN_ID, vid)
        await bot.send_message(
            ADMIN_ID, f"🆕 <b>Yangi foydalanuvchi</b>\n\n👤 {esc(d['name'])} {esc(d['surname'])}\n"
                      f"📞 {esc(d['phone'])}\n🏠 {esc(d['address'])}\n🎂 {d['age']} yosh\n"
                      f"🆔 <code>{m.from_user.id}</code>\n🔗 @{esc(m.from_user.username or '—')}")
    except Exception as e:
        logging.warning("Adminga xabar yuborilmadi: %s", e)
    await m.answer("✅ Ro'yxatdan o'tdingiz!", reply_markup=main_menu(m.from_user.id))
    if m.from_user.id in pending_apply:
        await begin_apply(m, state, pending_apply.pop(m.from_user.id))


@router.message(Reg.photo)
async def reg_video_wrong(m: Message):
    await m.answer("❌ Rasm yoki oddiy video qabul qilinmaydi.\n\n" + VIDEO_HELP)


@router.message(F.text == "👤 Profilim")
async def profile(m: Message, bot: Bot):
    u = get_user(m.from_user.id)
    if not u:
        return await m.answer("Avval /start bosing.")
    await send_profile(bot, m.chat.id, u, caption=(
        f"👤 {esc(u['name'])} {esc(u['surname'])}\n📞 {esc(u['phone'])}\n"
        f"🏠 {esc(u['address'])}\n🎂 {u['age']} yosh\n💰 Balans: {money(u['balance'])} so'm"))


# ---------------------------------------------------------------- HISOB (BALANS)
@router.message(F.text == "💰 Hisobim")
async def my_balance(m: Message):
    u = get_user(m.from_user.id)
    if not u:
        return await m.answer("Avval /start bosing.")
    await m.answer(
        f"💰 Balansingiz: <b>{money(u['balance'])} so'm</b>\n\n"
        f"Ishga yozilganda xizmat haqi ({money(fee_int())} so'm) shu hisobdan avtomatik yechiladi. "
        "Balans yetmasa, to'lovni kartaga o'tkazib chek yuborasiz.",
        reply_markup=inline([("➕ Hisobni to'ldirish", "topup")], 1))


@router.callback_query(F.data == "topup")
async def topup_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Topup.amount)
    await c.message.answer("Qancha so'm to'ldirmoqchisiz? Summani raqamda yozing (masalan: 50000):",
                           reply_markup=cancel_kb())


@router.message(Topup.amount, F.text)
async def topup_amount(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text)
    if not digits or int(digits) < 1000:
        return await m.answer("Summani to'g'ri kiriting (kamida 1 000).")
    await state.update_data(amount=int(digits))
    await state.set_state(Topup.receipt)
    await m.answer(f"💳 Shu kartaga <b>{money(int(digits))} so'm</b> o'tkazing:\n"
                   f"<code>{esc(setting('card'))}</code>\n\n"
                   "To'lovdan keyin <b>chek rasmini</b> yuboring 📸")


@router.message(Topup.receipt, F.photo)
async def topup_receipt(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    tid = q("INSERT INTO topups(user_id,amount) VALUES(?,?)", (m.from_user.id, d["amount"]), commit=True)
    u = get_user(m.from_user.id)
    await bot.send_photo(
        ADMIN_ID, m.photo[-1].file_id,
        caption=(f"💰 <b>Balans to'ldirish #{tid}</b>\n👤 {esc(u['name'])} {esc(u['surname'])}\n"
                 f"🆔 <code>{u['id']}</code>\nSumma: <b>{money(d['amount'])} so'm</b>"),
        reply_markup=inline([("✅ Qo'shish", f"tp:ok:{tid}"), ("❌ Rad etish", f"tp:no:{tid}")]))
    await m.answer("⏳ Chek adminga yuborildi. Tasdiqlangach balansingiz to'ldiriladi.",
                   reply_markup=main_menu(m.from_user.id))


@router.callback_query(MAIN, F.data.startswith("tp:"))
async def topup_admin(c: CallbackQuery, bot: Bot):
    _, act, tid = c.data.split(":")
    t = q("SELECT * FROM topups WHERE id=?", (tid,), one=True)
    if not t or t["status"] != "new":
        return await c.answer("Allaqachon ko'rib chiqilgan.", show_alert=True)
    if act == "ok":
        q("UPDATE topups SET status='ok' WHERE id=?", (tid,), commit=True)
        q("UPDATE users SET balance=balance+? WHERE id=?", (t["amount"], t["user_id"]), commit=True)
        bal = get_user(t["user_id"])["balance"]
        await bot.send_message(t["user_id"], f"✅ Hisobingiz {money(t['amount'])} so'mga to'ldirildi.\n"
                                             f"💰 Balans: {money(bal)} so'm")
        await c.message.edit_caption(caption=c.message.html_caption + "\n\n✅ Qo'shildi")
    else:
        q("UPDATE topups SET status='no' WHERE id=?", (tid,), commit=True)
        await bot.send_message(t["user_id"], "❌ To'lov tasdiqlanmadi. Admin bilan bog'laning.")
        await c.message.edit_caption(caption=c.message.html_caption + "\n\n❌ Rad etildi")


# ---------------------------------------------------------------- ADMIN BILAN BOG'LANISH
@router.message(F.text == "📞 Admin bilan bog'lanish")
async def contact_start(m: Message, state: FSMContext):
    if not get_user(m.from_user.id):
        return await m.answer("Avval /start bosing.")
    await state.set_state(Contact.msg)
    await m.answer("Adminga xabaringizni yozing (matn, rasm yoki ovozli xabar):", reply_markup=cancel_kb())


@router.message(Contact.msg)
async def contact_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    u = get_user(m.from_user.id)
    await bot.send_message(
        ADMIN_ID, f"📩 <b>Xabar</b>: {esc(u['name'])} {esc(u['surname'])}\n"
                  f"🆔 <code>{u['id']}</code> | 📞 {esc(u['phone'])}",
        reply_markup=inline([("↩️ Javob berish", f"rp:{u['id']}"), ("🔎 Anketa", f"u:{u['id']}")]))
    await bot.copy_message(ADMIN_ID, m.chat.id, m.message_id)
    await m.answer("✅ Xabaringiz adminga yuborildi. Javobni shu yerda olasiz.",
                   reply_markup=main_menu(m.from_user.id))


@router.callback_query(MAIN, F.data.startswith("rp:"))
async def reply_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.reply)
    await state.update_data(uid=int(c.data[3:]))
    await c.message.answer("Javobingizni yozing:")


@router.message(MAIN, Adm.reply, F.text)
async def reply_send(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    try:
        await bot.send_message(d["uid"], "📩 <b>Admin javobi:</b>\n\n" + esc(m.text))
        await m.answer("✅ Yuborildi.")
    except Exception:
        await m.answer("Yuborib bo'lmadi.")


# ---------------------------------------------------------------- E'LON BERISH
@router.message(F.text == "📢 E'lon berish")
async def ad_start(m: Message, state: FSMContext):
    if not get_user(m.from_user.id):
        return await m.answer("Avval /start bosib ro'yxatdan o'ting.")
    await state.clear()
    await m.answer("E'lon yaratamiz.", reply_markup=cancel_kb())
    kb = inline([("☀️ Kunlik", "kind:Kunlik"), ("🗓 Oylik", "kind:Oylik")])
    await m.answer("💼 Ish turini tanlang:", reply_markup=kb)
    await state.set_state(Ad.kind)


@router.callback_query(Ad.kind, F.data.startswith("kind:"))
async def ad_kind(c: CallbackQuery, state: FSMContext):
    await state.update_data(kind=c.data[5:])
    kb = inline([(money(p), f"price:{p}") for p in PRICES], 3)
    await c.message.edit_text("💰 Narxni tanlang (so'm):", reply_markup=kb)
    await state.set_state(Ad.price)


@router.callback_query(Ad.price, F.data.startswith("price:"))
async def ad_price(c: CallbackQuery, state: FSMContext):
    await state.update_data(price=int(c.data[6:]))
    rows = [(str(i), f"cnt:{i}") for i in range(1, 16)]
    await c.message.edit_text("👥 Nechta odam kerak? Tugmani bosing yoki raqamni yozing:",
                              reply_markup=inline(rows, 5))
    await state.set_state(Ad.count)


async def ask_time(msg):
    await msg.answer("Ish vaqti uchun tugmani bosing 👇",
                     reply_markup=inline([("🕒 Ish vaqtini yozish", "time_go")], 1))


@router.callback_query(Ad.count, F.data.startswith("cnt:"))
async def ad_count_btn(c: CallbackQuery, state: FSMContext):
    n = int(c.data[4:])
    await state.update_data(workers=n)
    await c.message.edit_text(f"👥 Odam soni: <b>{n} ta</b> ✅")
    await ask_time(c.message)
    await state.set_state(Ad.time)


@router.message(Ad.count, F.text)
async def ad_count_text(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit() or not 1 <= int(t) <= 100:
        return await m.answer("Odam sonini 1 dan 100 gacha raqam bilan yozing yoki tugmani bosing.")
    await state.update_data(workers=int(t))
    await m.answer(f"👥 Odam soni: <b>{int(t)} ta</b> ✅")
    await ask_time(m)
    await state.set_state(Ad.time)


@router.callback_query(Ad.time, F.data == "time_go")
async def ad_time_btn(c: CallbackQuery):
    await c.message.edit_text("🕒 Ish vaqtini yozing (masalan: 09:00 – 18:00):")


@router.message(Ad.time, F.text)
async def ad_time(m: Message, state: FSMContext):
    await state.update_data(time=m.text.strip())
    kb = inline([("Bugunga", "when:Bugunga"), ("Hozirga", "when:Hozirga"), ("Ertaga", "when:Ertaga")], 3)
    await m.answer("📅 Ish qachonga?", reply_markup=kb)
    await state.set_state(Ad.when)


@router.callback_query(Ad.when, F.data.startswith("when:"))
async def ad_when(c: CallbackQuery, state: FSMContext):
    await state.update_data(when=c.data[5:])
    await c.message.delete()
    admin = is_admin(c.from_user.id)
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="📍 Geolokatsiya yuborish", request_location=True))
    if admin:
        b.button(text="✍️ Manzilni yozish")
    b.button(text="❌ Bekor qilish")
    b.adjust(1)
    text = ("📍 Geolokatsiya yuboring yoki «✍️ Manzilni yozish» ni bosing:" if admin
            else "📍 Ish manzilini geolokatsiya orqali yuboring:")
    await c.message.answer(text, reply_markup=b.as_markup(resize_keyboard=True))
    await state.set_state(Ad.location)


@router.message(Ad.location, F.location)
async def ad_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude, address="")
    await m.answer("ℹ️ Qo'shimcha ma'lumot yozing (ish haqida, talablar...):", reply_markup=cancel_kb())
    await state.set_state(Ad.extra)


@router.message(ADM, Ad.location, F.text == "✍️ Manzilni yozish")
async def ad_address_ask(m: Message, state: FSMContext):
    await m.answer("✍️ Manzilni yozing (masalan: Chilonzor, 9-kvartal, 12-uy):", reply_markup=cancel_kb())
    await state.set_state(Ad.address)


@router.message(ADM, Ad.address, F.text)
async def ad_address(m: Message, state: FSMContext):
    await state.update_data(lat=None, lon=None, address=m.text.strip()[:200])
    await m.answer("ℹ️ Qo'shimcha ma'lumot yozing (ish haqida, talablar...):", reply_markup=cancel_kb())
    await state.set_state(Ad.extra)


def ad_dict(ad):
    d = dict(ad)
    d["when"] = d.get("when_")
    return d


def loc_line(a):
    if a.get("lat") is not None and a.get("lon") is not None:
        url = "https://maps.google.com/?q=" + str(a["lat"]) + "," + str(a["lon"])
        line = "📍 <a href=\"" + url + "\">Xaritada ko'rish</a>"
        if a.get("address"):
            line += " — " + esc(a["address"])
        return line
    return "📍 Manzil: " + esc(a.get("address") or "—")


def ad_text(a):
    return ("📢 <b>YANGI ISH E'LONI</b>\n\n"
            f"💼 Ish: <b>{esc(a['kind'])}</b>\n"
            f"💰 Narxi: <b>{money(a['price'])} so'm</b>\n"
            f"👥 Kerakli odam: <b>{a.get('workers') or 1} ta</b>\n"
            f"🕒 Ish vaqti: {esc(a['time'])}\n"
            f"📅 Qachonga: <b>{esc(a['when'])}</b>\n"
            + loc_line(a) + "\n\n"
            f"ℹ️ {esc(a['extra'])}")


def post_text(ad):
    """Kanaldagi e'lon matni (holat bilan)."""
    need = ad["workers"] or 1
    hired = hired_count(ad["id"])
    t = ad_text(ad_dict(ad)) + f"\n\n👥 Olingan: <b>{hired}/{need}</b>"
    if ad["status"] in ("full", "closed"):
        if hired >= need:
            t += "\n✅ <b>Holat: odam olindi</b>"
        elif hired == 0:
            t += "\n❌ <b>Holat: odam olinmadi</b>"
        else:
            t += "\n⚠️ <b>Holat: odam to'liq olinmadi</b>"
    return t + f"\n🆔 #{ad['id']}"


def apply_kb(ad_id):
    b = InlineKeyboardBuilder()
    b.button(text="✍️ Ishga yozilish", url=f"https://t.me/{BOT_USERNAME}?start=apply_{ad_id}")
    return b.as_markup()


async def post_ad(bot, ad_id):
    ad = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    msg = await bot.send_message(CHANNEL_ID, post_text(ad), reply_markup=apply_kb(ad_id),
                                 disable_web_page_preview=True)
    q("UPDATE ads SET msg_id=? WHERE id=?", (msg.message_id, ad_id), commit=True)


async def refresh_post(bot, ad_id):
    """E'lon kanalda QOLADI, faqat holat/tugma yangilanadi."""
    ad = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    if not ad or not ad["msg_id"]:
        return
    kb = apply_kb(ad_id) if ad["status"] == "active" else None
    try:
        await bot.edit_message_text(post_text(ad), chat_id=CHANNEL_ID, message_id=ad["msg_id"],
                                    reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        logging.warning("Kanal e'loni yangilanmadi: %s", e)


async def check_fill(bot, ad_id):
    ad = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    if ad and ad["status"] == "active" and hired_count(ad_id) >= (ad["workers"] or 1):
        q("UPDATE ads SET status='full' WHERE id=?", (ad_id,), commit=True)
    await refresh_post(bot, ad_id)


async def show_preview(m, state):
    d = await state.get_data()
    kb = inline([("✅ Tasdiqlash", "ad:ok"), ("❌ Bekor qilish", "ad:no")])
    card_line = ""
    if is_admin(m.chat.id):
        card_line = (f"\n\n📞 Ish beruvchi raqami: <code>{esc(d.get('phone') or '—')}</code>"
                     f"\n💳 To'lov kartasi: <code>{esc(d.get('card') or setting('card'))}</code>")
    await m.answer("Tekshiring:\n\n" + ad_text(d) + card_line, reply_markup=kb,
                   disable_web_page_preview=True)
    await state.set_state(Ad.confirm)


async def ask_card(m, state, uid):
    rows = q("SELECT * FROM admin_cards WHERE admin_id=?", (uid,))
    buttons = [(r["card"][:30], f"ac:{r['id']}") for r in rows]
    buttons.append(("⭐ Standart karta", "ac:0"))
    await m.answer("💳 Ishga yozilganlar to'lov qiladigan kartani tanlang:", reply_markup=inline(buttons, 1))
    await state.set_state(Ad.card)


@router.message(Ad.extra, F.text)
async def ad_extra(m: Message, state: FSMContext):
    uid = m.from_user.id
    me = get_user(uid)
    await state.update_data(extra=m.text.strip(), card="", phone=me["phone"] if me else "")
    if not is_admin(uid):
        return await show_preview(m, state)
    # adminlar ish beruvchi raqamini kiritadi (ishchilarga admin raqami bormaydi)
    await m.answer("📞 <b>Ish beruvchining</b> telefon raqamini yozing.\n"
                   "Ish uchun to'lov qilgan ishchilarga shu raqam beriladi (admin raqami emas):",
                   reply_markup=cancel_kb())
    await state.set_state(Ad.phone)


@router.message(ADM, Ad.phone, F.text)
async def ad_phone(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text)
    if not 9 <= len(digits) <= 13:
        return await m.answer("Telefon raqamni to'g'ri yozing (masalan: +998 90 123 45 67).")
    await state.update_data(phone=m.text.strip()[:25])
    await ask_card(m, state, m.from_user.id)


@router.callback_query(Ad.card, F.data.startswith("ac:"))
async def ad_card(c: CallbackQuery, state: FSMContext):
    cid = int(c.data[3:])
    card = ""
    if cid:
        r = q("SELECT * FROM admin_cards WHERE id=? AND admin_id=?", (cid, c.from_user.id), one=True)
        card = r["card"] if r else ""
    await state.update_data(card=card)
    await c.message.delete()
    await show_preview(c.message, state)


@router.callback_query(Ad.confirm, F.data == "ad:no")
async def ad_no(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("❌ E'lon bekor qilindi.")
    await c.message.answer("Menyu:", reply_markup=main_menu(c.from_user.id))


@router.callback_query(Ad.confirm, F.data == "ad:ok")
async def ad_ok(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    uid = c.from_user.id
    admin = is_admin(uid)
    ad_id = q("INSERT INTO ads(owner,kind,price,time,when_,lat,lon,extra,card,workers,address,status,phone) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, d["kind"], d["price"], d["time"], d["when"], d.get("lat"), d.get("lon"), d["extra"],
               d.get("card", "") if admin else "", d.get("workers", 1), d.get("address", ""),
               "active" if admin else "pending", d.get("phone", "")), commit=True)
    await state.clear()
    if admin:
        try:
            await post_ad(bot, ad_id)
            await c.message.edit_text(f"✅ E'lon kanalga joylandi! (#{ad_id})")
        except Exception as e:
            logging.error("Kanalga joylanmadi: %s", e)
            q("UPDATE ads SET status='pending' WHERE id=?", (ad_id,), commit=True)
            await c.message.edit_text("❌ Kanalga joylab bo'lmadi. Bot kanalda administrator ekanini tekshiring.")
        await c.message.answer("Menyu:", reply_markup=main_menu(uid))
        return
    # oddiy foydalanuvchi: hamma adminga tasdiqlash so'rovi
    await c.message.edit_text("⏳ E'loningiz adminlar tekshiruviga yuborildi. Tasdiqlangach kanalga joylanadi.")
    await c.message.answer("Menyu:", reply_markup=main_menu(uid))
    u = get_user(uid)
    ad = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    text = (f"📝 <b>Tasdiq kutayotgan e'lon #{ad_id}</b>\n👤 {esc(u['name'])} {esc(u['surname'])} | "
            f"📞 {esc(u['phone'])} | <code>{uid}</code>\n\n" + ad_text(ad_dict(ad)))
    kb = inline([("✅ Tasdiqlash", f"adv:ok:{ad_id}"), ("❌ Rad etish", f"adv:no:{ad_id}")])
    for aid in admin_ids():
        try:
            await bot.send_message(aid, text, reply_markup=kb, disable_web_page_preview=True)
        except Exception as e:
            logging.warning("Adminga e'lon yuborilmadi %s: %s", aid, e)


@router.callback_query(ADM, F.data.startswith("adv:"))
async def ad_review(c: CallbackQuery, bot: Bot):
    _, act, ad_id = c.data.split(":")
    ad = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    if not ad or ad["status"] != "pending":
        return await c.answer("Allaqachon ko'rib chiqilgan.", show_alert=True)
    me = get_user(c.from_user.id)
    who = esc(me["name"]) if me else str(c.from_user.id)
    if act == "ok":
        q("UPDATE ads SET status='active' WHERE id=?", (ad_id,), commit=True)
        try:
            await post_ad(bot, int(ad_id))
        except Exception as e:
            logging.error("Kanalga joylanmadi: %s", e)
            q("UPDATE ads SET status='pending' WHERE id=?", (ad_id,), commit=True)
            return await c.answer("Kanalga joylab bo'lmadi. Bot kanalda admin ekanini tekshiring.", show_alert=True)
        try:
            await bot.send_message(ad["owner"], f"✅ E'loningiz #{ad_id} tasdiqlandi va kanalga joylandi!")
        except Exception:
            pass
        await c.message.edit_text(c.message.html_text + f"\n\n✅ Tasdiqladi: {who}", disable_web_page_preview=True)
    else:
        q("UPDATE ads SET status='rejected' WHERE id=?", (ad_id,), commit=True)
        try:
            await bot.send_message(ad["owner"], f"❌ E'loningiz #{ad_id} rad etildi. Savol bo'lsa admin bilan bog'laning.")
        except Exception:
            pass
        await c.message.edit_text(c.message.html_text + f"\n\n❌ Rad etdi: {who}", disable_web_page_preview=True)


@router.callback_query(ADM, F.data == "adm:pending")
async def adm_pending(c: CallbackQuery):
    await c.answer()
    rows = q("SELECT * FROM ads WHERE status='pending' ORDER BY id")
    if not rows:
        return await c.message.answer("Tasdiq kutayotgan e'lonlar yo'q.")
    for r in rows:
        await c.message.answer(
            f"📝 <b>E'lon #{r['id']}</b> (egasi: <code>{r['owner']}</code>)\n\n" + ad_text(ad_dict(r)),
            reply_markup=inline([("✅ Tasdiqlash", f"adv:ok:{r['id']}"), ("❌ Rad etish", f"adv:no:{r['id']}")]),
            disable_web_page_preview=True)


@router.message(F.text == "📋 E'lonlarim")
async def my_ads(m: Message):
    if not get_user(m.from_user.id):
        return await m.answer("Avval /start bosing.")
    rows = q("SELECT * FROM ads WHERE owner=? ORDER BY id DESC LIMIT 10", (m.from_user.id,))
    if not rows:
        return await m.answer("Sizda e'lonlar yo'q.")
    names = {"active": "faol", "pending": "tasdiq kutilmoqda", "full": "odam olindi",
             "closed": "yopilgan", "rejected": "rad etilgan"}
    for r in rows:
        btns = []
        if r["status"] == "active":
            btns.append(("🔒 Yopish", f"adclose:{r['id']}"))
        if hired_count(r["id"]) > 0:
            btns.append(("👥 Ishchilar", f"workers:{r['id']}"))
        kb = inline(btns, 2) if btns else None
        await m.answer(f"#{r['id']} | {r['kind']} | {money(r['price'])} so'm | "
                       f"👥 {hired_count(r['id'])}/{r['workers']} | {names.get(r['status'], r['status'])}",
                       reply_markup=kb)


# ---------------------------------------------------------------- ISHGA YOZILISH
async def begin_apply(m: Message, state: FSMContext, ad_id: int):
    ad = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    if not ad:
        return await m.answer("Bu e'lon topilmadi.", reply_markup=main_menu(m.chat.id))
    if ad["status"] == "full":
        return await m.answer("👥 Bu e'londa odam to'lgan.", reply_markup=main_menu(m.chat.id))
    if ad["status"] != "active":
        return await m.answer("Bu e'lon yopilgan yoki hali tasdiqlanmagan.", reply_markup=main_menu(m.chat.id))
    if hired_count(ad_id) >= (ad["workers"] or 1):
        return await m.answer("👥 Bu e'londa odam to'lgan.", reply_markup=main_menu(m.chat.id))
    if ad["owner"] == m.chat.id:
        return await m.answer("O'z e'loningizga yozila olmaysiz.", reply_markup=main_menu(m.chat.id))
    if q("SELECT 1 FROM apps WHERE ad_id=? AND user_id=? AND status IN "
         "('new','awaiting_pay','paid_check','paid')", (ad_id, m.chat.id), one=True):
        return await m.answer("Siz bu ishga allaqachon yozilgansiz.", reply_markup=main_menu(m.chat.id))
    now = int(time.time())
    recent = q("SELECT created FROM apps WHERE user_id=? AND status IN ('awaiting_pay','paid_check','paid') "
               "AND created>? ORDER BY created DESC", (m.chat.id, now - 12 * 3600), one=True)
    if recent:
        left = recent["created"] + 12 * 3600 - now
        h, mn = divmod(left // 60, 60)
        return await m.answer(f"⏳ 12 soat ichida faqat 1 ta ishga yozilish mumkin.\n"
                              f"Yangi ishga yozilishga <b>{h} soat {mn} daqiqa</b> qoldi.\n\n"
                              "Oldingi ishdan voz kechsangiz, «📝 Arizalarim» orqali bekor qilishingiz mumkin.",
                              reply_markup=main_menu(m.chat.id))
    await state.set_state(Apply.shot1)
    await state.update_data(ad_id=ad_id)
    await m.answer(f"✍️ E'lon #{ad_id} ga yozilish.\n\n"
                   "1️⃣ Bormoqchi bo'lgan <b>ish joyingiz</b> skrinshotini yuboring:",
                   reply_markup=cancel_kb())


@router.message(Apply.shot1, F.photo)
async def apply_shot1(m: Message, state: FSMContext):
    await state.update_data(shot1=m.photo[-1].file_id)
    await m.answer("2️⃣ Endi hozir <b>turgan joyingiz</b> skrinshotini yuboring:")
    await state.set_state(Apply.shot2)


@router.message(Apply.shot2, F.photo)
async def apply_shot2(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    ad = q("SELECT * FROM ads WHERE id=?", (d["ad_id"],), one=True)
    if not ad or ad["status"] != "active" or hired_count(ad["id"]) >= (ad["workers"] or 1):
        return await m.answer("😔 Kechikdingiz, bu e'londa odam to'lib bo'ldi.", reply_markup=main_menu(m.from_user.id))
    app_id = q("INSERT INTO apps(ad_id,user_id,shot1,shot2,created) VALUES(?,?,?,?,?)",
               (d["ad_id"], m.from_user.id, d["shot1"], m.photo[-1].file_id, int(time.time())), commit=True)
    await m.answer("✅ Arizangiz qabul qilindi!", reply_markup=main_menu(m.from_user.id))
    await process_application(bot, app_id)


async def process_application(bot, app_id):
    """Ariza AVTOMATIK tasdiqlanadi. Faqat kartadan to'lovni admin tasdiqlaydi."""
    app = q("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    user = get_user(app["user_id"])
    fee = fee_int()
    info = (f"🙋 <b>Yangi ariza #{app_id}</b> (e'lon #{ad['id']})\n"
            f"👤 {esc(user['name'])} {esc(user['surname'])}, {user['age']} yosh\n"
            f"📞 {esc(user['phone'])}\n🏠 {esc(user['address'])}\n")
    if fee <= 0 or user["balance"] >= fee:
        if fee > 0:
            q("UPDATE users SET balance=balance-? WHERE id=?", (fee, user["id"]), commit=True)
        mark_paid(app_id, "balance", fee)
        left = get_user(user["id"])["balance"]
        extra = f"\n💰 Hisobingizdan {money(fee)} so'm yechildi. Qoldiq: {money(left)} so'm" if fee > 0 else ""
        await bot.send_message(user["id"], "✅ Arizangiz avtomatik tasdiqlandi!" + extra)
        await release_contacts(bot, app_id)
        await check_fill(bot, ad["id"])
        info += "✅ Avto-tasdiqlandi, to'lov balansdan yechildi"
    else:
        q("UPDATE apps SET status='awaiting_pay' WHERE id=?", (app_id,), commit=True)
        card = card_for(ad)
        await bot.send_message(
            user["id"],
            f"✅ Arizangiz avtomatik tasdiqlandi!\n\n💳 To'lov kartasi:\n<code>{esc(card)}</code>\n"
            f"💵 Summa: <b>{money(fee)} so'm</b>\n\n"
            "To'lov qilgach, <b>chek rasmini</b> shu yerga yuboring 📸\n"
            "💡 Keyingi safar tezroq bo'lishi uchun «💰 Hisobim» orqali balansni to'ldirib qo'ying.")
        info += "✅ Avto-tasdiqlandi. 💳 To'lov kutilmoqda, chek kelganda tasdiqlaysiz."
    target = handler_for(ad)
    try:
        await bot.send_media_group(target, [
            InputMediaPhoto(media=app["shot1"], caption=f"📝 Ariza #{app_id} — ish joyi"),
            InputMediaPhoto(media=app["shot2"], caption="Ariza beruvchining joyi")])
        await bot.send_message(target, info)
    except Exception as e:
        logging.warning("Ariza haqida xabar yuborilmadi: %s", e)


async def release_contacts(bot, app_id):
    app = q("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    owner = get_user(ad["owner"])
    worker = get_user(app["user_id"])
    admin_owned = is_admin(ad["owner"])
    phone = ad["phone"] or (owner["phone"] if owner else "")
    # admin e'lonida faqat ish beruvchi raqami beriladi (admin ismi/raqami/Telegrami emas)
    who = "" if admin_owned or not owner else f"👤 {esc(owner['name'])} {esc(owner['surname'])}\n"
    ad_d = ad_dict(ad)
    loc = loc_line(ad_d).replace("Xaritada ko'rish", "Ish manzili") if ad["lat"] is not None else loc_line(ad_d)
    await bot.send_message(
        app["user_id"],
        f"✅ To'lov qabul qilindi!\n\n📌 E'lon #{ad['id']} ish beruvchisi:\n{who}"
        f"📞 {esc(phone)}\n{loc}\n\nIshga borasizmi?",
        reply_markup=inline([("✅ Boraman", f"wy:{app_id}"), ("❌ Bekor qilish", f"wc:{app_id}")]))
    await bot.send_message(app["user_id"], "⭐ Xizmatimizni baholang (1 — yomon, 5 — a'lo):",
                           reply_markup=rate_kb(app_id, "w"))
    info = (f"🙋 E'lon #{ad['id']} bo'yicha ishchi topildi:\n"
            f"👤 {esc(worker['name'])} {esc(worker['surname'])}, {worker['age']} yosh\n"
            f"📞 {esc(worker['phone'])}")
    kb = inline([("❌ Chaqirmaslik (bekor qilish)", f"dis:{app_id}")], 1)
    for target in {ad["owner"], handler_for(ad)}:
        try:
            await send_profile(bot, target, worker, caption=info, reply_markup=kb)
        except Exception as e:
            logging.warning("Ishchi haqida xabar yuborilmadi %s: %s", target, e)
    if not admin_owned:
        try:
            await bot.send_message(ad["owner"], "⭐ Xizmatimizni baholang (1 — yomon, 5 — a'lo):",
                                   reply_markup=rate_kb(app_id, "o"))
        except Exception:
            pass


@router.callback_query(ADM, F.data.startswith("ap:"))
async def admin_app(c: CallbackQuery, bot: Bot):
    _, act, app_id = c.data.split(":")
    app = q("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
    if not app or app["status"] != "new":
        return await c.answer("Allaqachon ko'rib chiqilgan.", show_alert=True)
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    if not can_manage(c.from_user.id, ad):
        return await c.answer("Bu ariza sizniki emas.", show_alert=True)
    if act == "no":
        q("UPDATE apps SET status='rejected' WHERE id=?", (app_id,), commit=True)
        await bot.send_message(app["user_id"], "❌ Afsus, arizangiz rad etildi.")
        return await c.message.edit_text(c.message.html_text + "\n\n❌ Rad etildi")
    fee = fee_int()
    user = get_user(app["user_id"])
    if fee > 0 and user["balance"] >= fee:
        # balansdan avtomatik yechish
        q("UPDATE users SET balance=balance-? WHERE id=?", (fee, user["id"]), commit=True)
        mark_paid(app_id, "balance", fee)
        left = user["balance"] - fee
        await bot.send_message(user["id"], f"✅ Arizangiz tasdiqlandi!\n💰 Hisobingizdan {money(fee)} so'm "
                                           f"yechildi. Qoldiq: {money(left)} so'm")
        await release_contacts(bot, int(app_id))
        await check_fill(bot, app["ad_id"])
        await c.message.edit_text(c.message.html_text + "\n\n✅ Tasdiqlandi (balansdan yechildi)")
    else:
        q("UPDATE apps SET status='awaiting_pay' WHERE id=?", (app_id,), commit=True)
        card = card_for(ad)
        await bot.send_message(
            user["id"],
            f"✅ Arizangiz tasdiqlandi!\n\n💳 To'lov kartasi:\n<code>{esc(card)}</code>\n"
            f"💵 Summa: <b>{money(fee)} so'm</b>\n\n"
            "To'lov qilgach, <b>chek rasmini</b> shu yerga yuboring 📸\n"
            "💡 Keyingi safar tezroq bo'lishi uchun «💰 Hisobim» orqali balansni to'ldirib qo'ying.")
        await c.message.edit_text(c.message.html_text + "\n\n✅ Tasdiqlandi (to'lov kutilmoqda)")


@router.message(StateFilter(None), F.photo)
async def receipt(m: Message, bot: Bot):
    app = q("SELECT * FROM apps WHERE user_id=? AND status='awaiting_pay' ORDER BY id DESC",
            (m.from_user.id,), one=True)
    if not app:
        return await m.answer("Hozir sizdan chek kutilmayapti. Menyudan tanlang 👇",
                              reply_markup=main_menu(m.from_user.id))
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    q("UPDATE apps SET receipt=?, status='paid_check' WHERE id=?",
      (m.photo[-1].file_id, app["id"]), commit=True)
    await bot.send_photo(handler_for(ad), m.photo[-1].file_id,
                         caption=f"🧾 Ariza #{app['id']} to'lov cheki\n🆔 <code>{m.from_user.id}</code>",
                         reply_markup=inline([("✅ To'lov keldi", f"pay:ok:{app['id']}"),
                                              ("❌ Yo'q", f"pay:no:{app['id']}")]))
    await m.answer("⏳ Chek tekshirilmoqda. Tasdiqlangach xabar beramiz.")


@router.callback_query(ADM, F.data.startswith("pay:"))
async def admin_pay(c: CallbackQuery, bot: Bot):
    _, act, app_id = c.data.split(":")
    app = q("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
    if not app or app["status"] != "paid_check":
        return await c.answer("Allaqachon ko'rib chiqilgan.", show_alert=True)
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    if not can_manage(c.from_user.id, ad):
        return await c.answer("Bu ariza sizniki emas.", show_alert=True)
    if act == "ok":
        if hired_count(ad["id"]) >= (ad["workers"] or 1):
            fee = fee_int()
            q("UPDATE apps SET status='refunded' WHERE id=?", (app_id,), commit=True)
            q("UPDATE users SET balance=balance+? WHERE id=?", (fee, app["user_id"]), commit=True)
            await bot.send_message(app["user_id"], f"😔 Bu ishga odam to'lib bo'ldi. To'lovingiz ({money(fee)} so'm) "
                                                   "balansingizga qaytarildi, keyingi ishga ishlatishingiz mumkin.")
            return await c.message.edit_caption(
                caption=c.message.html_caption + "\n\nℹ️ Ish to'lgan edi, summa foydalanuvchi balansiga qaytarildi")
        mark_paid(app_id, "card", fee_int())
        await release_contacts(bot, int(app_id))
        await check_fill(bot, ad["id"])
        await c.message.edit_caption(caption=c.message.html_caption + "\n\n✅ Tasdiqlandi")
    else:
        q("UPDATE apps SET status='awaiting_pay' WHERE id=?", (app_id,), commit=True)
        await bot.send_message(app["user_id"], "❌ To'lov tasdiqlanmadi. To'g'ri chekni qayta yuboring.")
        await c.message.edit_caption(caption=c.message.html_caption + "\n\n❌ Rad etildi")


# ---------------------------------------------------------------- BAHOLASH
@router.message(F.text == "⭐ Baholar")
async def ratings_panel(m: Message):
    await m.answer(rating_text(), reply_markup=inline([("⭐ Xizmatni baholash", "rate:menu")], 1))


@router.callback_query(F.data == "rate:menu")
async def rate_menu(c: CallbackQuery):
    await c.answer()
    uid = c.from_user.id
    todo = []
    for r in q("SELECT * FROM apps WHERE user_id=? AND status='paid' ORDER BY id DESC LIMIT 20", (uid,)):
        if not q("SELECT 1 FROM ratings WHERE app_id=? AND role='w'", (r["id"],), one=True):
            todo.append((r["id"], "w", f"Ishga yozilish (ariza #{r['id']})"))
    for r in q("SELECT apps.id AS id, ads.owner AS owner FROM apps JOIN ads ON ads.id=apps.ad_id "
               "WHERE ads.owner=? AND apps.status='paid' ORDER BY apps.id DESC LIMIT 20", (uid,)):
        if not is_admin(uid) and not q("SELECT 1 FROM ratings WHERE app_id=? AND role='o'", (r["id"],), one=True):
            todo.append((r["id"], "o", f"E'lon berish (ariza #{r['id']})"))
    if not todo:
        return await c.message.answer("Baholash uchun avval ishga yozilib to'lov qilgan yoki e'lon "
                                      "bergan bo'lishingiz kerak. Baholanmagan xizmat yo'q.")
    for app_id, role, label in todo[:5]:
        await c.message.answer(f"⭐ {label}. Xizmatimizni baholang (1 — yomon, 5 — a'lo):",
                               reply_markup=rate_kb(app_id, role))


@router.callback_query(F.data.startswith("rate:"))
async def rate_cb(c: CallbackQuery):
    parts = c.data.split(":")
    if len(parts) != 4:
        return await c.answer()
    _, app_id, role, score = parts
    app = q("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
    if not app or role not in ("w", "o") or not score.isdigit() or not 1 <= int(score) <= 5:
        return await c.answer("Xato.", show_alert=True)
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    uid = c.from_user.id
    if (role == "w" and app["user_id"] != uid) or (role == "o" and ad["owner"] != uid):
        return await c.answer("Bu baho sizniki emas.", show_alert=True)
    if q("SELECT 1 FROM ratings WHERE app_id=? AND role=?", (app_id, role), one=True):
        return await c.answer("Siz allaqachon baholagansiz.", show_alert=True)
    q("INSERT INTO ratings(user_id,app_id,role,score,ts) VALUES(?,?,?,?,?)",
      (uid, app_id, role, int(score), int(time.time())), commit=True)
    await c.answer("Rahmat!")
    await c.message.edit_text(f"⭐ Bahoyingiz: <b>{score}/5</b>. Rahmat!")


# ---------------------------------------------------------------- ISHCHI PANELI (ARIZALARIM)
@router.message(F.text == "📝 Arizalarim")
async def my_apps(m: Message):
    if not get_user(m.from_user.id):
        return await m.answer("Avval /start bosing.")
    rows = q("SELECT * FROM apps WHERE user_id=? AND status IN ('awaiting_pay','paid_check','paid') "
             "ORDER BY id DESC LIMIT 10", (m.from_user.id,))
    if not rows:
        return await m.answer("Sizda faol arizalar yo'q.")
    for r in rows:
        if r["status"] == "paid":
            st = "✅ To'langan" + (", borishni tasdiqlagansiz" if r["confirmed"] else "")
            btns = ([] if r["confirmed"] else [("✅ Boraman", f"wy:{r['id']}")]) + [("❌ Bekor qilish", f"wc:{r['id']}")]
        elif r["status"] == "awaiting_pay":
            st, btns = "💳 To'lov kutilmoqda", [("❌ Bekor qilish", f"wc:{r['id']}")]
        else:
            st, btns = "🧾 Chek tekshirilmoqda", []
        await m.answer(f"📝 Ariza #{r['id']} (e'lon #{r['ad_id']}) — {st}",
                       reply_markup=inline(btns, 2) if btns else None)


async def notify_ad_side(bot, ad, text, kb=None):
    for target in {ad["owner"], handler_for(ad)}:
        try:
            await bot.send_message(target, text, reply_markup=kb)
        except Exception:
            pass


@router.callback_query(F.data.startswith("wy:"))
async def worker_confirm(c: CallbackQuery, bot: Bot):
    app = q("SELECT * FROM apps WHERE id=?", (c.data[3:],), one=True)
    if not app or app["user_id"] != c.from_user.id or app["status"] != "paid":
        return await c.answer("Bu ariza faol emas.", show_alert=True)
    q("UPDATE apps SET confirmed=1 WHERE id=?", (app["id"],), commit=True)
    await c.answer("Tasdiqlandi ✅", show_alert=True)
    try:
        await c.message.edit_reply_markup(reply_markup=inline([("❌ Bekor qilish", f"wc:{app['id']}")], 1))
    except Exception:
        pass
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    u = get_user(app["user_id"])
    await notify_ad_side(bot, ad, f"✅ Ishchi {esc(u['name'])} {esc(u['surname'])} e'lon #{ad['id']} "
                                  "uchun borishini tasdiqladi.")


@router.callback_query(F.data.startswith("wc:"))
async def worker_cancel_ask(c: CallbackQuery):
    app = q("SELECT * FROM apps WHERE id=?", (c.data[3:],), one=True)
    if not app or app["user_id"] != c.from_user.id or app["status"] not in ("awaiting_pay", "paid"):
        return await c.answer("Bu arizani bekor qilib bo'lmaydi.", show_alert=True)
    await c.answer()
    warn = ("Ishdan voz kechsangiz, to'langan summa <b>qaytarilmaydi</b>. Rostdan bekor qilasizmi?"
            if app["status"] == "paid" else "Arizani bekor qilasizmi?")
    await c.message.answer(warn, reply_markup=inline(
        [("✅ Ha, bekor qilish", f"wcok:{app['id']}"), ("↩️ Yo'q", "noop")]))


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()
    await c.message.delete()


@router.callback_query(F.data.startswith("wcok:"))
async def worker_cancel_do(c: CallbackQuery, bot: Bot):
    app = q("SELECT * FROM apps WHERE id=?", (c.data[5:],), one=True)
    if not app or app["user_id"] != c.from_user.id or app["status"] not in ("awaiting_pay", "paid"):
        return await c.answer("Bu arizani bekor qilib bo'lmaydi.", show_alert=True)
    was_paid = app["status"] == "paid"
    q("UPDATE apps SET status='cancelled' WHERE id=?", (app["id"],), commit=True)
    await c.answer()
    await c.message.edit_text("❌ Ariza bekor qilindi.")
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    if was_paid:
        await after_slot_freed(bot, ad["id"])
        u = get_user(app["user_id"])
        await notify_ad_side(bot, ad, f"❌ Ishchi {esc(u['name'])} {esc(u['surname'])} e'lon #{ad['id']} "
                                      "uchun ishdan voz kechdi. Joy yana bo'sh.")


# ---------------------------------------------------------------- ISH BERUVCHI: ISHGA CHAQIRMASLIK
def can_dismiss(uid, ad):
    return uid == ADMIN_ID or uid == ad["owner"] or uid == handler_for(ad)


@router.callback_query(F.data.startswith("dis:"))
async def dismiss_ask(c: CallbackQuery):
    app = q("SELECT * FROM apps WHERE id=?", (c.data[4:],), one=True)
    if not app or app["status"] != "paid":
        return await c.answer("Bu ishchi faol emas.", show_alert=True)
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    if not can_dismiss(c.from_user.id, ad):
        return await c.answer("Bu sizning e'loningiz emas.", show_alert=True)
    await c.answer()
    await c.message.answer("Bu ishchini ishga chaqirmaslikni tasdiqlaysizmi?\n"
                           "Uning to'lovi balansiga qaytariladi va joy yana ochiladi.",
                           reply_markup=inline([("✅ Ha, chaqirmayman", f"disok:{app['id']}"), ("↩️ Yo'q", "noop")]))


@router.callback_query(F.data.startswith("disok:"))
async def dismiss_do(c: CallbackQuery, bot: Bot):
    app = q("SELECT * FROM apps WHERE id=?", (c.data[6:],), one=True)
    if not app or app["status"] != "paid":
        return await c.answer("Bu ishchi faol emas.", show_alert=True)
    ad = q("SELECT * FROM ads WHERE id=?", (app["ad_id"],), one=True)
    if not can_dismiss(c.from_user.id, ad):
        return await c.answer("Bu sizning e'loningiz emas.", show_alert=True)
    amount = app["paid_amount"] or fee_int()
    q("UPDATE apps SET status='dismissed' WHERE id=?", (app["id"],), commit=True)
    q("UPDATE users SET balance=balance+? WHERE id=?", (amount, app["user_id"]), commit=True)
    await c.answer()
    await c.message.edit_text("✅ Ishchi ishga chaqirilmadi, joy yana ochildi.")
    try:
        await bot.send_message(app["user_id"], f"😔 Ish beruvchi e'lon #{ad['id']} bo'yicha sizni ishga "
                                               f"chaqirmaslikka qaror qildi.\n💰 To'lovingiz ({money(amount)} so'm) "
                                               "balansingizga qaytarildi.")
    except Exception:
        pass
    await after_slot_freed(bot, ad["id"])


@router.callback_query(F.data.startswith("workers:"))
async def ad_workers(c: CallbackQuery, bot: Bot):
    ad = q("SELECT * FROM ads WHERE id=?", (c.data[8:],), one=True)
    if not ad or not can_dismiss(c.from_user.id, ad):
        return await c.answer("Bu sizning e'loningiz emas.", show_alert=True)
    await c.answer()
    rows = q("SELECT * FROM apps WHERE ad_id=? AND status='paid'", (ad["id"],))
    if not rows:
        return await c.message.answer("Hali qabul qilingan ishchi yo'q.")
    for r in rows:
        u = get_user(r["user_id"])
        await send_profile(bot, c.message.chat.id, u, caption=(
            f"👤 {esc(u['name'])} {esc(u['surname'])}, {u['age']} yosh\n📞 {esc(u['phone'])}\n"
            f"{'✅ Borishni tasdiqlagan' if r['confirmed'] else '⏳ Tasdiqlamagan'}"),
            reply_markup=inline([("❌ Chaqirmaslik (bekor qilish)", f"dis:{r['id']}")], 1))


# ---------------------------------------------------------------- ADMIN PANEL
def admin_kb(uid):
    if is_main(uid):
        return inline([
            ("📊 Statistika", "adm:stats"), ("🔎 Foydalanuvchi qidirish", "adm:find"),
            ("👥 Foydalanuvchilar", "adm:users"), ("📢 E'lonlar", "adm:ads"),
            ("📝 Arizalar", "adm:apps"), ("⏳ Kutilayotgan e'lonlar", "adm:pending"),
            ("💰 Balans berish", "adm:bal"),
            ("👮 Adminlar", "adm:admins"), ("💳 Kartalarim", "adm:cards"), ("⭐ Baholar", "adm:ratings"),
            ("⭐ Standart karta", "set:card"), ("💵 Xizmat narxi", "set:fee"),
            ("📨 Xabar yuborish", "adm:bc"), ("🚫 Bloklash", "adm:ban"),
            ("♻️ Blokdan chiqarish", "adm:unban"),
        ])
    return inline([("⏳ Kutilayotgan e'lonlar", "adm:pending"), ("💳 Kartalarim", "adm:cards"),
                   ("📝 Arizalarim", "adm:apps")], 1)


@router.message(ADM, F.text == "🛠 Admin panel")
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb(m.from_user.id))


async def send_user_card(m, uid, bot):
    u = get_user(uid)
    if not u:
        return await m.answer("Foydalanuvchi topilmadi.")
    ads_n = q("SELECT COUNT(*) FROM ads WHERE owner=?", (uid,), one=True)[0]
    apps_n = q("SELECT COUNT(*) FROM apps WHERE user_id=?", (uid,), one=True)[0]
    role = "👮 Admin" if is_admin(uid) else "Foydalanuvchi"
    blocked = "ha" if u["banned"] else "yo'q"
    cap = (f"👤 <b>{esc(u['name'])} {esc(u['surname'])}</b> ({role})\n"
           f"📞 {esc(u['phone'])}\n🏠 {esc(u['address'])}\n🎂 {u['age']} yosh\n"
           f"🆔 <code>{uid}</code>\n🔗 @{esc(u['username'] or '—')}\n"
           f"💰 Balans: <b>{money(u['balance'])} so'm</b>\n"
           f"📢 E'lonlari: {ads_n} | 📝 Arizalari: {apps_n}\n🚫 Bloklangan: {blocked}")
    kb = inline([
        ("💰 Balans o'zgartirish", f"bal:{uid}"), ("✉️ Xabar yozish", f"rp:{uid}"),
        ("♻️ Blokdan chiqarish" if u["banned"] else "🚫 Bloklash",
         f"unban:{uid}" if u["banned"] else f"ban:{uid}"),
    ], 1)
    await send_profile(bot, m.chat.id, u, caption=cap, reply_markup=kb)


def users_buttons(rows):
    return inline([(f"{r['name']} {r['surname']} | {r['phone']}", f"u:{r['id']}") for r in rows], 1)


def search_users(t):
    t = t.strip()
    if t.isdigit():
        r = get_user(int(t))
        if r:
            return [r]
        return q("SELECT * FROM users WHERE REPLACE(phone,'+','') LIKE ? LIMIT 10", ("%" + t[-9:] + "%",))
    if t.startswith("@"):
        return q("SELECT * FROM users WHERE username=? COLLATE NOCASE", (t[1:],))
    like = "%" + t + "%"
    return q("SELECT * FROM users WHERE name LIKE ? OR surname LIKE ? OR username LIKE ? LIMIT 10",
             (like, like, like))


@router.callback_query(MAIN, F.data.startswith("u:"))
async def user_card_cb(c: CallbackQuery, bot: Bot):
    await c.answer()
    await send_user_card(c.message, int(c.data[2:]), bot)


@router.message(MAIN, Adm.find, F.text)
async def adm_find(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    rows = search_users(m.text)
    if not rows:
        return await m.answer("Hech narsa topilmadi.")
    if len(rows) == 1:
        return await send_user_card(m, rows[0]["id"], bot)
    await m.answer("Topilganlar, birini tanlang:", reply_markup=users_buttons(rows))


SET_PROMPTS = {
    "card": "Standart karta raqami (va egasi) ni yozing.\n"
            "U balans to'ldirish va oddiy foydalanuvchilar e'lonlari uchun ishlatiladi:",
    "fee": "Ishga yozilish xizmat haqini yozing (faqat raqam, masalan: 10000):",
}


@router.callback_query(MAIN, F.data.startswith("set:"))
async def set_start(c: CallbackQuery, state: FSMContext):
    key = c.data[4:]
    await c.answer()
    await state.set_state(Adm.setting)
    await state.update_data(key=key)
    await c.message.answer(SET_PROMPTS[key])


@router.message(MAIN, Adm.setting, F.text)
async def set_save(m: Message, state: FSMContext):
    d = await state.get_data()
    val = m.text.strip()
    if d["key"] == "fee":
        digits = re.sub(r"\D", "", val)
        if not digits:
            return await m.answer("Faqat raqam yozing.")
        val = digits
    q("UPDATE settings SET v=? WHERE k=?", (val, d["key"]), commit=True)
    await state.clear()
    await m.answer("✅ Saqlandi.")


# ---- kartalar (hamma adminlar uchun)
async def show_cards(m, uid):
    rows = q("SELECT * FROM admin_cards WHERE admin_id=?", (uid,))
    for r in rows:
        await m.answer(f"💳 <code>{esc(r['card'])}</code>",
                       reply_markup=inline([("🗑 O'chirish", f"dc:{r['id']}")], 1))
    if not rows:
        await m.answer("Sizda saqlangan karta yo'q.")
    await m.answer("E'lon berayotganda shu kartalardan birini tanlaysiz.",
                   reply_markup=inline([("➕ Karta qo'shish", "addcard")], 1))


@router.callback_query(ADM, F.data == "addcard")
async def addcard_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.addcard)
    await c.message.answer("Karta raqami va egasi ismini yozing\n(masalan: 8600 1234 5678 9012 Ali V.):")


@router.message(ADM, Adm.addcard, F.text)
async def addcard_save(m: Message, state: FSMContext):
    await state.clear()
    q("INSERT INTO admin_cards(admin_id,card) VALUES(?,?)", (m.from_user.id, m.text.strip()[:80]), commit=True)
    await m.answer("✅ Karta saqlandi.")


@router.callback_query(ADM, F.data.startswith("dc:"))
async def delcard(c: CallbackQuery):
    q("DELETE FROM admin_cards WHERE id=? AND admin_id=?", (int(c.data[3:]), c.from_user.id), commit=True)
    await c.answer("O'chirildi", show_alert=True)
    await c.message.delete()


# ---- Arizalar ro'yxati
@router.callback_query(ADM, F.data == "adm:apps")
async def adm_apps(c: CallbackQuery):
    await c.answer()
    if is_main(c.from_user.id):
        rows = q("SELECT apps.*, ads.owner FROM apps JOIN ads ON ads.id=apps.ad_id "
                 "WHERE apps.status IN ('new','awaiting_pay','paid_check') ORDER BY apps.id DESC LIMIT 20")
    else:
        rows = q("SELECT apps.*, ads.owner FROM apps JOIN ads ON ads.id=apps.ad_id "
                 "WHERE ads.owner=? AND apps.status IN ('new','awaiting_pay','paid_check') "
                 "ORDER BY apps.id DESC LIMIT 20", (c.from_user.id,))
    names = {"new": "yangi", "awaiting_pay": "to'lov kutilmoqda", "paid_check": "chek tekshiriladi"}
    t = "\n".join(f"Ariza #{r['id']} (e'lon #{r['ad_id']}) — {names.get(r['status'], r['status'])}"
                  for r in rows) or "Kutilayotgan arizalar yo'q."
    await c.message.answer("📝 Arizalar:\n\n" + t)


@router.callback_query(ADM, F.data == "adm:cards")
async def adm_cards(c: CallbackQuery):
    await c.answer()
    await show_cards(c.message, c.from_user.id)


# ---- bosh admin funksiyalari
@router.callback_query(MAIN, F.data.startswith("adm:"))
async def admin_cb(c: CallbackQuery, state: FSMContext):
    act = c.data[4:]
    await c.answer()
    if act == "stats":
        def n(sql):
            return q(sql, one=True)[0]
        t = "📊 Foydalanuvchilar: " + str(n("SELECT COUNT(*) FROM users"))
        t += "\n👮 Qo'shimcha adminlar: " + str(n("SELECT COUNT(*) FROM admins"))
        t += "\n🚫 Bloklangan: " + str(n("SELECT COUNT(*) FROM users WHERE banned=1"))
        t += "\n📢 E'lonlar: " + str(n("SELECT COUNT(*) FROM ads"))
        t += "\n📝 Arizalar: " + str(n("SELECT COUNT(*) FROM apps"))
        t += "\n✅ To'langan: " + str(n("SELECT COUNT(*) FROM apps WHERE status='paid'"))
        avg = q("SELECT COUNT(*) AS c, COALESCE(AVG(score),0) AS a FROM ratings", one=True)
        t += f"\n⭐ O'rtacha baho: {avg['a']:.1f}/5 ({avg['c']} ta)"
        t += "\n💰 Jami balanslar: " + money(n("SELECT COALESCE(SUM(balance),0) FROM users")) + " so'm"
        await c.message.answer(t)
    elif act == "ratings":
        t = rating_text() + "\n<b>Oxirgi baholar:</b>\n"
        for r in q("SELECT * FROM ratings ORDER BY id DESC LIMIT 10"):
            u = get_user(r["user_id"])
            t += f"{r['score']}⭐ — {esc(u['name']) if u else r['user_id']} ({'ishchi' if r['role'] == 'w' else 'ish beruvchi'})\n"
        await c.message.answer(t)
    elif act == "find":
        await state.set_state(Adm.find)
        await c.message.answer("Qidirish uchun ism, familiya, telefon, @username yoki ID yozing:")
    elif act == "users":
        rows = q("SELECT * FROM users ORDER BY rowid DESC LIMIT 20")
        if not rows:
            return await c.message.answer("Foydalanuvchilar yo'q.")
        await c.message.answer("👥 Oxirgi 20 ta. Anketasini ko'rish uchun tanlang:",
                               reply_markup=users_buttons(rows))
    elif act == "ads":
        rows = q("SELECT * FROM ads ORDER BY id DESC LIMIT 10")
        if not rows:
            return await c.message.answer("E'lonlar yo'q.")
        for r in rows:
            if r["status"] == "active":
                kb = inline([("🔒 Yopish", f"adclose:{r['id']}"), ("👥 Ishchilar", f"workers:{r['id']}")])
            elif r["status"] == "full":
                kb = inline([("👥 Ishchilar", f"workers:{r['id']}")], 1)
            elif r["status"] == "pending":
                kb = inline([("✅ Tasdiqlash", f"adv:ok:{r['id']}"), ("❌ Rad etish", f"adv:no:{r['id']}")])
            else:
                kb = None
            await c.message.answer(
                f"#{r['id']} | {r['kind']} | {money(r['price'])} | 👥 {hired_count(r['id'])}/{r['workers']} | "
                f"{r['status']} | egasi: {r['owner']}", reply_markup=kb)
    elif act == "bal":
        await state.set_state(Adm.bal)
        await state.update_data(uid=0)
        await c.message.answer("Format: <code>ID summa</code> (masalan: 123456789 50000)\n"
                               "Ayirish uchun minus: <code>123456789 -20000</code>")
    elif act == "admins":
        rows = q("SELECT * FROM admins")
        for r in rows:
            u = get_user(r["id"])
            name = f"{u['name']} {u['surname']}" if u else "Noma'lum"
            await c.message.answer(f"👮 {esc(name)} (<code>{r['id']}</code>)",
                                   reply_markup=inline([("🗑 Olib tashlash", f"da:{r['id']}")], 1))
        if not rows:
            await c.message.answer("Qo'shimcha adminlar yo'q.")
        await c.message.answer("Yangi admin qo'shish:", reply_markup=inline([("➕ Admin qo'shish", "addadmin")], 1))
    elif act == "bc":
        await state.set_state(Adm.broadcast)
        await c.message.answer("Hammaga yuboriladigan xabarni yozing:")
    elif act == "ban":
        await state.set_state(Adm.ban)
        await c.message.answer("Bloklanadigan foydalanuvchi ID sini yozing:")
    elif act == "unban":
        await state.set_state(Adm.unban)
        await c.message.answer("Blokdan chiqariladigan ID ni yozing:")


@router.callback_query(MAIN, F.data == "addadmin")
async def addadmin_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.addadmin)
    await c.message.answer("Yangi admin ID, telefon yoki @username ini yozing.\n"
                           "(U avval botga /start bosib ro'yxatdan o'tgan bo'lishi kerak)")


@router.message(MAIN, Adm.addadmin, F.text)
async def addadmin_save(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    rows = search_users(m.text)
    if len(rows) != 1:
        return await m.answer("Aniq bitta foydalanuvchi topilmadi. ID bilan urinib ko'ring.")
    u = rows[0]
    if is_admin(u["id"]):
        return await m.answer("Bu foydalanuvchi allaqachon admin.")
    q("INSERT OR IGNORE INTO admins VALUES(?)", (u["id"],), commit=True)
    await m.answer(f"✅ {esc(u['name'])} {esc(u['surname'])} admin qilindi.")
    try:
        await bot.send_message(u["id"], "👮 Siz admin etib tayinlandingiz!\n"
                                        "/start bosing, menyuda «🛠 Admin panel» chiqadi. "
                                        "U yerdan o'z kartangizni qo'shing.")
    except Exception:
        pass


@router.callback_query(MAIN, F.data.startswith("da:"))
async def deladmin(c: CallbackQuery, bot: Bot):
    uid = int(c.data[3:])
    q("DELETE FROM admins WHERE id=?", (uid,), commit=True)
    q("DELETE FROM admin_cards WHERE admin_id=?", (uid,), commit=True)
    await c.answer("Admin olib tashlandi", show_alert=True)
    await c.message.delete()
    try:
        await bot.send_message(uid, "Siz endi admin emassiz.", reply_markup=main_menu(uid))
    except Exception:
        pass


# ---- balans berish
@router.callback_query(MAIN, F.data.startswith("bal:"))
async def bal_from_card(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.bal)
    await state.update_data(uid=int(c.data[4:]))
    await c.message.answer("Summani yozing (qo'shish uchun 50000, ayirish uchun -20000):")


@router.message(MAIN, Adm.bal, F.text)
async def bal_save(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    parts = m.text.split()
    uid = d.get("uid", 0)
    if uid:
        amount_s = parts[0] if parts else ""
    else:
        if len(parts) != 2 or not parts[0].isdigit():
            return await m.answer("Format noto'g'ri. Masalan: 123456789 50000")
        uid, amount_s = int(parts[0]), parts[1]
    if not re.fullmatch(r"-?\d+", amount_s):
        return await m.answer("Summa faqat raqam bo'lishi kerak.")
    u = get_user(uid)
    if not u:
        await state.clear()
        return await m.answer("Foydalanuvchi topilmadi.")
    amount = int(amount_s)
    new = u["balance"] + amount
    if new < 0:
        return await m.answer(f"Balans manfiy bo'lib qoladi (hozir {money(u['balance'])}). Boshqa summa yozing.")
    q("UPDATE users SET balance=? WHERE id=?", (new, uid), commit=True)
    await state.clear()
    await m.answer(f"✅ {esc(u['name'])} balansi: {money(u['balance'])} → <b>{money(new)} so'm</b>")
    sign = "to'ldirildi" if amount >= 0 else "kamaytirildi"
    try:
        await bot.send_message(uid, f"💰 Hisobingiz {money(abs(amount))} so'mga {sign}.\n"
                                    f"Balans: {money(new)} so'm")
    except Exception:
        pass


# ---- e'lonni yopish, bloklash, xabar
@router.callback_query(F.data.startswith("adclose:"))
async def ad_close(c: CallbackQuery, bot: Bot):
    ad_id = int(c.data.split(":")[1])
    ad = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    if not ad:
        return await c.answer("E'lon topilmadi.", show_alert=True)
    if c.from_user.id != ADMIN_ID and c.from_user.id != ad["owner"]:
        return await c.answer("Bu e'lon sizniki emas.", show_alert=True)
    if ad["status"] not in ("active", "pending"):
        return await c.answer("E'lon allaqachon yopilgan.", show_alert=True)
    q("UPDATE ads SET status='closed' WHERE id=?", (ad_id,), commit=True)
    await refresh_post(bot, ad_id)      # e'lon kanalda qoladi, tagiga holat yoziladi
    await c.answer()
    await c.message.edit_text(f"#{ad_id} yopildi 🔒")


@router.callback_query(MAIN, F.data.startswith("ban:"))
async def ban_cb(c: CallbackQuery):
    q("UPDATE users SET banned=1 WHERE id=?", (int(c.data[4:]),), commit=True)
    await c.answer("Bloklandi", show_alert=True)


@router.callback_query(MAIN, F.data.startswith("unban:"))
async def unban_cb(c: CallbackQuery):
    q("UPDATE users SET banned=0 WHERE id=?", (int(c.data[6:]),), commit=True)
    await c.answer("Blokdan chiqarildi", show_alert=True)


@router.message(MAIN, Adm.broadcast, F.text)
async def adm_bc(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ok = 0
    for r in q("SELECT id FROM users WHERE banned=0"):
        try:
            await bot.send_message(r["id"], m.text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"📨 Yuborildi: {ok}")


@router.message(MAIN, Adm.ban, F.text)
async def adm_ban(m: Message, state: FSMContext):
    await state.clear()
    if m.text.strip().isdigit():
        q("UPDATE users SET banned=1 WHERE id=?", (int(m.text),), commit=True)
        await m.answer("🚫 Bloklandi.")


@router.message(MAIN, Adm.unban, F.text)
async def adm_unban(m: Message, state: FSMContext):
    await state.clear()
    if m.text.strip().isdigit():
        q("UPDATE users SET banned=0 WHERE id=?", (int(m.text),), commit=True)
        await m.answer("♻️ Blokdan chiqarildi.")


# ---------------------------------------------------------------- Javobsiz xabarlar
@router.message(StateFilter(None))
async def fallback(m: Message):
    if get_user(m.from_user.id):
        await m.answer("Menyudan tanlang 👇", reply_markup=main_menu(m.from_user.id))
    else:
        await m.answer("Boshlash uchun /start bosing.")


@router.message(~StateFilter(None))
async def wrong_input(m: Message):
    await m.answer("Iltimos, so'ralgan ma'lumotni yuboring yoki «❌ Bekor qilish» ni bosing.")


# ---------------------------------------------------------------- RUN
async def health(request):
    return web.Response(text="Bot ishlayapti")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000")))
    await site.start()


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    await start_web()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(BanMiddleware())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
