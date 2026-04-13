import asyncio
import logging
import os
import re
import sqlite3
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from rapidfuzz import fuzz

API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

DB_PATH = "checklists.db"

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

STATUS_ORDER = ["empty", "done", "fail", "warning"]
STATUS_EMOJI = {
    "empty": "⚪",
    "done": "✅",
    "fail": "❌",
    "warning": "⚠️",
}

FILTER_ALIASES = {
    "empty only": {"empty"},
    "empty and warning": {"empty", "warning"},
    "done only": {"done"},
    "not done": {"fail", "warning"},
}

SOURCE_PREFIX = "!!!"


# -----------------------------
# Database
# -----------------------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
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
            source_user_id INTEGER,
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
            user_name TEXT,
            FOREIGN KEY(checklist_id) REFERENCES checklists(id) ON DELETE CASCADE
        );
        """)
        await db.commit()


async def get_owner_id() -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            "SELECT value FROM meta WHERE key = 'owner_id'"
        )
    return int(row[0]) if row and row[0] else None


async def set_owner_id(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO meta(key, value)
            VALUES('owner_id', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(user_id),),
        )
        await db.commit()


async def is_allowed_chat(chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            "SELECT 1 FROM allowed_chats WHERE chat_id = ?",
            (chat_id,),
        )
    return bool(row)


async def allow_chat(chat: types.Chat, added_by: int) -> None:
    title = chat.title or chat.full_name or chat.username or str(chat.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO allowed_chats(chat_id, title, added_by)
            VALUES(?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                added_by=excluded.added_by
            """,
            (chat.id, title, added_by),
        )
        await db.commit()


async def disallow_chat(chat_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM allowed_chats WHERE chat_id = ?", (chat_id,))
        await db.commit()

> Rafael Krasovskikh:
async def get_allowed_chats() -> list[tuple[int, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT chat_id, title FROM allowed_chats ORDER BY added_at DESC"
        )
    return [(int(r[0]), r[1]) for r in rows]


# -----------------------------
# Helpers
# -----------------------------

def get_display_name(user: types.User) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return user.username
    return str(user.id)


def is_source_text(text: str) -> bool:
    return bool(text and text.strip().startswith(SOURCE_PREFIX))


def normalize_item_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def parse_source(text: str) -> tuple[str, list[str]]:
    """
    Format:
    !!! Title
    - item 1
    - item 2

    Also accepts non-empty lines after the title as items,
    even if they do not start with '-'.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines:
        return "Checklist", []

    first = lines[0].strip()
    title = first[len(SOURCE_PREFIX):].strip() if first.startswith(SOURCE_PREFIX) else first.strip()
    title = title or "Checklist"

    items: list[str] = []
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("-"):
            item = line[1:].strip()
        else:
            item = line
        if item:
            items.append(item)

    return title, items


def format_checklist_text(title: str, items: list[dict]) -> str:
    lines = [title, ""]
    for item in items:
        emoji = STATUS_EMOJI[item["status"]]
        user_part = f" ✔ {item['user_name']}" if item.get("user_name") else ""
        lines.append(f"{emoji} {item['text']}{user_part}")
    return "\n".join(lines).strip()


def build_keyboard(checklist_id: int, items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        btn_text = f"{STATUS_EMOJI[item['status']]} {item['position'] + 1}"
        rows.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"toggle:{checklist_id}:{item['position']}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def next_status(current: str) -> str:
    idx = STATUS_ORDER.index(current)
    return STATUS_ORDER[(idx + 1) % len(STATUS_ORDER)]


def filter_command_from_source(text: str) -> Optional[str]:
    body = text.strip()[len(SOURCE_PREFIX):].strip().lower()
    return body if body in FILTER_ALIASES else None


def make_filtered_source_text(title: str, filter_name: str, items: list[dict]) -> str:
    allowed = FILTER_ALIASES[filter_name]
    filtered = [i["text"] for i in items if i["status"] in allowed]

    new_title = f"{SOURCE_PREFIX} {title} ({filter_name})"
    lines = [new_title]
    for item_text in filtered:
        lines.append(f"- {item_text}")
    return "\n".join(lines)


async def get_checklist_by_source(chat_id: int, source_message_id: int) -> Optional[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            """
            SELECT id, checklist_message_id, title
            FROM checklists
            WHERE chat_id = ? AND source_message_id = ?
            """,
            (chat_id, source_message_id),
        )
    return row


async def get_checklist_by_checklist_message(chat_id: int, checklist_message_id: int) -> Optional[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            """
            SELECT id, source_message_id, title
            FROM checklists
            WHERE chat_id = ? AND checklist_message_id = ?
            """,
            (chat_id, checklist_message_id),
        )
    return row

> Rafael Krasovskikh:
async def load_items(checklist_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            """
            SELECT position, text, status, user_id, user_name
            FROM items
            WHERE checklist_id = ?
            ORDER BY position ASC
            """,
            (checklist_id,),
        )
    return [
        {
            "position": int(r[0]),
            "text": r[1],
            "status": r[2],
            "user_id": r[3],
            "user_name": r[4],
        }
        for r in rows
    ]


async def save_new_checklist(
    chat_id: int,
    source_message: types.Message,
    title: str,
    source_text: str,
    item_texts: list[str],
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO checklists(chat_id, source_message_id, source_user_id, source_text, title)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                source_message.message_id,
                source_message.from_user.id if source_message.from_user else None,
                source_text,
                title,
            ),
        )
        checklist_id = cur.lastrowid

        for pos, text in enumerate(item_texts):
            await db.execute(
                """
                INSERT INTO items(checklist_id, position, text, status, user_id, user_name)
                VALUES(?, ?, ?, 'empty', NULL, NULL)
                """,
                (checklist_id, pos, text),
            )

        await db.commit()
    return int(checklist_id)


async def set_checklist_message_id(checklist_id: int, checklist_message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE checklists SET checklist_message_id = ? WHERE id = ?",
            (checklist_message_id, checklist_id),
        )
        await db.commit()


async def update_source_and_items_preserving_status(
    checklist_id: int,
    new_title: str,
    new_source_text: str,
    new_item_texts: list[str],
) -> None:
    old_items = await load_items(checklist_id)
    unmatched_old = old_items.copy()
    result_items = []

    for pos, raw_text in enumerate(new_item_texts):
        new_text = normalize_item_text(raw_text)
        best_index = None
        best_score = -1

        for idx, old in enumerate(unmatched_old):
            score = fuzz.ratio(new_text, normalize_item_text(old["text"]))
            if score > best_score:
                best_score = score
                best_index = idx

        if best_index is not None and best_score >= 70:
            old = unmatched_old.pop(best_index)
            result_items.append({
                "position": pos,
                "text": raw_text,
                "status": old["status"],
                "user_id": old["user_id"],
                "user_name": old["user_name"],
            })
        else:
            result_items.append({
                "position": pos,
                "text": raw_text,
                "status": "empty",
                "user_id": None,
                "user_name": None,
            })

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE checklists
            SET title = ?, source_text = ?
            WHERE id = ?
            """,
            (new_title, new_source_text, checklist_id),
        )
        await db.execute("DELETE FROM items WHERE checklist_id = ?", (checklist_id,))
        for item in result_items:
            await db.execute(
                """
                INSERT INTO items(checklist_id, position, text, status, user_id, user_name)
                VALUES(?, ?, ?, ?, ?, ?)
                """,

> Rafael Krasovskikh:
(
                    checklist_id,
                    item["position"],
                    item["text"],
                    item["status"],
                    item["user_id"],
                    item["user_name"],
                ),
            )
        await db.commit()


async def render_checklist_message(chat_id: int, checklist_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            "SELECT title FROM checklists WHERE id = ?",
            (checklist_id,),
        )
    title = row[0] if row else "Checklist"
    items = await load_items(checklist_id)
    return format_checklist_text(title, items), build_keyboard(checklist_id, items)


async def upsert_checklist_from_source_message(message: types.Message) -> None:
    title, item_texts = parse_source(message.text or "")
    existing = await get_checklist_by_source(message.chat.id, message.message_id)

    if existing:
        checklist_id, checklist_message_id, _ = existing
        await update_source_and_items_preserving_status(
            checklist_id=checklist_id,
            new_title=title,
            new_source_text=message.text,
            new_item_texts=item_texts,
        )
        text, kb = await render_checklist_message(message.chat.id, checklist_id)
        if checklist_message_id:
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=checklist_message_id,
                    text=text,
                    reply_markup=kb,
                )
            except TelegramBadRequest:
                pass
    else:
        checklist_id = await save_new_checklist(
            chat_id=message.chat.id,
            source_message=message,
            title=title,
            source_text=message.text,
            item_texts=item_texts,
        )
        text, kb = await render_checklist_message(message.chat.id, checklist_id)
        sent = await message.reply(text, reply_markup=kb)
        await set_checklist_message_id(checklist_id, sent.message_id)


def chat_is_permitted(chat: types.Chat, owner_id: Optional[int], from_user_id: Optional[int]) -> bool:
    if chat.type == "private":
        return owner_id is not None and from_user_id == owner_id
    return True


# -----------------------------
# Owner / security commands
# -----------------------------

@dp.message(Command("claimowner"))
async def cmd_claimowner(message: types.Message) -> None:
    if message.chat.type != "private":
        await message.reply("Use /claimowner in private chat with the bot.")
        return

    owner_id = await get_owner_id()
    if owner_id and owner_id != message.from_user.id:
        await message.reply("Owner is already claimed.")
        return

    await set_owner_id(message.from_user.id)
    await message.reply("Owner claimed successfully.")


@dp.message(Command("allowhere"))
async def cmd_allowhere(message: types.Message) -> None:
    owner_id = await get_owner_id()
    if owner_id != message.from_user.id:
        return

    await allow_chat(message.chat, message.from_user.id)
    await message.reply("This chat is now allowed.")


@dp.message(Command("disallowhere"))
async def cmd_disallowhere(message: types.Message) -> None:
    owner_id = await get_owner_id()
    if owner_id != message.from_user.id:
        return

    await disallow_chat(message.chat.id)
    await message.reply("This chat is now disallowed.")


@dp.message(Command("listallowed"))
async def cmd_listallowed(message: types.Message) -> None:
    owner_id = await get_owner_id()
    if owner_id != message.from_user.id:
        return

    rows = await get_allowed_chats()
    if not rows:
        await message.reply("No allowed chats yet.")
        return

    text = "Allowed chats:\n\n" + "\n".join(f"{cid} — {title}" for cid, title in rows)
    await message.reply(text)


@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    owner_id = await get_owner_id()

> Rafael Krasovskikh:
if message.chat.type == "private":
        if owner_id is None:
            await message.reply(
                "Use /claimowner first.\n\n"
                "Then add me to your group and run /allowhere there."
            )
            return

        if message.from_user.id != owner_id:
            await message.reply("This bot is private.")
            return

        await message.reply(
            "Bot is running.\n\n"
            "Main commands:\n"
            "/allowhere\n"
            "/disallowhere\n"
            "/listallowed\n\n"
            "Checklist source format:\n"
            "!!! Kitchen install\n"
            "- Sink\n"
            "- Faucet"
        )


# -----------------------------
# Checklist creation / update
# -----------------------------

@dp.message(F.text)
async def on_text_message(message: types.Message) -> None:
    text = message.text or ""
    if not is_source_text(text):
        return

    owner_id = await get_owner_id()
    if not chat_is_permitted(message.chat, owner_id, message.from_user.id if message.from_user else None):
        return

    if message.chat.type != "private":
        if not await is_allowed_chat(message.chat.id):
            return

    filter_name = filter_command_from_source(text)
    if filter_name:
        await handle_filter_request(message, filter_name)
        return

    await upsert_checklist_from_source_message(message)


@dp.edited_message(F.text)
async def on_edited_source(message: types.Message) -> None:
    text = message.text or ""
    if not is_source_text(text):
        return

    owner_id = await get_owner_id()
    if not chat_is_permitted(message.chat, owner_id, message.from_user.id if message.from_user else None):
        return

    if message.chat.type != "private":
        if not await is_allowed_chat(message.chat.id):
            return

    filter_name = filter_command_from_source(text)
    if filter_name:
        return

    existing = await get_checklist_by_source(message.chat.id, message.message_id)
    if not existing:
        return

    await upsert_checklist_from_source_message(message)


# -----------------------------
# Filtering
# -----------------------------

async def handle_filter_request(message: types.Message, filter_name: str) -> None:
    if not message.reply_to_message:
        await message.reply("Reply to a checklist message or source message.")
        return

    replied = message.reply_to_message

    target_checklist = await get_checklist_by_checklist_message(message.chat.id, replied.message_id)
    if not target_checklist:
        target_checklist = await get_checklist_by_source(message.chat.id, replied.message_id)

    if not target_checklist:
        await message.reply("Could not find a checklist for that message.")
        return

    checklist_id, _, title = target_checklist
    items = await load_items(checklist_id)

    new_source = make_filtered_source_text(title, filter_name, items)

    sent_source = await message.reply(new_source)
    fake_message = sent_source
    await upsert_checklist_from_source_message(fake_message)


# -----------------------------
# Toggle buttons
# -----------------------------

@dp.callback_query(F.data.startswith("toggle:"))
async def on_toggle(callback: types.CallbackQuery) -> None:
    owner_id = await get_owner_id()
    if not chat_is_permitted(
        callback.message.chat,
        owner_id,
        callback.from_user.id if callback.from_user else None
    ):
        await callback.answer("Not allowed.", show_alert=True)
        return

    if callback.message.chat.type != "private":
        if not await is_allowed_chat(callback.message.chat.id):
            await callback.answer("Chat is not allowed.", show_alert=True)
            return

    try:
        _, checklist_id_raw, position_raw = callback.data.split(":")
        checklist_id = int(checklist_id_raw)
        position = int(position_raw)
    except ValueError:
        await callback.answer()
        return

    user_name = get_display_name(callback.from_user)

> Rafael Krasovskikh:
async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone(
            """
            SELECT id, status
            FROM items
            WHERE checklist_id = ? AND position = ?
            """,
            (checklist_id, position),
        )
        if not row:
            await callback.answer()
            return

        item_id, current_status = row
        new_status = next_status(current_status)

        if new_status == "empty":
            await db.execute(
                """
                UPDATE items
                SET status = ?, user_id = NULL, user_name = NULL
                WHERE id = ?
                """,
                (new_status, item_id),
            )
        else:
            await db.execute(
                """
                UPDATE items
                SET status = ?, user_id = ?, user_name = ?
                WHERE id = ?
                """,
                (new_status, callback.from_user.id, user_name, item_id),
            )
        await db.commit()

    text, kb = await render_checklist_message(callback.message.chat.id, checklist_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass

    await callback.answer()


# -----------------------------
# Run
# -----------------------------

async def main() -> None:
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if name == "main":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
