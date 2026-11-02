"""ابزارهای مشترک ربات: تقویم شمسی، قالب‌بندی اعداد و پارس مبلغ.

جداکنندهٔ هزارگان طبق نیاز پروژه، کاراکتر «'» است و ارقام از راست
هر سه رقم یک گروه می‌شوند؛ مثل 1'234'567.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import jdatetime

TEHRAN = ZoneInfo("Asia/Tehran")

JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

# دسته‌بندی‌های پیش‌فرض خرج (ایموجی، نام)
CATEGORIES = [
    ("🍔", "خوراک"),
    ("🛒", "خرید روزمره"),
    ("🏠", "خانه و اجاره"),
    ("🧾", "قبض و شارژ"),
    ("🚕", "حمل‌ونقل"),
    ("🎮", "سرگرمی"),
    ("💊", "سلامت و درمان"),
    ("📚", "آموزش"),
    ("🎁", "هدیه و کمک"),
    ("📦", "سایر"),
]

CATEGORY_EMOJI = {name: emoji for emoji, name in CATEGORIES}

# نشانهٔ جهت راست‌به‌چپ (نامرئی) برای راست‌چین‌کردن پیام‌ها در تلگرام
RLM = "\u200f"

# تبدیل ارقام فارسی و عربی به لاتین
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# جداکننده‌های هزارگانی که هنگام پارس مبلغ حذف می‌شوند
_THOUSANDS_STRIP = str.maketrans("", "", "'’٬،,\u066c \u00a0\u200f\u200e\t")


def today_tehran() -> date:
    """تاریخ امروز به وقت تهران."""
    return datetime.now(TEHRAN).date()


def to_jalali(d: date) -> tuple[int, int, int]:
    """تبدیل تاریخ میلادی به (سال، ماه، روز) شمسی."""
    j = jdatetime.date.fromgregorian(date=d)
    return j.year, j.month, j.day


def format_jalali(d: date) -> str:
    """قالب خوانای تاریخ شمسی؛ مثل «11 آبان 1405»."""
    jy, jm, jd = to_jalali(d)
    return f"{jd} {JALALI_MONTHS[jm - 1]} {jy}"


def normalize_digits(text: str) -> str:
    """تبدیل ارقام فارسی/عربی به لاتین."""
    return str(text).translate(_DIGIT_MAP)


def parse_amount(raw: str) -> float | None:
    """تبدیل متن مبلغ به عدد.

    ارقام فارسی/عربی، جداکننده‌های هزارگان (' , ٬ ،) و فاصله‌ها پذیرفته
    می‌شوند؛ ممیز اعشار «.» یا «٫» است. نامعتبر، صفر یا منفی → None.
    """
    s = normalize_digits(raw).translate(_THOUSANDS_STRIP).replace("٫", ".").strip()
    if not s or s.count(".") > 1:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    if not 0 < value <= 1e15:
        return None
    return value


def format_amount(value: float) -> str:
    """قالب‌بندی مبلغ با جداکنندهٔ «'» از راست؛ مثل 2'500'000.

    بخش اعشار در صورت وجود با حداکثر دو رقم و بدون صفرهای انتهایی نمایش
    داده می‌شود: 1234.5 → 1'234.5
    """
    value = round(float(value), 2)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value == int(value):
        int_part, frac = int(value), ""
    else:
        text = f"{value:.2f}"
        int_raw, _, frac_raw = text.partition(".")
        int_part, frac = int(int_raw), "." + frac_raw.rstrip("0")
    grouped = f"{int_part:,}".replace(",", "'")
    return f"{sign}{grouped}{frac}"


def rtl(text: str) -> str:
    """تضمین نمایش راست‌به‌چپ پیام در تلگرام.

    تلگرام جهت پاراگراف را از اولین نویسهٔ قوی تعیین می‌کند؛ سطرهایی که با
    ایموجی، عدد یا علامت شروع می‌شوند چپ‌چین می‌شوند و اعداد با
    جداکنندهٔ ' به‌هم می‌ریزند. با گذاشتن نشانهٔ نامرئی RLM در ابتدای پیام
    و ابتدای هر سطر، کل پیام راست‌چین و اعداد خوانا می‌شوند.
    """
    return RLM + f"\n{RLM}".join(text.split("\n"))


def truncate(text: str, limit: int) -> str:
    """کوتاه‌کردن متن بلند با سه‌نقطه."""
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
