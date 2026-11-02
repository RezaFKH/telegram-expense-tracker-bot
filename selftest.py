"""تست‌های سریع بدون وابستگی به تست‌فریمورک — اجرا: python selftest.py"""

from datetime import date

from utils import format_amount, format_jalali, parse_amount, to_jalali

CASES = [
    # جداکنندهٔ هزارگان: هر سه رقم از راست با '
    (format_amount(2500000), "2'500'000"),
    (format_amount(1234567), "1'234'567"),
    (format_amount(987654321), "987'654'321"),
    (format_amount(99), "99"),
    (format_amount(1234.5), "1'234.5"),
    (format_amount(1234.56), "1'234.56"),
    (format_amount(350000.00000000006), "350'000"),  # گرد کردن خطای ممیز شناور
    # پارس مبلغ: ارقام فارسی/عربی و جداکننده‌های گوناگون
    (parse_amount("۱'۲۵۰'۰۰۰"), 1250000.0),
    (parse_amount("2,500,000"), 2500000.0),
    (parse_amount("۲۵٬۰۰۰"), 25000.0),
    (parse_amount("۱۲۳"), 123.0),
    (parse_amount("12.50"), 12.5),
    (parse_amount("۱۲٫۵"), 12.5),
    (parse_amount(" 7 500 "), 7500.0),
    (parse_amount("abc"), None),
    (parse_amount("-5"), None),
    (parse_amount("0"), None),
    (parse_amount("1.2.3"), None),
    # تقویم شمسی
    (to_jalali(date(2026, 11, 2)), (1405, 8, 11)),
    (to_jalali(date(2024, 3, 20)), (1403, 1, 1)),   # نوروز 1403
    (to_jalali(date(2025, 3, 21)), (1404, 1, 1)),   # نوروز 1404
    (to_jalali(date(2026, 3, 21)), (1405, 1, 1)),   # نوروز 1405
    (to_jalali(date(2026, 3, 20)), (1404, 12, 29)),
    (format_jalali(date(2026, 11, 2)), "11 آبان 1405"),
]

for i, (got, want) in enumerate(CASES, 1):
    assert got == want, f"case {i}: {got!r} != {want!r}"

print(f"✅ {len(CASES)} تست با موفقیت پاس شد.")
