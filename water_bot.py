#!/usr/bin/env python3
"""
💧 Water Tracker Bot
Данные хранятся локально в SQLite, изолированно по Telegram user_id.
Никаких внешних сервисов не требуется.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# Токен берётся из переменной окружения BOT_TOKEN (Railway),
# при локальном запуске — из значения по умолчанию.
TOKEN = os.environ.get("BOT_TOKEN", "8932702484:AAHs2TbHiQX_e_TpSAQelZDrijNx5ssL5WY")

MSK = timezone(timedelta(hours=3))

# DB_PATH: на Railway указывай переменную DB_PATH=/data (persistent volume)
_db_dir = os.environ.get("DB_PATH", str(Path(__file__).parent))
DB_PATH = Path(_db_dir) / "water.db"

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
                chat_id INTEGER NOT NULL
            );
        """)

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

def register_user(user_id: int, chat_id: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, chat_id) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id",
            (user_id, chat_id),
        )

def all_users() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT user_id, chat_id FROM users"
        ).fetchall()]

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
            InlineKeyboardButton("💧 500мл", callback_data="add:500"),
        ],
        [InlineKeyboardButton("✏️ Своё количество", callback_data="custom")],
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
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Назад", callback_data="back")
    ]])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data="back")
    ]])

# ── /start ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_chat.id)
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

    # ── Добавить воду ──
    if data.startswith("add:"):
        ml = int(data.split(":")[1])
        add_entry(uid, ml)
        await query.edit_message_text(
            "💧 *Трекер воды*\n\n" + build_status(uid, added_ml=ml),
            reply_markup=main_kb(),
            parse_mode="Markdown",
        )

    # ── Своё количество ──
    elif data == "custom":
        ctx.user_data["waiting"] = "ml"
        await query.edit_message_text(
            "✏️ Введи количество мл (1–2000):",
            reply_markup=cancel_kb(),
        )

    # ── Обновить / Назад ──
    elif data in ("refresh", "back"):
        ctx.user_data.pop("waiting", None)
        await query.edit_message_text(
            "💧 *Трекер воды*\n\n" + build_status(uid),
            reply_markup=main_kb(),
            parse_mode="Markdown",
        )

    # ── История ──
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
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=back_kb(),
            parse_mode="Markdown",
        )

    # ── Статистика ──
    elif data == "stats":
        goal   = get_goal(uid)
        totals = [
            sum(e["ml"] for e in get_entries(
                uid, (datetime.now(MSK) - timedelta(days=i)).strftime("%Y-%m-%d")
            ))
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

    # ── Изменить норму ──
    elif data == "set_goal":
        ctx.user_data["waiting"] = "goal"
        await query.edit_message_text(
            f"⚙️ Текущая норма: *{get_goal(uid)} мл*\n\n"
            "Введи новую норму (500–5000 мл):",
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
    now = datetime.now(MSK)
    if not (8 <= now.hour < 19):
        return

    for user in all_users():
        uid     = user["user_id"]
        chat_id = user["chat_id"]
        goal    = get_goal(uid)
        total   = sum(e["ml"] for e in get_entries(uid))
        if total >= goal:
            continue

        remain = goal - total
        pct    = total * 100 // goal
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
            logger.warning("Reminder failed for %s: %s", chat_id, e)

# ── Запуск ──────────────────────────────────────────────────────────
def main():
    init_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Первый запуск напоминалки — в начале следующего часа
    now  = datetime.now(MSK)
    secs = (60 - now.minute) * 60 - now.second
    app.job_queue.run_repeating(reminder_job, interval=3600, first=secs)
    logger.info("Бот запущен. Первое напоминание через %d сек.", secs)

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
