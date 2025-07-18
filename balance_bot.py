#!/usr/bin/env python3

import os
import re
import sqlite3
from datetime import datetime
import pytz
import configparser                   # ← new
from telegram import Update, ParseMode
from telegram.ext import Updater, MessageHandler, CommandHandler, Filters, CallbackContext
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
    """Initialize SQLite DB and return the connection."""
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
    """
    Format a float into Indian-style comma grouping with two decimals,
    e.g. 153834.03 → '1,53,834.03'
    """
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

# Regex to capture "<alias>_<bank> ... <amount>"
BALANCE_RE = re.compile(r"""
    ^\s*
    (?P<alias>[^\s_]+) _ (?P<bank>[^\s]+)   # alias_bank
    .*?
    (?P<amt>[\d,]+(?:\.\d+)?)
""", re.VERBOSE)

def parse_balance(text: str):
    """
    Parse a line of text for alias_bank and amount.
    Returns (alias, bank, amount: float, is_credit: int) or None.
    """
    m = BALANCE_RE.search(text)
    if not m:
        return None

    alias = m.group("alias")
    bank  = m.group("bank").lower()
    raw   = m.group("amt").replace(",", "")

    is_credit = 0
    # Handle TMB “Cr. ”
    if bank == "tmb":
        if "cr." in text.lower():
            is_credit = 1
        raw = re.sub(r"(?i)cr\.\s*", "", raw)
    # Handle IDBI “INR ”
    if bank == "idbi":
        raw = re.sub(r"(?i)inr\s*", "", raw)

    try:
        amt = float(raw)
    except ValueError:
        return None

    return alias, bank, amt, is_credit

# Initialize global DB connection
DB = init_db()

def handle_message(update: Update, context: CallbackContext):
    """On every text message, try parsing a balance and storing it."""
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

def handle_balance(update: Update, context: CallbackContext):
    """Show the latest balance for each alias in this chat."""
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
        update.message.reply_text("No balances stored yet.")
        return

    lines = []
    for alias, bank, bal, credit in rows:
        s = format_indian(bal)
        suffix = " Cr." if credit else ""
        lines.append(f"`{alias}_{bank}`  💰: ₹{s}{suffix}")

    update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN
    )

def handle_history(update: Update, context: CallbackContext):
    """Show the last 25 balance updates in this chat."""
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
        update.message.reply_text("No history yet.")
        return

    lines = []
    for alias, bank, bal, credit, ts in rows:
        s = format_indian(bal)
        suffix = " Cr." if credit else ""
        human_ts = datetime.fromisoformat(ts).strftime("%d %b %Y, %I:%M %p")
        lines.append(f"{human_ts} — `{alias}_{bank}`: ₹{s}{suffix}")

    update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN
    )

def main():
    """Start the bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set the TELEGRAM_BOT_TOKEN environment variable")

    updater = Updater(token, use_context=True)
    dp = updater.dispatcher

    # Listen for all text (to parse balances) and commands
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_handler(CommandHandler("balance", handle_balance))
    dp.add_handler(CommandHandler("history", handle_history))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
