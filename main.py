import logging
import os
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

DB_PATH = Path("bot.db")
SOURCE_PREFIX = "!!!"

STATUS_ORDER = ["empty", "done", "fail", "warning"]
STATUS_EMOJI = {
    "empty": "⚪",
    "done": "✅",
    "fail": "❌",
    "warning": "⚠️",
}

FILTER_RULES = {
    "empty only": {"empty"},
    "empty and warning": {"empty", "warning"},
    "done only": {"done"},
    "not done": {"fail", "warning"},
}


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(name)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS allowed_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS checklists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                checklist_message_id INTEGER,
                source_text TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, source_message_id)
            );

            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checklist_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'empty',
                user_id INTEGER,
                user_name TEXT
            );
            """
        )


def get_owner_id():
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='owner_id'").fetchone()
        return int(row["value"]) if row and row["value"] else None


def set_owner_id(user_id: int):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES('owner_id', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(user_id),),
        )


def allow_chat(chat_id: int, title: str, added_by: int):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO allowed_chats(chat_id, title, added_by)
            VALUES(?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                added_by=excluded.added_by
            """,
            (chat_id, title, added_by),
        )


def disallow_chat(chat_id: int):
    with db() as conn:
        conn.execute("DELETE FROM allowed_chats WHERE chat_id=?", (chat_id,))


def is_allowed_chat(chat_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_chats WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return bool(row)


def get_allowed_chats():
    with db() as conn:
        return conn.execute(
            "SELECT chat_id, title FROM allowed_chats ORDER BY added_at DESC"
        ).fetchall()


def can_use_bot(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    owner_id = get_owner_id()

    if not chat or not user:
        return False

    if chat.type == "private":
        return owner_id is not None and user.id == owner_id

    return is_allowed_chat(chat.id)
def parse_source(text: str):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = lines[0].strip()
    title = first[len(SOURCE_PREFIX):].strip() if first.startswith(SOURCE_PREFIX) else first
    title = title or "Checklist"

    items = []
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        items.append(line)

    return title, items


def build_checklist_text(title: str, items: list[sqlite3.Row]) -> str:
    lines = [title, ""]
    for item in items:
        emoji = STATUS_EMOJI[item["status"]]
        user_part = f" ✔ {item['user_name']}" if item["user_name"] else ""
        lines.append(f"{emoji} {item['text']}{user_part}")
    return "\n".join(lines).strip()


def build_keyboard(checklist_id: int, items: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{STATUS_EMOJI[item['status']]} {item['position'] + 1}",
                    callback_data=f"toggle:{checklist_id}:{item['position']}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def get_checklist_by_source(chat_id: int, source_message_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM checklists
            WHERE chat_id=? AND source_message_id=?
            """,
            (chat_id, source_message_id),
        ).fetchone()


def get_checklist_by_any_message(chat_id: int, message_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM checklists
            WHERE chat_id=? AND (source_message_id=? OR checklist_message_id=?)
            """,
            (chat_id, message_id, message_id),
        ).fetchone()


def get_items(checklist_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM items
            WHERE checklist_id=?
            ORDER BY position ASC
            """,
            (checklist_id,),
        ).fetchall()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


def save_new_checklist(chat_id: int, source_message_id: int, source_text: str, title: str, items: list[str]):
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO checklists(chat_id, source_message_id, source_text, title)
            VALUES(?, ?, ?, ?)
            """,
            (chat_id, source_message_id, source_text, title),
        )
        checklist_id = cur.lastrowid
        for pos, item_text in enumerate(items):
            conn.execute(
                """
                INSERT INTO items(checklist_id, position, text, status, user_id, user_name)
                VALUES(?, ?, ?, 'empty', NULL, NULL)
                """,
                (checklist_id, pos, item_text),
            )
        return checklist_id


def set_checklist_message_id(checklist_id: int, checklist_message_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE checklists SET checklist_message_id=? WHERE id=?",
            (checklist_message_id, checklist_id),
        )


def update_checklist_from_edited_source(checklist_id: int, new_source_text: str, new_title: str, new_items: list[str]):
    old_items = get_items(checklist_id)
    remaining_old = [dict(row) for row in old_items]
    result = []

    for pos, new_text in enumerate(new_items):
        best_index = None
        best_score = -1.0

        for idx, old in enumerate(remaining_old):
            score = similarity(new_text, old["text"])
            if score > best_score:
                best_score = score
                best_index = idx
              if best_index is not None and best_score >= 0.70:
            old = remaining_old.pop(best_index)
            result.append(
                {
                    "position": pos,
                    "text": new_text,
                    "status": old["status"],
                    "user_id": old["user_id"],
                    "user_name": old["user_name"],
                }
            )
        else:
            result.append(
                {
                    "position": pos,
                    "text": new_text,
                    "status": "empty",
                    "user_id": None,
                    "user_name": None,
                }
            )

    with db() as conn:
        conn.execute(
            """
            UPDATE checklists
            SET source_text=?, title=?
            WHERE id=?
            """,
            (new_source_text, new_title, checklist_id),
        )
        conn.execute("DELETE FROM items WHERE checklist_id=?", (checklist_id,))
        for item in result:
            conn.execute(
                """
                INSERT INTO items(checklist_id, position, text, status, user_id, user_name)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    checklist_id,
                    item["position"],
                    item["text"],
                    item["status"],
                    item["user_id"],
                    item["user_name"],
                ),
            )


def next_status(current: str) -> str:
    idx = STATUS_ORDER.index(current)
    return STATUS_ORDER[(idx + 1) % len(STATUS_ORDER)]


async def render_checklist(context: ContextTypes.DEFAULT_TYPE, chat_id: int, checklist_id: int):
    with db() as conn:
        checklist = conn.execute(
            "SELECT * FROM checklists WHERE id=?", (checklist_id,)
        ).fetchone()

    items = get_items(checklist_id)
    text = build_checklist_text(checklist["title"], items)
    kb = build_keyboard(checklist_id, items)

    if checklist["checklist_message_id"]:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=checklist["checklist_message_id"],
            text=text,
            reply_markup=kb,
        )
    else:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        set_checklist_message_id(checklist_id, sent.message_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = get_owner_id()
    user = update.effective_user

    if update.effective_chat.type == "private":
        if owner_id is None:
            await update.effective_message.reply_text(
                "Use /claimowner first.\n\nThen add me to your group and run /allowhere there."
            )
            return

        if user.id != owner_id:
            await update.effective_message.reply_text("This bot is private.")
            return

    await update.effective_message.reply_text("Bot is running.")


async def cmd_claimowner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Use /claimowner in private chat.")
        return

    existing = get_owner_id()
    if existing and existing != update.effective_user.id:
        await update.effective_message.reply_text("Owner already exists.")
        return

    set_owner_id(update.effective_user.id)
    await update.effective_message.reply_text("Owner claimed.")


async def cmd_allowhere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_owner_id() != update.effective_user.id:
        return
    chat = update.effective_chat
    title = chat.title or chat.full_name or str(chat.id)
    allow_chat(chat.id, title, update.effective_user.id)
    await update.effective_message.reply_text("This chat is allowed.")
  async def cmd_disallowhere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_owner_id() != update.effective_user.id:
        return
    disallow_chat(update.effective_chat.id)
    await update.effective_message.reply_text("This chat is disallowed.")


async def cmd_listallowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_owner_id() != update.effective_user.id:
        return
    rows = get_allowed_chats()
    if not rows:
        await update.effective_message.reply_text("No allowed chats.")
        return
    text = "Allowed chats:\n\n" + "\n".join(f"{r['chat_id']} — {r['title']}" for r in rows)
    await update.effective_message.reply_text(text)


async def handle_filter_request(update: Update, context: ContextTypes.DEFAULT_TYPE, filter_name: str):
    reply = update.effective_message.reply_to_message
    if not reply:
        await update.effective_message.reply_text("Reply to a source or checklist message.")
        return

    checklist = get_checklist_by_any_message(update.effective_chat.id, reply.message_id)
    if not checklist:
        await update.effective_message.reply_text("Checklist not found.")
        return

    items = get_items(checklist["id"])
    allowed = FILTER_RULES[filter_name]
    filtered = [row["text"] for row in items if row["status"] in allowed]

    source_title = f"{SOURCE_PREFIX} {checklist['title']} ({filter_name})"
    source_text = "\n".join([source_title] + [f"- {t}" for t in filtered])

    sent_source = await update.effective_message.reply_text(source_text)
    title, parsed_items = parse_source(source_text)
    new_id = save_new_checklist(
        chat_id=update.effective_chat.id,
        source_message_id=sent_source.message_id,
        source_text=source_text,
        title=title,
        items=parsed_items,
    )
    await render_checklist(context, update.effective_chat.id, new_id)


async def handle_source_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_use_bot(update):
        return

    msg = update.effective_message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if not text.startswith(SOURCE_PREFIX):
        return

    command_body = text[len(SOURCE_PREFIX):].strip().lower()
    if command_body in FILTER_RULES:
        await handle_filter_request(update, context, command_body)
        return

    title, parsed_items = parse_source(text)
    existing = get_checklist_by_source(update.effective_chat.id, msg.message_id)

    if existing:
        update_checklist_from_edited_source(existing["id"], text, title, parsed_items)
        await render_checklist(context, update.effective_chat.id, existing["id"])
        return

    checklist_id = save_new_checklist(
        chat_id=update.effective_chat.id,
        source_message_id=msg.message_id,
        source_text=text,
        title=title,
        items=parsed_items,
    )
    sent = await msg.reply_text(
        build_checklist_text(title, get_items(checklist_id)),
        reply_markup=build_keyboard(checklist_id, get_items(checklist_id)),
    )
    set_checklist_message_id(checklist_id, sent.message_id)


async def on_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_use_bot(update):
        await update.callback_query.answer("Not allowed.", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    try:
        _, checklist_id_raw, position_raw = query.data.split(":")
        checklist_id = int(checklist_id_raw)
        position = int(position_raw)
    except Exception:
        return

    user = update.effective_user
    user_name = user.full_name or user.username or str(user.id)

    with db() as conn:
        item = conn.execute(
            """
            SELECT * FROM items
            WHERE checklist_id=? AND position=?
            """,
            (checklist_id, position),
        ).fetchone()

        if not item:
            return

        new_status = next_status(item["status"])
      if new_status == "empty":
            conn.execute(
                """
                UPDATE items
                SET status=?, user_id=NULL, user_name=NULL
                WHERE id=?
                """,
                (new_status, item["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE items
                SET status=?, user_id=?, user_name=?
                WHERE id=?
                """,
                (new_status, user.id, user_name, item["id"]),
            )

    await render_checklist(context, update.effective_chat.id, checklist_id)


def main():
    init_db()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("claimowner", cmd_claimowner))
    app.add_handler(CommandHandler("allowhere", cmd_allowhere))
    app.add_handler(CommandHandler("disallowhere", cmd_disallowhere))
    app.add_handler(CommandHandler("listallowed", cmd_listallowed))

    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^toggle:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_source_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_source_message))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if name == "main":
    main()
