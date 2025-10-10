import os
import re
import io
import csv
import sqlite3
import logging
import datetime as dt
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, ChatMember, InputFile
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# ============== Config & Globals ==============
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")              # <-- set this in Render env vars
TIMEZONE = os.getenv("TIMEZONE", "Asia/Phnom_Penh")

TZ = ZoneInfo(TIMEZONE)
DB_PATH = "aba_totals.sqlite"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aba-bot")

# Regex patterns catching common ABA formats:
USD_PATTERNS = [
    r"\$\s*([0-9][\d,]*(?:\.\d{1,2})?)",
    r"USD\s*([0-9][\d,]*(?:\.\d{1,2})?)",
    r"([0-9][\d,]*(?:\.\d{1,2})?)\s*USD",
]
KHR_PATTERNS = [
    r"[៛]\s*([0-9][\d,]*)",
    r"KHR\s*([0-9][\d,]*)",
    r"([0-9][\d,]*)\s*KHR",
]


# ============== Storage (SQLite) ==============
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS tx (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            msg_id INTEGER,
            ts TEXT,              -- ISO8601 in UTC
            currency TEXT,        -- USD / KHR
            amount REAL,
            raw TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            source_username TEXT   -- restrict counting to this sender if set
        )"""
    )
    con.commit()
    con.close()


def save_tx(chat_id: int, msg_id: int, ts_local: dt.datetime, currency: str, amount: float, raw: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO tx(chat_id,msg_id,ts,currency,amount,raw) VALUES(?,?,?,?,?,?)",
        (chat_id, msg_id, ts_local.astimezone(dt.timezone.utc).isoformat(), currency, amount, raw[:2000]),
    )
    con.commit()
    con.close()


def get_totals(chat_id: int, start_local: dt.datetime, end_local: dt.datetime):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """SELECT currency, SUM(amount) FROM tx
           WHERE chat_id=? AND ts>=? AND ts<?
           GROUP BY currency""",
        (
            chat_id,
            start_local.astimezone(dt.timezone.utc).isoformat(),
            end_local.astimezone(dt.timezone.utc).isoformat(),
        ),
    )
    rows = cur.fetchall()
    con.close()
    return {c: (s or 0) for c, s in rows}


def export_range_csv(chat_id: int, start_local: dt.datetime, end_local: dt.datetime) -> io.BytesIO:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """SELECT ts,currency,amount,raw FROM tx
           WHERE chat_id=? AND ts>=? AND ts<? ORDER BY ts ASC""",
        (
            chat_id,
            start_local.astimezone(dt.timezone.utc).isoformat(),
            end_local.astimezone(dt.timezone.utc).isoformat(),
        ),
    )
    rows = cur.fetchall()
    con.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp_utc", "currency", "amount", "snippet"])
    for ts, curcy, amt, raw in rows:
        writer.writerow([ts, curcy, amt, (raw or "").replace("\n", " ")[:250]])
    return io.BytesIO(buf.getvalue().encode("utf-8"))


# ============== Helpers ==============
def parse_amounts(text: str):
    amounts = []
    if not text:
        return amounts
    for pat in USD_PATTERNS:
        for m in re.findall(pat, text, flags=re.IGNORECASE):
            try:
                amounts.append(("USD", float(str(m).replace(",", ""))))
            except Exception:
                pass
    for pat in KHR_PATTERNS:
        for m in re.findall(pat, text, flags=re.IGNORECASE):
            try:
                amounts.append(("KHR", float(str(m).replace(",", ""))))
            except Exception:
                pass
    return amounts


def parse_hhmm(s: str):
    try:
        h, m = s.split(":")
        h = int(h)
        m = int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return None


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    return True


# ============== Handlers ==============
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not (msg and msg.text):
        return

    # Optional: only count messages from a configured source username
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT source_username FROM settings WHERE chat_id=?", (chat.id,))
    row = cur.fetchone()
    con.close()

    source_ok = True
    if row and row[0]:
        src = row[0].lstrip("@").lower()
        u = (msg.from_user.username or "").lower() if msg.from_user else ""
        source_ok = (u == src)
    if not source_ok:
        return

    amounts = parse_amounts(msg.text)
    if not amounts:
        return

    now_local = dt.datetime.now(TZ)
    for curcy, amt in amounts:
        save_tx(chat.id, msg.message_id, now_local, curcy, amt, msg.text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I total ABA-style amounts in this group.\n\n"
        "Commands:\n"
        "/today – totals for today\n"
        "/month – totals for this month\n"
        "/shift 1 | 2 | HH:MM HH:MM – totals for a shift or custom time today\n"
        "/exportcsv – export this month to CSV\n"
        "/setsource @username – only count messages from this sender\n"
        "/reset_today – admin only; clears today’s records"
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    now = dt.datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    totals = get_totals(chat.id, start, end)
    if not totals:
        await update.message.reply_text("Today: no totals yet.")
        return
    parts = [f"{k}: {v:,.2f}" if k == "USD" else f"{k}: {v:,.0f}" for k, v in totals.items()]
    await update.message.reply_text("Today ➜ " + " | ".join(parts))


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    now = dt.datetime.now(TZ)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    totals = get_totals(chat.id, start, next_month)
    if not totals:
        await update.message.reply_text("This month: no totals yet.")
        return
    parts = [f"{k}: {v:,.2f}" if k == "USD" else f"{k}: {v:,.0f}" for k, v in totals.items()]
    await update.message.reply_text("This month ➜ " + " | ".join(parts))


async def cmd_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
      /shift 1               (06:00–13:00)
      /shift 2               (13:00–20:00)
      /shift 06:00 13:00     (custom range for today)
    """
    chat = update.effective_chat
    now = dt.datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    args = [a.strip() for a in (context.args or [])]
    label = ""
    if len(args) == 1 and args[0] in ("1", "2"):
        if args[0] == "1":
            start = today.replace(hour=6)
            end = today.replace(hour=13)
            label = "Shift 1 (06:00–13:00)"
        else:
            start = today.replace(hour=13)
            end = today.replace(hour=20)
            label = "Shift 2 (13:00–20:00)"
    elif len(args) == 2 and parse_hhmm(args[0]) and parse_hhmm(args[1]):
        (sh, sm) = parse_hhmm(args[0])
        (eh, em) = parse_hhmm(args[1])
        start = today.replace(hour=sh, minute=sm)
        end = today.replace(hour=eh, minute=em)
        label = f"{args[0]}–{args[1]}"
    else:
        await update.message.reply_text("Usage: /shift 1 | /shift 2 | /shift HH:MM HH:MM")
        return

    totals = get_totals(chat.id, start, end)
    if not totals:
        await update.message.reply_text(f"No transactions found for_
