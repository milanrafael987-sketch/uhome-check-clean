import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

conn = sqlite3.connect("bot.db", check_same_thread=False)
cur = conn.cursor()

# TABLES
cur.execute("CREATE TABLE IF NOT EXISTS owner (user_id TEXT PRIMARY KEY)")
cur.execute("CREATE TABLE IF NOT EXISTS allowed_chats (chat_id TEXT PRIMARY KEY)")
cur.execute("CREATE TABLE IF NOT EXISTS items (cid TEXT, position INTEGER, text TEXT, status TEXT, user TEXT)")
conn.commit()

STATUS_ORDER = ["⚪", "✅", "❌", "⚠️"]

def next_status(s):
    return STATUS_ORDER[(STATUS_ORDER.index(s) + 1) % len(STATUS_ORDER)]

def is_owner(user_id):
    cur.execute("SELECT user_id FROM owner WHERE user_id=?", (str(user_id),))
    return cur.fetchone() is not None

def is_allowed(chat_id):
    cur.execute("SELECT chat_id FROM allowed_chats WHERE chat_id=?", (str(chat_id),))
    return cur.fetchone() is not None

# OWNER COMMANDS

async def claim_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cur.execute("SELECT * FROM owner")
    if cur.fetchone():
        await update.message.reply_text("❌ Owner already set")
        return
    cur.execute("INSERT INTO owner VALUES (?)", (user_id,))
    conn.commit()
    await update.message.reply_text("✅ You are now OWNER")

async def allow_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    chat_id = str(update.effective_chat.id)
    cur.execute("INSERT OR REPLACE INTO allowed_chats VALUES (?)", (chat_id,))
    conn.commit()
    await update.message.reply_text("✅ Chat allowed")

async def disallow_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    chat_id = str(update.effective_chat.id)
    cur.execute("DELETE FROM allowed_chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    await update.message.reply_text("❌ Chat disallowed")

async def list_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    cur.execute("SELECT chat_id FROM allowed_chats")
    rows = cur.fetchall()
    text = "Allowed chats:\n" + "\n".join(r[0] for r in rows)
    await update.message.reply_text(text)

# CHECKLIST

def build_keyboard(cid):
    cur.execute("SELECT position, text, status, user FROM items WHERE cid=? ORDER BY position", (cid,))
    rows = cur.fetchall()
    keyboard = []
    for pos, text, status, user in rows:
        label = f"{status} {text}"
        if user:
            label += f" ✔ {user}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{cid}:{pos}")])
    return InlineKeyboardMarkup(keyboard)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not is_allowed(chat_id):
        return

    text = update.message.text
    if not text.startswith("!!!"):
        return

    lines = text.split("\n")
    title = lines[0].replace("!!!", "").strip()
    items = [l.strip() for l in lines[1:] if l.strip()]

    cid = str(update.message.message_id)

    cur.execute("DELETE FROM items WHERE cid=?", (cid,))
    for i, item in enumerate(items):
        cur.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?)", (cid, i, item, "⚪", None))
    conn.commit()

    await update.message.reply_text(title, reply_markup=build_keyboard(cid))

async def toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cid, pos = query.data.split(":")
    pos = int(pos)

    user = query.from_user.first_name

    cur.execute("SELECT status FROM items WHERE cid=? AND position=?", (cid, pos))
    s = cur.fetchone()[0]
    new_s = next_status(s)

    cur.execute("UPDATE items SET status=?, user=? WHERE cid=? AND position=?", (new_s, user, cid, pos))
    conn.commit()

    await query.edit_message_reply_markup(reply_markup=build_keyboard(cid))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 UHOME BOT READY")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("claimowner", claim_owner))
    app.add_handler(CommandHandler("allowhere", allow_here))
    app.add_handler(CommandHandler("disallowhere", disallow_here))
    app.add_handler(CommandHandler("listallowed", list_allowed))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(toggle))

    print("SECURE BOT RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
