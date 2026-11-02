"""ربات تلگرامی مدیریت خرج‌های ماهانه.

امکانات:
- ثبت سریع خرج با یک پیام (مثل: 250000 کافه) و ثبت مرحله‌ای با دسته‌بندی
- گزارش ماهانهٔ شمسی با ناوبری بین ماه‌ها، تفکیک دسته‌بندی و لیست خریدها
- جمع خرج از ابتدای ماه تا لحظهٔ حال
- واحد پول قابل تنظیم (در نخستین اجرا از کاربر پرسیده می‌شود)
- نمایش اعداد با جداکنندهٔ «'» برای هر سه رقم از راست: 1'234'567
- حذف تک‌خرج، واگرد آخرین خرج، پاک‌سازی کل داده‌ها و خروجی CSV
- ذخیره‌سازی محلی با SQLite (بدون سرویس خارجی)
"""

from __future__ import annotations

import csv
import html
import io
import logging
import os
import re
from datetime import date
from pathlib import Path

import jdatetime
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
from utils import (
    CATEGORIES,
    CATEGORY_EMOJI,
    JALALI_MONTHS,
    format_amount,
    format_jalali,
    normalize_digits,
    parse_amount,
    today_tehran,
    to_jalali,
    truncate,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("expense-bot")

# ------------------- رابط کاربری -------------------

MENU_ADD = "➕ ثبت خرج"
MENU_REPORT = "📊 گزارش ماه"
MENU_TOTAL = "💰 جمع این ماه"
MENU_ITEMS = "🛒 خریدهای ماه"
MENU_SETTINGS = "⚙️ تنظیمات"
MENU_HELP = "❓ راهنما"
MENU_LABELS = {MENU_ADD, MENU_REPORT, MENU_TOTAL, MENU_ITEMS, MENU_SETTINGS, MENU_HELP}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [MENU_ADD, MENU_REPORT],
        [MENU_TOTAL, MENU_ITEMS],
        [MENU_SETTINGS, MENU_HELP],
    ],
    resize_keyboard=True,
    input_field_placeholder="مثلاً: 250000 کافه",
)

CURRENCY_PRESETS = [
    "تومان", "ریال", "دلار آمریکا", "دلار کانادا",
    "یورو", "پوند", "درهم", "لیر", "یوان", "روبل",
]

QUICK_CATEGORY = CATEGORIES[-1][1]  # «سایر» برای ثبت سریع
NO_DESC = "بدون توضیح"
DASH_REPLIES = {"-", "–", "—", NO_DESC}
MAX_ITEMS_IN_REPORT = 40
MAX_ITEMS_IN_DELETE_VIEW = 30
MAX_DESC_LEN = 100
MAX_CURRENCY_LEN = 24


def e(text: object) -> str:
    """امن‌سازی متن کاربر برای ارسال با HTML."""
    return html.escape(str(text), quote=False)


# ------------------- کیبوردها -------------------

def currency_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(CURRENCY_PRESETS), 2):
        pair = CURRENCY_PRESETS[i:i + 2]
        rows.append([
            InlineKeyboardButton(f"💱 {name}", callback_data=f"cur:{i + j}")
            for j, name in enumerate(pair)
        ])
    rows.append([InlineKeyboardButton("✍️ نوشتن واحد دیگر", callback_data="cur:custom")])
    return InlineKeyboardMarkup(rows)


def category_keyboard(callback_fmt) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(CATEGORIES), 2):
        pair = CATEGORIES[i:i + 2]
        rows.append([
            InlineKeyboardButton(
                f"{CATEGORIES[i + j][0]} {CATEGORIES[i + j][1]}",
                callback_data=callback_fmt(i + j),
            )
            for j in range(len(pair))
        ])
    return InlineKeyboardMarkup(rows)


# ------------------- کمکی‌های پیام -------------------

async def safe_edit(message, text: str, kb: InlineKeyboardMarkup | None) -> None:
    """ویرایش پیام با تحمل خطاهای بی‌خطر (متن تغییریافته/پیام قدیمی)."""
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as ex:
        if "not modified" in str(ex).lower():
            return
        try:
            await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:  # noqa: BLE001 - پیام قدیمی‌تر از 48 ساعت و امثال آن
            pass


async def ensure_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """اگر واحد پول تنظیم نباشد، سؤال واحد پول را می‌پرسد."""
    if db.get_currency(update.effective_chat.id):
        return True
    context.chat_data["state"] = "currency"
    await update.effective_message.reply_text(
        "💱 برای ادامه، اول واحد پول خودتون رو انتخاب کنید:",
        reply_markup=currency_keyboard(),
    )
    return False


def month_shift(jy: int, jm: int, delta: int) -> tuple[int, int]:
    """جابه‌جایی بین ماه‌های شمسی با رعایت گذر سال."""
    idx = jy * 12 + (jm - 1) + delta
    return idx // 12, idx % 12 + 1


def parse_month_arg(args: list[str]) -> tuple[int, int] | None:
    """پارس آرگومان ماه به شکل 1405/08 (ارقام فارسی هم پذیرفته می‌شود)."""
    if not args:
        return None
    m = re.match(r"^(\d{4})[/\-.](\d{1,2})$", normalize_digits("".join(args)).strip())
    if not m:
        return None
    jy, jm = int(m.group(1)), int(m.group(2))
    if not (1200 <= jy <= 1500 and 1 <= jm <= 12):
        return None
    return jy, jm


# ------------------- رندر گزارش‌ها -------------------

def render_report(chat_id: int, jy: int, jm: int) -> tuple[str, InlineKeyboardMarkup]:
    """متن و کیبورد گزارش یک ماه شمسی."""
    currency = db.get_currency(chat_id) or ""
    rows = db.get_month(chat_id, jy, jm)
    title = f"{JALALI_MONTHS[jm - 1]} {jy}"

    prev_jy, prev_jm = month_shift(jy, jm, -1)
    next_jy, next_jm = month_shift(jy, jm, 1)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️", callback_data=f"rep:{prev_jy}:{prev_jm}"),
            InlineKeyboardButton(f"🗓 {title}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=f"rep:{next_jy}:{next_jm}"),
        ],
        [
            InlineKeyboardButton("🛒 مدیریت خریدها", callback_data=f"items:{jy}:{jm}"),
            InlineKeyboardButton("📄 خروجی CSV", callback_data=f"export:{jy}:{jm}"),
        ],
    ])

    if not rows:
        return (
            f"📊 گزارش <b>{e(title)}</b>\n\n"
            "هنوز خرجی در این ماه ثبت نشده 🌱\n"
            "برای ثبت، مبلغ و توضیح را بفرستید؛ مثلاً: <code>250000 کافه</code>"
        ), kb

    total = round(sum(r["amount"] for r in rows), 2)
    count = len(rows)
    lines = [
        f"📊 گزارش <b>{e(title)}</b>",
        "",
        f"💰 جمع کل: <b>{format_amount(total)}</b> {e(currency)}",
        f"🧾 تعداد خریدها: {count}",
        f"📈 میانگین هر خرید: {format_amount(round(total / count, 2))} {e(currency)}",
        "",
        "🏷 <b>دسته‌بندی‌ها:</b>",
    ]
    for name, (cat_sum, _cat_count) in db.category_totals(chat_id, jy, jm).items():
        pct = (cat_sum / total * 100) if total else 0
        filled = min(10, max(1, round(pct / 10)))
        bar = "█" * filled + "░" * (10 - filled)
        emoji = CATEGORY_EMOJI.get(name, "📦")
        lines.append(
            f"{emoji} {e(name)} — <b>{format_amount(round(cat_sum, 2))}</b> "
            f"{e(currency)} ({pct:.0f}%) {bar}"
        )

    lines += ["", f"🛍 <b>خریدها</b> ({count}):"]
    for i, r in enumerate(rows, 1):
        if i > MAX_ITEMS_IN_REPORT:
            lines.append(f"… و {count - MAX_ITEMS_IN_REPORT} مورد دیگر (برای مشاهدهٔ کامل: 📄 خروجی CSV)")
            break
        desc = r["description"] or NO_DESC
        lines.append(f"{i}. [{r['jday']:02d}] {e(truncate(desc, 40))} — <b>{format_amount(r['amount'])}</b>")
    return "\n".join(lines), kb


def render_items(chat_id: int, jy: int, jm: int) -> tuple[str, InlineKeyboardMarkup]:
    """لیست خریدهای یک ماه با دکمهٔ حذف برای هر کدام."""
    currency = db.get_currency(chat_id) or ""
    rows = db.get_month(chat_id, jy, jm)
    title = f"{JALALI_MONTHS[jm - 1]} {jy}"

    kb_rows = []
    for r in list(reversed(rows))[:MAX_ITEMS_IN_DELETE_VIEW]:
        desc = truncate(r["description"] or NO_DESC, 18)
        kb_rows.append([InlineKeyboardButton(
            f"🗑 {desc} — {format_amount(r['amount'])}",
            callback_data=f"delv:{r['id']}",
        )])
    kb_rows.append([InlineKeyboardButton("📊 بازگشت به گزارش", callback_data=f"rep:{jy}:{jm}")])

    if not rows:
        text = f"🛒 خریدهای <b>{e(title)}</b>\n\nاین ماه هنوز خالی است 🌱"
    else:
        total = round(sum(r["amount"] for r in rows), 2)
        hidden = len(rows) - min(len(rows), MAX_ITEMS_IN_DELETE_VIEW)
        text = (
            f"🛒 خریدهای <b>{e(title)}</b>\n\n"
            f"جمع: <b>{format_amount(total)}</b> {e(currency)} — {len(rows)} خرید\n\n"
            "برای حذف، روی خرید موردنظر بزنید:"
        )
        if hidden > 0:
            text += f"\n\n({hidden} خرید قدیمی‌تر در همین ماه از لیست بالا جا افتاده؛ با 📄 CSV قابل مشاهده است)"
    return text, InlineKeyboardMarkup(kb_rows)


def build_csv(chat_id: int, jy: int, jm: int) -> tuple[bytes, str, int, float, str]:
    """ساخت فایل CSV یک ماه؛ خروجی سازگار با Excel (BOM + بدون جداکننده)."""
    currency = db.get_currency(chat_id) or ""
    rows = db.get_month(chat_id, jy, jm)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ردیف", "تاریخ", f"مبلغ ({currency})", "دسته‌بندی", "توضیحات"])
    total = 0.0
    for i, r in enumerate(rows, 1):
        amount = round(r["amount"], 2)
        total += amount
        writer.writerow([
            i,
            f"{r['jyear']}/{r['jmonth']:02d}/{r['jday']:02d}",
            int(amount) if amount == int(amount) else amount,
            r["category"],
            r["description"],
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8"), f"{JALALI_MONTHS[jm - 1]} {jy}", len(rows), round(total, 2), currency


async def send_saved_confirmation(message, chat_id: int, expense_id: int,
                                  amount: float, desc: str, category: str) -> None:
    """پیام تأیید ثبت خرج + جمع این ماه + دکمه‌های حذف/تغییر دسته."""
    currency = db.get_currency(chat_id) or ""
    total, count = db.month_total(chat_id, *to_jalali(today_tehran())[:2])
    emoji = CATEGORY_EMOJI.get(category, "📦")
    text = (
        "✅ خرج ثبت شد!\n\n"
        f"🛍 {e(desc)}\n"
        f"💰 <b>{format_amount(amount)}</b> {e(currency)}\n"
        f"{emoji} دسته: {e(category)}\n"
        f"📅 {format_jalali(today_tehran())}\n\n"
        f"💰 جمع این ماه تا امروز: <b>{format_amount(round(total, 2))}</b> "
        f"{e(currency)} ({count} خرید)"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 حذف", callback_data=f"delc:{expense_id}"),
        InlineKeyboardButton("🔁 تغییر دسته", callback_data=f"recat:{expense_id}"),
    ]])
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ------------------- دستورها -------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not db.get_currency(chat_id):
        context.chat_data["state"] = "currency"
        await update.effective_message.reply_text(
            "👋 سلام! به ربات مدیریت خرج خوش اومدید.\n\n"
            "💱 اول از همه، واحد پول خودتون رو انتخاب کنید:",
            reply_markup=currency_keyboard(),
        )
        return
    context.chat_data["state"] = None
    context.chat_data.pop("pending", None)
    currency = db.get_currency(chat_id)
    await update.effective_message.reply_text(
        "👋 سلام! من ربات مدیریت خرج شما هستم.\n\n"
        f"💱 واحد پول فعلی: <b>{e(currency)}</b>\n\n"
        "🛍 ثبت سریع خرج: کافیه مبلغ و توضیح رو بفرستید؛ مثلاً:\n"
        "<code>250000 کافه</code>\n\n"
        "از دکمه‌های پایین هم می‌تونید استفاده کنید. راهنما: /help",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_currency(update, context):
        return
    # پشتیبانی از /add 250000 کافه
    if context.args:
        await handle_amount_and_desc(update, context, " ".join(context.args))
        return
    context.chat_data["state"] = "add_amount"
    await update.effective_message.reply_text(
        "➕ <b>ثبت خرج جدید</b>\n\n"
        "1️⃣ مبلغ را بنویسید:\n"
        "(مثلاً <code>250000</code> یا <code>1'250'000</code> — لغو: /cancel)",
        parse_mode=ParseMode.HTML,
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_currency(update, context):
        return
    context.chat_data["state"] = None
    context.chat_data.pop("pending", None)
    month = parse_month_arg(context.args) or to_jalali(today_tehran())[:2]
    text, kb = render_report(update.effective_chat.id, *month)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_total(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_currency(update, context):
        return
    context.chat_data["state"] = None
    context.chat_data.pop("pending", None)
    chat_id = update.effective_chat.id
    jy, jm, jd = to_jalali(today_tehran())
    total, count = db.month_total(chat_id, jy, jm)
    currency = db.get_currency(chat_id) or ""
    if count:
        text = (
            f"💰 خرج <b>{JALALI_MONTHS[jm - 1]} {jy}</b> تا امروز ({jd} {JALALI_MONTHS[jm - 1]}):\n\n"
            f"جمع: <b>{format_amount(round(total, 2))}</b> {e(currency)}\n"
            f"تعداد: {count} خرید\n"
            f"میانگین: {format_amount(round(total / count, 2))} {e(currency)}"
        )
    else:
        text = f"💰 از اول <b>{JALALI_MONTHS[jm - 1]} {jy}</b> تا امروز ({jd} {JALALI_MONTHS[jm - 1]}) هنوز خرجی ثبت نشده 🌱"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 گزارش کامل", callback_data=f"rep:{jy}:{jm}")]])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_currency(update, context):
        return
    context.chat_data["state"] = None
    context.chat_data.pop("pending", None)
    month = parse_month_arg(context.args) or to_jalali(today_tehran())[:2]
    text, kb = render_items(update.effective_chat.id, *month)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    row = db.last_expense(chat_id)
    if not row:
        await update.effective_message.reply_text("↩️ خرجی برای حذف وجود ندارد.")
        return
    db.delete_expense(row["id"])
    desc = row["description"] or NO_DESC
    await update.effective_message.reply_text(
        f"↩️ آخرین خرج حذف شد:\n"
        f"🛍 {e(truncate(desc, 40))} — <b>{format_amount(row['amount'])}</b> "
        f"{e(db.get_currency(chat_id) or '')} ({row['jday']} {JALALI_MONTHS[row['jmonth'] - 1]})",
        parse_mode=ParseMode.HTML,
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_currency(update, context):
        return
    context.chat_data["state"] = None
    if context.args and parse_month_arg(context.args) is None:
        await update.effective_message.reply_text("قالب درست: <code>/export 1405/08</code>", parse_mode=ParseMode.HTML)
        return
    jy, jm = parse_month_arg(context.args) or to_jalali(today_tehran())[:2]
    data, title, count, total, currency = build_csv(update.effective_chat.id, jy, jm)
    if count == 0:
        await update.effective_message.reply_text(f"📄 ماه «{title}» خالی است 🌱")
        return
    await update.effective_message.reply_document(
        document=data,
        filename=f"expenses-{jy}-{jm:02d}.csv",
        caption=(
            f"📄 خروجی <b>{e(title)}</b>\n"
            f"{count} خرید — جمع: <b>{format_amount(total)}</b> {e(currency)}"
        ),
        parse_mode=ParseMode.HTML,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    currency = db.get_currency(chat_id)
    if currency is None:
        if not await ensure_currency(update, context):
            return
        return
    context.chat_data["state"] = None
    context.chat_data.pop("pending", None)
    text = (
        "⚙️ <b>تنظیمات</b>\n\n"
        f"💱 واحد پول فعلی: <b>{e(currency)}</b>\n"
        f"🗄 تعداد کل خرج‌های ثبت‌شده: {db.count_expenses(chat_id)}\n"
        "📅 تقویم: شمسی | جداکنندهٔ ارقام: '"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💱 تغییر واحد پول", callback_data="setcur")],
        [InlineKeyboardButton("🧨 پاک‌کردن همهٔ داده‌ها", callback_data="delall:ask")],
    ])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["state"] = None
    context.chat_data.pop("pending", None)
    await update.effective_message.reply_text(
        "❓ <b>راهنما</b>\n\n"
        "➕ <b>ثبت خرج:</b>\n"
        "• سریع: مبلغ و توضیح را بفرستید؛ مثل <code>250000 کافه</code>\n"
        "• فقط مبلغ بفرستید تا توضیح پرسیده شود\n"
        "• مرحله‌ای: دکمهٔ «➕ ثبت خرج» یا دستور /add\n"
        "• جداکنندهٔ هزارگان آزاد است: <code>1'250'000</code> یا <code>1,250,000</code> یا ارقام فارسی\n\n"
        "📊 /report — گزارش کامل ماه (ناوبری ماه قبل/بعد با فلش‌ها)\n"
        "💰 /total — جمع خرج از اول ماه تا الان\n"
        "🛒 /items — لیست خریدهای ماه با امکان حذف\n"
        "↩️ /undo — حذف آخرین خرج\n"
        "📄 /export — خروجی CSV؛ مثل <code>/export 1405/08</code>\n"
        "⚙️ /settings — تغییر واحد پول، پاک‌سازی داده‌ها\n"
        "↩️ /cancel — لغو عملیات جاری\n\n"
        "📅 تقویم ربات شمسی است و اعداد با جداکنندهٔ «'» نمایش داده می‌شوند.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    had_state = context.chat_data.get("state") is not None
    context.chat_data["state"] = None
    context.chat_data.pop("pending", None)
    await update.effective_message.reply_text(
        "↩️ عملیات لغو شد." if had_state else "چیزی برای لغو نبود 🙂"
    )


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """رد کردن مرحلهٔ توضیح در ثبت مرحله‌ای."""
    if context.chat_data.get("state") == "add_desc":
        await ask_category(update, context, NO_DESC)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "دستور ناشناخته است 🤔 — راهنما: /help"
    )


# ------------------- جریان ثبت خرج -------------------

async def ask_category(update: Update, context: ContextTypes.DEFAULT_TYPE, desc: str) -> None:
    pending = context.chat_data.get("pending")
    if not pending:
        context.chat_data["state"] = None
        await update.effective_message.reply_text(
            "این عملیات منقضی شده ✋ — دوباره تلاش کنید؛ مثلاً: <code>250000 کافه</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    pending["description"] = desc
    context.chat_data["state"] = "add_category"
    amount = context.chat_data["pending"]["amount"]
    currency = db.get_currency(update.effective_chat.id) or ""
    await update.effective_message.reply_text(
        f"💰 مبلغ: <b>{format_amount(amount)}</b> {e(currency)}\n"
        f"🛍 توضیح: {e(desc)}\n\n"
        "3️⃣ دسته‌بندی را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=category_keyboard(lambda idx: f"cat:{idx}"),
    )


async def handle_amount_and_desc(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """ثبت سریع «مبلغ توضیح»؛ اگر توضیح نبود، پرسیده می‌شود."""
    parts = text.split(None, 1)
    amount = parse_amount(parts[0]) if parts else None
    if amount is None:
        await update.effective_message.reply_text(
            "متوجه نشدم 🤔\n"
            "برای ثبت خرج، مبلغ و توضیح را بفرستید؛ مثل: <code>250000 کافه</code>\n"
            "یا از دکمه‌های پایین استفاده کنید.",
            parse_mode=ParseMode.HTML,
        )
        return
    if len(parts) == 1:
        context.chat_data["pending"] = {"amount": amount}
        context.chat_data["state"] = "add_desc"
        await update.effective_message.reply_text(
            f"💰 مبلغ: <b>{format_amount(amount)}</b>\n\n"
            "2️⃣ توضیح خرید چیه؟\n"
            "(بدون توضیح: «-» یا /skip)",
            parse_mode=ParseMode.HTML,
        )
        return
    desc = parts[1].strip()[:MAX_DESC_LEN] or NO_DESC
    chat_id = update.effective_chat.id
    expense_id = db.add_expense(chat_id, amount, desc, QUICK_CATEGORY, today_tehran())
    await send_saved_confirmation(update.effective_message, chat_id, expense_id, amount, desc, QUICK_CATEGORY)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    state = context.chat_data.get("state")

    # دکمه‌های منو در هر وضعیتی اولویت دارند (و مثل لغو عملیات جاری عمل می‌کنند)
    if text in MENU_LABELS:
        context.chat_data["state"] = None
        context.chat_data.pop("pending", None)
        if text == MENU_ADD:
            await cmd_add(update, context)
        elif text == MENU_REPORT:
            await cmd_report(update, context)
        elif text == MENU_TOTAL:
            await cmd_total(update, context)
        elif text == MENU_ITEMS:
            await cmd_items(update, context)
        elif text == MENU_SETTINGS:
            await cmd_settings(update, context)
        else:
            await cmd_help(update, context)
        return

    if state == "currency_custom":
        currency = text[:MAX_CURRENCY_LEN]
        db.set_currency(update.effective_chat.id, currency)
        context.chat_data["state"] = None
        await update.effective_message.reply_text(
            f"✅ واحد پول شما تنظیم شد: <b>{e(currency)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if state == "add_amount":
        amount = parse_amount(text)
        if amount is not None:
            context.chat_data["pending"] = {"amount": amount}
            context.chat_data["state"] = "add_desc"
            await update.effective_message.reply_text(
                f"💰 مبلغ: <b>{format_amount(amount)}</b>\n\n"
                "2️⃣ توضیح خرید چیه؟\n(بدون توضیح: «-» یا /skip)",
                parse_mode=ParseMode.HTML,
            )
            return
        # «250000 کافه» در مرحلهٔ مبلغ = ثبت کامل سریع
        parts = text.split(None, 1)
        if len(parts) == 2 and parse_amount(parts[0]) is not None:
            context.chat_data["state"] = None
            await handle_amount_and_desc(update, context, text)
            return
        await update.effective_message.reply_text(
            "❗️ مبلغ نامعتبر است. فقط عدد بفرستید؛ مثل <code>250000</code>\n(لغو: /cancel)",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == "add_desc":
        desc = NO_DESC if text in DASH_REPLIES else text[:MAX_DESC_LEN]
        await ask_category(update, context, desc)
        return

    if state == "add_category":
        # به‌جای انتخاب دسته متن فرستاده شد؛ فرم قبلی را رها کن و مثل متن آزاد رفتار کن
        context.chat_data["state"] = None
        context.chat_data.pop("pending", None)

    await handle_amount_and_desc(update, context, text)


# ------------------- دکمه‌های شیشه‌ای -------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = update.effective_chat.id
    await query.answer()

    if data == "noop":
        await query.answer("همین ماه در حال نمایش است 🙂", show_alert=False)
        return

    # ---- واحد پول ----
    if data.startswith("cur:"):
        choice = data.split(":", 1)[1]
        if choice == "custom":
            context.chat_data["state"] = "currency_custom"
            await safe_edit(query.message,
                            "✍️ نام واحد پول را بنویسید:\n(مثلاً: تومان، دینار، افغانی، …)",
                            None)
            return
        try:
            idx = int(choice)
            currency = CURRENCY_PRESETS[idx]
        except (ValueError, IndexError):
            return
        db.set_currency(chat_id, currency)
        context.chat_data["state"] = None
        await safe_edit(query.message, f"✅ واحد پول شما: <b>{e(currency)}</b>", None)
        await query.message.reply_text(
            "🎉 همه‌چیز آماده است!\n\n"
            "🛍 ثبت سریع خرج: مبلغ و توضیح را بفرستید؛ مثل:\n"
            "<code>250000 کافه</code>\n\n"
            "راهنمای کامل: /help",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if data == "setcur":
        context.chat_data["state"] = "currency"
        await safe_edit(query.message, "💱 واحد پول جدید را انتخاب کنید:", currency_keyboard())
        return

    # ---- دسته‌بندی (ثبت مرحله‌ای) ----
    if data.startswith("cat:"):
        pending = context.chat_data.get("pending")
        if not pending:
            await query.answer("این درخواست قدیمی است؛ دوباره تلاش کنید ✋", show_alert=True)
            return
        try:
            emoji, category = CATEGORIES[int(data.split(":", 1)[1])]
        except (ValueError, IndexError):
            return
        context.chat_data["state"] = None
        context.chat_data.pop("pending", None)
        expense_id = db.add_expense(chat_id, pending["amount"], pending["description"],
                                    category, today_tehran())
        await safe_edit(query.message, f"{emoji} دستهٔ «{e(category)}» انتخاب شد.", None)
        await send_saved_confirmation(query.message, chat_id, expense_id,
                                      pending["amount"], pending["description"], category)
        return

    # ---- تغییر دستهٔ خرج ثبت‌شده ----
    if data.startswith("recat:"):
        row = db.get_expense(int(data.split(":", 1)[1]))
        if not row or row["chat_id"] != chat_id:
            await query.answer("این خرج پیدا نشد.", show_alert=True)
            return
        await safe_edit(
            query.message,
            f"🔁 دستهٔ جدید برای «{e(truncate(row['description'] or NO_DESC, 30))}» را انتخاب کنید:",
            category_keyboard(lambda idx: f"catset:{row['id']}:{idx}"),
        )
        return

    if data.startswith("catset:"):
        _, eid, idx = data.split(":", 2)
        row = db.get_expense(int(eid))
        if not row or row["chat_id"] != chat_id:
            await query.answer("این خرج پیدا نشد.", show_alert=True)
            return
        try:
            emoji, category = CATEGORIES[int(idx)]
        except (ValueError, IndexError):
            return
        db.set_category(row["id"], category)
        await safe_edit(query.message,
                        f"✅ دستهٔ «{e(truncate(row['description'] or NO_DESC, 30))}» "
                        f"به {emoji} {e(category)} تغییر کرد.",
                        None)
        return

    # ---- حذف ----
    if data.startswith("delc:"):
        row = db.get_expense(int(data.split(":", 1)[1]))
        if not row or row["chat_id"] != chat_id:
            await query.answer("این خرج پیدا نشد.", show_alert=True)
            return
        db.delete_expense(row["id"])
        await safe_edit(query.message,
                        f"🗑 حذف شد: {e(truncate(row['description'] or NO_DESC, 40))} — "
                        f"<b>{format_amount(row['amount'])}</b>",
                        None)
        return

    if data.startswith("delv:"):
        row = db.get_expense(int(data.split(":", 1)[1]))
        if not row or row["chat_id"] != chat_id:
            await query.answer("این خرج پیدا نشد.", show_alert=True)
            return
        db.delete_expense(row["id"])
        await query.answer("حذف شد ✅")
        text, kb = render_items(chat_id, row["jyear"], row["jmonth"])
        await safe_edit(query.message, text, kb)
        return

    # ---- پاک‌سازی کل داده‌ها ----
    if data == "delall:ask":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله، همه پاک شود", callback_data="delall:yes"),
            InlineKeyboardButton("❌ انصراف", callback_data="delall:no"),
        ]])
        await safe_edit(query.message,
                        "🧨 <b>مطمئنید؟</b>\nهمهٔ خرج‌های ثبت‌شدهٔ این چت برای همیشه حذف می‌شوند.",
                        kb)
        return

    if data == "delall:no":
        await safe_edit(query.message, "انصراف داده شد؛ داده‌ها دست‌نخورده ماندند ✅", None)
        return

    if data == "delall:yes":
        count = db.delete_all(chat_id)
        await safe_edit(query.message, f"🧨 {count} خرج با موفقیت پاک شد.", None)
        return

    # ---- ناوبری گزارش و لیست ----
    if data.startswith("rep:") or data.startswith("items:") or data.startswith("export:"):
        try:
            jy, jm = (int(x) for x in data.split(":", 1)[1].split(":"))
        except ValueError:
            return
        if not (1200 <= jy <= 1500 and 1 <= jm <= 12):
            await query.answer("ماه نامعتبر است.", show_alert=True)
            return
        if data.startswith("rep:"):
            text, kb = render_report(chat_id, jy, jm)
            await safe_edit(query.message, text, kb)
        elif data.startswith("items:"):
            text, kb = render_items(chat_id, jy, jm)
            await safe_edit(query.message, text, kb)
        else:
            payload, title, count, total, currency = build_csv(chat_id, jy, jm)
            if count == 0:
                await query.answer(f"ماه «{title}» خالی است 🌱", show_alert=True)
                return
            await query.message.reply_document(
                document=payload,
                filename=f"expenses-{jy}-{jm:02d}.csv",
                caption=(f"📄 خروجی <b>{e(title)}</b>\n"
                         f"{count} خرید — جمع: <b>{format_amount(total)}</b> {e(currency)}"),
                parse_mode=ParseMode.HTML,
            )
        return


# ------------------- راه‌اندازی -------------------

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "شروع و منوی اصلی"),
        BotCommand("add", "ثبت خرج جدید"),
        BotCommand("report", "گزارش ماهانه"),
        BotCommand("total", "جمع خرج این ماه"),
        BotCommand("items", "خریدهای این ماه"),
        BotCommand("undo", "حذف آخرین خرج"),
        BotCommand("export", "خروجی CSV؛ مثل /export 1405/08"),
        BotCommand("settings", "تنظیمات"),
        BotCommand("help", "راهنما"),
        BotCommand("cancel", "لغو عملیات جاری"),
    ])
    logger.info("ربات بالا آمد و فهرست دستورها ثبت شد.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("خطا در پردازش آپدیت", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id, "⚠️ خطای غیرمنتظره‌ای رخ داد؛ لطفاً دوباره تلاش کنید."
            )
    except Exception:  # noqa: BLE001 - در گزارش خطا نباید خطا داد
        pass


def load_env() -> None:
    """خواندن .env کنار bot.py (بدون وابستگی خارجی)."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    load_env()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("❌ BOT_TOKEN تنظیم نشده است. آن را در فایل .env قرار دهید (نمونه: .env.example)")

    db.init_db()
    jdatetime.date.today()  # اطمینان از سالم بودن کتابخانهٔ تقویم در آغاز کار

    app = ApplicationBuilder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("total", cmd_total))
    app.add_handler(CommandHandler("items", cmd_items))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))  # دستور ناشناخته
    app.add_error_handler(on_error)

    logger.info("شروع polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
