#!/usr/bin/env python3
import os
import re
import sqlite3
from datetime import datetime
import pytz
import configparser

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# Load bot token from config.ini
config = configparser.ConfigParser()
config.read("config.ini")
try:
    TOKEN = config["telegram"]["token"].strip()
except KeyError:
    raise RuntimeError("Missing [telegram] token in config.ini")

# Timezone for timestamps
IST = pytz.timezone("Asia/Kolkata")

def init_db(path: str = "balances.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
      CREATE TABLE IF NOT EXISTS balances (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    INTEGER NOT NULL,
        alias      TEXT    NOT NULL,
        bank       TEXT    NOT NULL,
        balance    REAL    NOT NULL,
        is_credit  INTEGER NOT NULL,
        timestamp  TEXT    NOT NULL
      )
    """)
    conn.commit()
    return conn

def format_indian(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    x = abs(amount)
    whole = int(x)
    frac = f"{x - whole:.2f}".split(".")[1]
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{sign}{s}.{frac}"

BALANCE_RE = re.compile(r"""
    ^\s*
    (?P<alias>[^\s_]+) _ (?P<bank>[^\s]+)   # alias_bank
    .*?
    (?P<amt>[\d,]+(?:\.\d+)?)
""", re.VERBOSE)

def parse_balance(text: str):
    m = BALANCE_RE.search(text)
    if not m:
        return None
    alias = m.group("alias")
    bank  = m.group("bank").lower()
    raw   = m.group("amt").replace(",", "")
    is_credit = 0
    if bank == "tmb":
        if "cr." in text.lower():
            is_credit = 1
        raw = re.sub(r"(?i)cr\.\s*", "", raw)
    if bank == "idbi":
        raw = re.sub(r"(?i)inr\s*", "", raw)
    try:
        amt = float(raw)
    except ValueError:
        return None
    return alias, bank, amt, is_credit

DB = init_db()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text    = update.message.text or ""
    parsed  = parse_balance(text)
    if not parsed:
        return
    alias, bank, amt, is_credit = parsed
    ts = datetime.now(IST).isoformat(timespec="seconds")
    c = DB.cursor()
    c.execute("""
      INSERT INTO balances (chat_id, alias, bank, balance, is_credit, timestamp)
      VALUES (?, ?, ?, ?, ?, ?)
    """, (chat_id, alias, bank, amt, is_credit, ts))
    DB.commit()

async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    c = DB.cursor()
    c.execute("""
      SELECT alias, bank, balance, is_credit
      FROM (
        SELECT *, ROW_NUMBER() OVER (
          PARTITION BY alias, bank
          ORDER BY timestamp DESC
        ) AS rn
        FROM balances
        WHERE chat_id=?
      )
      WHERE rn=1
      ORDER BY alias
    """, (chat_id,))
    rows = c.fetchall()
    if not rows:
        await update.message.reply_text("No balances stored yet.")
        return
    lines = []
    for alias, bank, bal, credit in rows:
        s = format_indian(bal)
        suffix = " Cr." if credit else ""
        lines.append(f"`{alias}_{bank}`  💰: ₹{s}{suffix}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def handle_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    c = DB.cursor()
    c.execute("""
      SELECT alias, bank, balance, is_credit, timestamp
      FROM balances
      WHERE chat_id=?
      ORDER BY timestamp DESC
      LIMIT 25
    """, (chat_id,))
    rows = c.fetchall()
    if not rows:
        await update.message.reply_text("No history yet.")
        return
    lines = []
    for alias, bank, bal, credit, ts in rows:
        s = format_indian(bal)
        suffix = " Cr." if credit else ""
        human_ts = datetime.fromisoformat(ts).strftime("%d %b %Y, %I:%M %p")
        lines.append(f"{human_ts} — `{alias}_{bank}`: ₹{s}{suffix}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("balance", handle_balance))
    app.add_handler(CommandHandler("history", handle_history))
    app.run_polling()

if __name__ == "__main__":
    main()
