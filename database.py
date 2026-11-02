"""لایهٔ دیتابیس SQLite برای ربات مدیریت خرج.

داده‌ها به ازای هر «چت» ذخیره می‌شوند؛ بنابراین ربات هم در چت خصوصی و هم
در گروه کار می‌کند. ماه‌ها بر پایهٔ تقویم شمسی در ستون‌های jyear/jmonth
ذخیره می‌شوند تا گزارش‌گیری ماهانه ساده و سریع باشد.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from utils import TEHRAN, to_jalali

DEFAULT_DB = Path(__file__).with_name("expenses.db")
DB_PATH = Path(os.environ.get("EXPENSES_DB", str(DEFAULT_DB)))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    chat_id    INTEGER PRIMARY KEY,
    currency   TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    amount      REAL    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL,
    gdate       TEXT    NOT NULL,
    jyear       INTEGER NOT NULL,
    jmonth      INTEGER NOT NULL,
    jday        INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_expenses_chat_month
    ON expenses (chat_id, jyear, jmonth);
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return datetime.now(TEHRAN).isoformat(timespec="seconds")


def init_db() -> None:
    """ساخت جداول در صورت نبود."""
    with closing(_connect()) as con, con:
        con.executescript(_SCHEMA)


# ------------------- تنظیمات -------------------

def get_currency(chat_id: int) -> str | None:
    with closing(_connect()) as con:
        row = con.execute(
            "SELECT currency FROM settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["currency"] if row else None


def set_currency(chat_id: int, currency: str) -> None:
    with closing(_connect()) as con, con:
        con.execute(
            """INSERT INTO settings (chat_id, currency, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET currency = excluded.currency""",
            (chat_id, currency, _now()),
        )


# ------------------- خرج‌ها -------------------

def add_expense(chat_id: int, amount: float, description: str,
                category: str, gdate: date) -> int:
    """ثبت خرج و بازگرداندن شناسهٔ آن.

    تاریخ شمسی از تاریخ میلادی محاسبه و در ستون‌های جدا ذخیره می‌شود.
    """
    jy, jm, jd = to_jalali(gdate)
    with closing(_connect()) as con, con:
        cur = con.execute(
            """INSERT INTO expenses
                   (chat_id, amount, description, category,
                    gdate, jyear, jmonth, jday, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, amount, description, category,
             gdate.isoformat(), jy, jm, jd, _now()),
        )
        return int(cur.lastrowid)


def get_month(chat_id: int, jy: int, jm: int) -> list[sqlite3.Row]:
    """همهٔ خرج‌های یک ماه شمسی به ترتیب زمانی."""
    with closing(_connect()) as con:
        rows = con.execute(
            """SELECT * FROM expenses
               WHERE chat_id = ? AND jyear = ? AND jmonth = ?
               ORDER BY gdate, id""",
            (chat_id, jy, jm),
        ).fetchall()
    return rows


def month_total(chat_id: int, jy: int, jm: int) -> tuple[float, int]:
    """جمع مبلغ و تعداد خرج‌های یک ماه شمسی."""
    with closing(_connect()) as con:
        row = con.execute(
            """SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt
               FROM expenses
               WHERE chat_id = ? AND jyear = ? AND jmonth = ?""",
            (chat_id, jy, jm),
        ).fetchone()
    return float(row["total"]), int(row["cnt"])


def category_totals(chat_id: int, jy: int, jm: int) -> dict[str, tuple[float, int]]:
    """جمع و تعداد خرج‌های هر دسته در یک ماه، نزولی بر اساس مبلغ."""
    with closing(_connect()) as con:
        rows = con.execute(
            """SELECT category, SUM(amount) AS total, COUNT(*) AS cnt
               FROM expenses
               WHERE chat_id = ? AND jyear = ? AND jmonth = ?
               GROUP BY category
               ORDER BY total DESC""",
            (chat_id, jy, jm),
        ).fetchall()
    return {r["category"]: (float(r["total"]), int(r["cnt"])) for r in rows}


def get_expense(expense_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as con:
        return con.execute(
            "SELECT * FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()


def set_category(expense_id: int, category: str) -> None:
    with closing(_connect()) as con, con:
        con.execute(
            "UPDATE expenses SET category = ? WHERE id = ?", (category, expense_id)
        )


def delete_expense(expense_id: int) -> None:
    with closing(_connect()) as con, con:
        con.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))


def last_expense(chat_id: int) -> sqlite3.Row | None:
    """آخرین خرج ثبت‌شدهٔ چت (برای /undo)."""
    with closing(_connect()) as con:
        return con.execute(
            "SELECT * FROM expenses WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()


def delete_all(chat_id: int) -> int:
    """حذف همهٔ خرج‌های چت و بازگرداندن تعداد حذف‌شده‌ها."""
    with closing(_connect()) as con, con:
        count = con.execute(
            "SELECT COUNT(*) AS cnt FROM expenses WHERE chat_id = ?", (chat_id,)
        ).fetchone()["cnt"]
        con.execute("DELETE FROM expenses WHERE chat_id = ?", (chat_id,))
    return int(count)


def count_expenses(chat_id: int) -> int:
    with closing(_connect()) as con:
        row = con.execute(
            "SELECT COUNT(*) AS cnt FROM expenses WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return int(row["cnt"])
