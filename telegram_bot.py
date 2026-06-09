#!/usr/bin/env python3
"""
telegram_bot.py
~~~~~~~~~~~~~~~
Commands:
  /start  — show menu + saved account count
  /gen    — generate accounts (sequential questions)
  /stop   — stop generation
  /status — progress + saved count
"""

from __future__ import annotations

import os
import sys
import threading
import json

import telebot
from telebot import types

import config.settings as settings
from core.generator import worker

# ── Bot Token ─────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8778151847:AAFFV7orwS4Pf9BYL0WnBib943S1EdrNsLk").strip()
if not BOT_TOKEN:
    print("❌  BOT_TOKEN not set.")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ── Shared state ──────────────────────────────────────────────────────────────

_gen_threads: list[threading.Thread] = []
_gen_lock = threading.Lock()

REGIONS = sorted(r for r in settings.REGION_LANG if r != "BR")

_sessions: dict[int, dict] = {}


def _new_session() -> dict:
    return {
        "step": "region",
        "region": None,
        "is_ghost": False,
        "count": 0,
        "name": "",
        "password": "",
        "threads": 1,
    }


def _cancel_session(chat_id: int):
    _sessions.pop(chat_id, None)


# ── Account file helpers ──────────────────────────────────────────────────────

def _account_folders() -> list[tuple[str, str]]:
    return [
        ("Normal",   settings.ACCOUNTS_FOLDER),
        ("Rare",     settings.RARE_ACCOUNTS_FOLDER),
        ("Couples",  settings.COUPLES_ACCOUNTS_FOLDER),
        ("Ghost",    settings.GHOST_ACCOUNTS_FOLDER),
    ]


def _count_saved_accounts() -> dict[str, int]:
    result = {}
    for label, folder in _account_folders():
        count = 0
        if os.path.exists(folder):
            for f in os.listdir(folder):
                if f.endswith(".json"):
                    try:
                        with open(os.path.join(folder, f), "r", encoding="utf-8") as fh:
                            data = json.load(fh)
                            if isinstance(data, list):
                                count += len(data)
                    except Exception:
                        pass
        result[label] = count
    return result


def _send_account_files(chat_id: int):
    any_sent = False
    for label, folder in _account_folders():
        if not os.path.exists(folder):
            continue
        for fname in os.listdir(folder):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(folder, fname)
            try:
                size = os.path.getsize(fpath)
                if size < 5:
                    continue
                with open(fpath, "rb") as f:
                    bot.send_document(
                        chat_id,
                        f,
                        visible_file_name=fname,
                        caption=f"📁 {label}: {fname}",
                    )
                any_sent = True
            except Exception as e:
                bot.send_message(chat_id, f"⚠️ Could not send {fname}: {e}")

    if not any_sent:
        bot.send_message(chat_id, "📭 No account files found to send.")


# ── Running check ─────────────────────────────────────────────────────────────

def _is_running() -> bool:
    return any(t.is_alive() for t in _gen_threads)


def _reset_counters(count: int):
    settings.EXIT_FLAG        = False
    settings.SUCCESS_COUNTER  = 0
    settings.INFLIGHT_COUNTER = 0
    settings.TARGET_ACCOUNTS  = count
    settings.RARE_COUNTER     = 0
    settings.COUPLES_COUNTER  = 0


# ── /start ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(msg: types.Message):
    counts = _count_saved_accounts()
    total  = sum(counts.values())
    state  = "🟢 Running" if _is_running() else "⚫ Idle"

    lines = [
        "💀 TSun FF Generator Bot",
        "",
        f"Status: {state}",
        "",
        "📁 Saved accounts:",
        f"  Normal  : {counts['Normal']}",
        f"  Rare    : {counts['Rare']}",
        f"  Couples : {counts['Couples']}",
        f"  Ghost   : {counts['Ghost']}",
        f"  Total   : {total}",
        "",
        "Commands:",
        "  /gen    — generate accounts",
        "  /stop   — stop generation",
        "  /status — progress + count",
    ]
    bot.send_message(msg.chat.id, "\n".join(lines))


# ── /gen — sequential questions ───────────────────────────────────────────────

@bot.message_handler(commands=["gen"])
def cmd_gen(msg: types.Message):
    chat_id = msg.chat.id

    if _is_running():
        bot.send_message(chat_id, "⚠️ Generation already running.\nUse /stop first.")
        return

    _sessions[chat_id] = _new_session()
    _ask_region(chat_id)


def _ask_region(chat_id: int):
    lines = ["🌍 Choose region:\n"]
    for i, r in enumerate(REGIONS, 1):
        lines.append(f"  {i}) {r} ({settings.REGION_LANG[r]})")
    lines.append(f"  {len(REGIONS)+1}) GHOST Mode")
    lines.append("\nSend the number:")
    bot.send_message(chat_id, "\n".join(lines))


def _ask_count(chat_id: int):
    bot.send_message(chat_id, "🎯 How many accounts to generate?")


def _ask_name(chat_id: int):
    bot.send_message(chat_id, "👤 Enter account name prefix:")


def _ask_password(chat_id: int):
    bot.send_message(chat_id, "🔑 Enter password prefix:")


def _ask_threads(chat_id: int):
    bot.send_message(chat_id, "🧵 How many threads? (1-10, recommended: 2)")


# ── Text handler — process steps ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.chat.id in _sessions and not m.text.startswith("/"))
def handle_step(msg: types.Message):
    chat_id = msg.chat.id
    text    = msg.text.strip()
    s       = _sessions[chat_id]
    step    = s["step"]

    if step == "region":
        chosen   = None
        is_ghost = False
        if text.isdigit():
            n = int(text)
            if 1 <= n <= len(REGIONS):
                chosen   = REGIONS[n - 1]
                is_ghost = False
            elif n == len(REGIONS) + 1:
                chosen   = "BR"
                is_ghost = True
            else:
                bot.send_message(chat_id, "❌ Invalid number. Try again:")
                return
        elif text.upper() in REGIONS:
            chosen   = text.upper()
            is_ghost = False
        elif text.upper() == "GHOST":
            chosen   = "BR"
            is_ghost = True
        else:
            bot.send_message(chat_id, "❌ Unknown region. Send a number:")
            return
        s["region"]   = chosen
        s["is_ghost"] = is_ghost
        s["step"]     = "count"
        _ask_count(chat_id)

    elif step == "count":
        if not text.isdigit() or int(text) < 1:
            bot.send_message(chat_id, "❌ Enter a valid number (min 1):")
            return
        s["count"] = int(text)
        s["step"]  = "name"
        _ask_name(chat_id)

    elif step == "name":
        if not text:
            bot.send_message(chat_id, "❌ Name cannot be empty:")
            return
        s["name"] = text
        s["step"] = "password"
        _ask_password(chat_id)

    elif step == "password":
        if not text:
            bot.send_message(chat_id, "❌ Password cannot be empty:")
            return
        s["password"] = text
        s["step"]     = "threads"
        _ask_threads(chat_id)

    elif step == "threads":
        if not text.isdigit() or not (1 <= int(text) <= 10):
            bot.send_message(chat_id, "❌ Enter a number between 1 and 10:")
            return
        s["threads"] = int(text)
        _start_generation(chat_id, s)


# ── Start generation ──────────────────────────────────────────────────────────

def _start_generation(chat_id: int, s: dict):
    _cancel_session(chat_id)

    region   = s["region"]
    is_ghost = s["is_ghost"]
    count    = s["count"]
    name     = s["name"]
    password = s["password"]
    threads  = s["threads"]

    mode = "GHOST Mode" if is_ghost else f"{region} ({settings.REGION_LANG.get(region, '')})"

    bot.send_message(
        chat_id,
        f"🚀 Starting generation...\n\n"
        f"🌍 Region  : {mode}\n"
        f"🎯 Target  : {count}\n"
        f"👤 Name    : {name}\n"
        f"🔑 Password: {password}\n"
        f"🧵 Threads : {threads}"
    )

    with _gen_lock:
        _gen_threads.clear()
        _reset_counters(count)

        actual_region = "BR" if is_ghost else region
        for i in range(1, threads + 1):
            t = threading.Thread(
                target=worker,
                args=(actual_region, name, password, count, i, is_ghost, False),
                daemon=True,
            )
            t.start()
            _gen_threads.append(t)

    def _monitor():
        for t in _gen_threads:
            t.join()

        done    = settings.SUCCESS_COUNTER
        rare    = settings.RARE_COUNTER
        couples = settings.COUPLES_COUNTER
        counts  = _count_saved_accounts()
        total   = sum(counts.values())

        bot.send_message(
            chat_id,
            f"✅ Generation finished!\n\n"
            f"📊 Generated : {done}/{count}\n"
            f"💎 Rare      : {rare}\n"
            f"💑 Couples   : {couples}\n\n"
            f"📁 Total saved: {total}\n"
            f"  Normal  : {counts['Normal']}\n"
            f"  Rare    : {counts['Rare']}\n"
            f"  Couples : {counts['Couples']}\n"
            f"  Ghost   : {counts['Ghost']}\n\n"
            f"📤 Sending account files..."
        )

        _send_account_files(chat_id)

    threading.Thread(target=_monitor, daemon=True).start()


# ── /stop ─────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["stop"])
def cmd_stop(msg: types.Message):
    chat_id = msg.chat.id
    _cancel_session(chat_id)

    if not _is_running():
        bot.send_message(chat_id, "ℹ️ No generation is currently running.")
        return

    settings.EXIT_FLAG = True
    bot.send_message(chat_id, "🛑 Stop signal sent. Generation will halt shortly.")


# ── /status ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["status"])
def cmd_status(msg: types.Message):
    done    = settings.SUCCESS_COUNTER
    total   = settings.TARGET_ACCOUNTS
    rare    = settings.RARE_COUNTER
    couples = settings.COUPLES_COUNTER
    counts  = _count_saved_accounts()
    saved   = sum(counts.values())
    state   = "🟢 Running" if _is_running() else "⚫ Idle"

    bot.send_message(
        msg.chat.id,
        f"{state}\n\n"
        f"📊 Session  : {done}/{total}\n"
        f"💎 Rare     : {rare}\n"
        f"💑 Couples  : {couples}\n\n"
        f"📁 Total saved: {saved}\n"
        f"  Normal  : {counts['Normal']}\n"
        f"  Rare    : {counts['Rare']}\n"
        f"  Couples : {counts['Couples']}\n"
        f"  Ghost   : {counts['Ghost']}"
    )


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("✅ Bot started. Commands: /start  /gen  /stop  /status")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
