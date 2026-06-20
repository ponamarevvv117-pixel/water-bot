#!/usr/bin/env python3
import logging
import os
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

TOKEN = os.environ["BOT_TOKEN"]
MSK = timezone(timedelta(hours=3))

_db_dir = os.environ.get("DB_PATH", str(Path(__file__).parent))
DB_PATH = Path(_db_dir) / "water.db"

APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── База данных ─────────────────────────────────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date    TEXT    NOT NULL,
                ml      INTEGER NOT NULL,
                time    TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS goals (
                user_id INTEGER PRIMARY KEY,
                goal    INTEGER NOT NULL DEFAULT 2000
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                name    TEXT
            );
        """)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT")
        except Exception:
            pass

# ── Работа с данными ────────────────────────────────────────────────
def today_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")

def get_entries(user_id: int, date: str | None = None) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, ml, time FROM entries WHERE user_id=? AND date=? ORDER BY id",
            (user_id, date or today_msk()),
        ).fetchall()
    return [dict(r) for r in rows]

def add_entry(user_id: int, ml: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO entries (user_id, date, ml, time) VALUES (?, ?, ?, ?)",
            (user_id, today_msk(), ml, datetime.now(MSK).strftime("%H:%M")),
        )

def get_goal(user_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT goal FROM goals WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["goal"] if row else 2000

def save_goal(user_id: int, goal: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO goals (user_id, goal) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET goal=excluded.goal",
            (user_id, goal),
        )

def register_user(user_id: int, chat_id: int, name: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, chat_id, name) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, name=excluded.name",
            (user_id, chat_id, name),
        )

def get_user_name(user_id: int) -> str:
    with db() as conn:
        row = conn.execute("SELECT name FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row["name"] if row and row["name"] else str(user_id)

def all_users() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT user_id, chat_id, name FROM users"
        ).fetchall()]

def delete_last_entry(user_id: int) -> int | None:
    with db() as conn:
        row = conn.execute(
            "SELECT id, ml FROM entries WHERE user_id=? AND date=? ORDER BY id DESC LIMIT 1",
            (user_id, today_msk()),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM entries WHERE id=?", (row["id"],))
            return row["ml"]
    return None

# ── Google Sheets (через Apps Script) ───────────────────────────────
def sync_to_sheet(user_id: int, date: str):
    if not APPS_SCRIPT_URL:
        return
    try:
        total    = sum(e["ml"] for e in get_entries(user_id, date))
        name     = get_user_name(user_id)
        date_fmt = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
        requests.post(
            APPS_SCRIPT_URL,
            json={"user_id": str(user_id), "name": name, "date": date_fmt, "total": total},
            timeout=10,
            allow_redirects=True,
        )
        logger.info("Sheets: %s %s → %d мл", name, date_fmt, total)
    except Exception as e:
        logger.warning("Sheets error: %s", e)

async def async_sync_to_sheet(user_id: int, date: str):
    import asyncio
    await asyncio.get_event_loop().run_in_executor(None, sync_to_sheet, user_id, date)

# ── Вспомогалки для UI ──────────────────────────────────────────────
def make_bar(total: int, goal: int) -> str:
    pct    = min(100, total * 100 // goal)
    filled = pct // 10
    return "▓" * filled + "░" * (10 - filled)

def build_status(user_id: int, date: str | None = None, added_ml: int | None = None) -> str:
    entries = get_entries(user_id, date)
    goal    = get_goal(user_id)
    total   = sum(e["ml"] for e in entries)
    pct     = min(100, total * 100 // goal)
    remain  = max(0, goal - total)
    bar     = make_bar(total, goal)

    lines = [
        f"`{bar}` {pct}%",
        f"Выпито: *{total} мл* из {goal} мл",
        f"Осталось: *{remain} мл*",
    ]
    if total >= goal:
        lines.append("\n🎉 *Норма выполнена!*")
    if added_ml:
        lines.append(f"\n✅ Добавлено *{added_ml} мл*")
    return "\n".join(lines)

def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💧 150мл", callback_data="add:150"),
            InlineKeyboardButton("💧 200мл", callback_data="add:200"),
            InlineKeyboardButton("💧 300мл", callback_data="add:300"),
        ],
        [
            InlineKeyboardButton("💧 500мл", callback_data="add:500"),
            InlineKeyboardButton("💧 700мл", callback_data="add:700"),
        ],
        [
            InlineKeyboardButton("✏️ Своё количество", callback_data="custom"),
            InlineKeyboardButton("↩️ Отменить последнее", callback_data="undo"),
        ],
        [
            InlineKeyboardButton("📅 История", callback_data="history"),
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("⚙️ Изменить норму", callback_data="set_goal"),
            InlineKeyboardButton("🔄 Обновить",       callback_data="refresh"),
        ],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back")]])

# ── /start ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name or update.effective_user.username or str(uid)
    register_user(uid, update.effective_chat.id, name)
    await update.message.reply_text(
        "💧 *Трекер воды*\n\n" + build_status(uid),
        reply_markup=main_kb(),
        parse_mode="Markdown",
    )

# ── Обработчик кнопок ───────────────────────────────────────────────
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    data = query.data

    if data.startswith("add:"):
        ml = int(data.split(":")[1])
        add_entry(uid, ml)
        await query.edit_message_text(
            "💧 *Трекер воды*\n\n" + build_status(uid, added_ml=ml),
            reply_markup=main_kb(),
            parse_mode="Markdown",
        )
        await async_sync_to_sheet(uid, today_msk())

    elif data == "undo":
        removed_ml = delete_last_entry(uid)
        if removed_ml:
            msg = "💧 *Трекер воды*\n\n" + build_status(uid) + f"\n\n↩️ Отменено: *{removed_ml} мл*"
        else:
            msg = "💧 *Трекер воды*\n\n" + build_status(uid) + "\n\n❌ Нечего отменять"
        await query.edit_message_text(msg, reply_markup=main_kb(), parse_mode="Markdown")
        if removed_ml:
            await async_sync_to_sheet(uid, today_msk())

    elif data == "custom":
        ctx.user_data["waiting"] = "ml"
        await query.edit_message_text("✏️ Введи количество мл (1–2000):", reply_markup=cancel_kb())

    elif data in ("refresh", "back"):
        ctx.user_data.pop("waiting", None)
        await query.edit_message_text(
            "💧 *Трекер воды*\n\n" + build_status(uid),
            reply_markup=main_kb(),
            parse_mode="Markdown",
        )

    elif data == "history":
        goal  = get_goal(uid)
        lines = ["📅 *История за 7 дней*\n"]
        for i in range(6, -1, -1):
            d     = (datetime.now(MSK) - timedelta(days=i)).strftime("%Y-%m-%d")
            total = sum(e["ml"] for e in get_entries(uid, d))
            pct   = min(100, total * 100 // goal)
            label = (
                "Сегодня" if i == 0 else
                "Вчера"   if i == 1 else
                (datetime.now(MSK) - timedelta(days=i)).strftime("%d.%m")
            )
            icon = "✅" if total >= goal else ("📍" if i == 0 else "❌")
            lines.append(f"{icon} *{label}*: {total} мл ({pct}%)\n`{make_bar(total, goal)}`")
        await query.edit_message_text("\n".join(lines), reply_markup=back_kb(), parse_mode="Markdown")

    elif data == "stats":
        goal   = get_goal(uid)
        totals = [
            sum(e["ml"] for e in get_entries(uid, (datetime.now(MSK) - timedelta(days=i)).strftime("%Y-%m-%d")))
            for i in range(7)
        ]
        avg    = sum(totals) // 7
        best   = max(totals)
        streak = 0
        for i in range(365):
            d = (datetime.now(MSK) - timedelta(days=i)).strftime("%Y-%m-%d")
            if sum(e["ml"] for e in get_entries(uid, d)) >= goal:
                streak += 1
            else:
                break
        await query.edit_message_text(
            f"📊 *Статистика*\n\n"
            f"🔥 Серия: *{streak} дней подряд*\n"
            f"📈 Среднее (7 дней): *{avg} мл*\n"
            f"🏆 Лучший день: *{best} мл*\n"
            f"🎯 Норма: *{goal} мл*",
            reply_markup=back_kb(),
            parse_mode="Markdown",
        )

    elif data == "set_goal":
        ctx.user_data["waiting"] = "goal"
        await query.edit_message_text(
            f"⚙️ Текущая норма: *{get_goal(uid)} мл*\n\nВведи новую норму (500–5000 мл):",
            reply_markup=cancel_kb(),
            parse_mode="Markdown",
        )

# ── Обработчик текстовых сообщений ─────────────────────────────────
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    waiting = ctx.user_data.get("waiting")
    if not waiting:
        return

    uid = update.effective_user.id
    raw = update.message.text.strip()

    if waiting == "ml":
        try:
            ml = int(raw)
            assert 1 <= ml <= 2000
        except (ValueError, AssertionError):
            await update.message.reply_text("❌ Введи число от 1 до 2000")
            return
        ctx.user_data.pop("waiting")
        add_entry(uid, ml)
        await update.message.reply_text(
            "💧 *Трекер воды*\n\n" + build_status(uid, added_ml=ml),
            reply_markup=main_kb(),
            parse_mode="Markdown",
        )
        await async_sync_to_sheet(uid, today_msk())

    elif waiting == "goal":
        try:
            goal = int(raw)
            assert 500 <= goal <= 5000
        except (ValueError, AssertionError):
            await update.message.reply_text("❌ Введи число от 500 до 5000")
            return
        ctx.user_data.pop("waiting")
        save_goal(uid, goal)
        await update.message.reply_text(
            f"✅ Норма: *{goal} мл*\n\n" + build_status(uid),
            reply_markup=main_kb(),
            parse_mode="Markdown",
        )

# ── Напоминалка каждый час ──────────────────────────────────────────
async def reminder_job(ctx: ContextTypes.DEFAULT_TYPE):
    now   = datetime.now(MSK)
    users = all_users()
    logger.info("reminder_job: %02d:%02d МСК, пользователей=%d", now.hour, now.minute, len(users))

    for user in users:
        uid     = user["user_id"]
        chat_id = user["chat_id"]
        goal    = get_goal(uid)
        total   = sum(e["ml"] for e in get_entries(uid))
        remain  = max(0, goal - total)
        pct     = total * 100 // goal

        if total >= goal:
            logger.info("reminder_job: user %s норма выполнена (%d мл), пропускаем", uid, total)
            continue

        logger.info("reminder_job: отправляем user %s (%d мл из %d)", uid, total, goal)
        try:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ *Не забудь попить воды!*\n\n"
                    f"`{make_bar(total, goal)}` {pct}%\n"
                    f"Выпито: {total} мл из {goal} мл\n"
                    f"Осталось: *{remain} мл*"
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💧 150мл", callback_data="add:150"),
                    InlineKeyboardButton("💧 200мл", callback_data="add:200"),
                    InlineKeyboardButton("💧 300мл", callback_data="add:300"),
                    InlineKeyboardButton("💧 500мл", callback_data="add:500"),
                ]]),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("reminder_job: ошибка для chat %s: %s", chat_id, e)

# ── Ежедневная реклама в 12:00 МСК ─────────────────────────────────
async def promo_job(ctx: ContextTypes.DEFAULT_TYPE):
    users = all_users()
    logger.info("promo_job: пользователей=%d", len(users))
    for user in users:
        chat_id = user["chat_id"]
        try:
            await ctx.bot.send_message(chat_id=chat_id, text="Если не сложно, подпишись на автора в инстаграмме;)")
            await ctx.bot.send_message(chat_id=chat_id, text="https://www.instagram.com/alleksashka.a?igsh=MTZ2OGx5eXd2b2xoYw%3D%3D&utm_source=qr")
            logger.info("promo_job: отправлено chat %s", chat_id)
        except Exception as e:
            logger.warning("promo_job: ошибка для chat %s: %s", chat_id, e)

# ── Запуск ──────────────────────────────────────────────────────────
def main():
    import datetime as dt
    init_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # 10:00 МСК = 07:00 UTC, 16:00 МСК = 13:00 UTC
    app.job_queue.run_daily(reminder_job, time=dt.time(7,  0, 0, tzinfo=timezone.utc))
    app.job_queue.run_daily(reminder_job, time=dt.time(13, 0, 0, tzinfo=timezone.utc))
    app.job_queue.run_daily(promo_job,    time=dt.time(9,  0, 0, tzinfo=timezone.utc))

    logger.info("Бот запущен. Напоминания в 10:00 и 16:00 МСК.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
