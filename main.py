import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

checklists = {}

def parse_checklist(text):
    lines = text.split("\n")
    title = lines[0].replace("!!!", "").strip()
    items = [l.strip() for l in lines[1:] if l.strip()]
    return title, items

def build_keyboard(cid):
    data = checklists[cid]
    keyboard = []
    for i, item in enumerate(data["items"]):
        status = data["status"][i]
        user = data["users"][i]
        label = f"{status} {item}"
        if user:
            label += f" ✔ {user}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{cid}:{i}")])
    return InlineKeyboardMarkup(keyboard)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text.startswith("!!!"):
        return

    title, items = parse_checklist(text)

    cid = str(update.message.message_id)
    checklists[cid] = {
        "title": title,
        "items": items,
        "status": ["⚪"] * len(items),
        "users": [None] * len(items)
    }

    await update.message.reply_text(title, reply_markup=build_keyboard(cid))

def next_status(s):
    order = ["⚪", "✅", "❌", "⚠️"]
    return order[(order.index(s) + 1) % len(order)]

async def toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cid, idx = query.data.split(":")
    idx = int(idx)

    user = query.from_user.first_name

    checklists[cid]["status"][idx] = next_status(checklists[cid]["status"][idx])
    checklists[cid]["users"][idx] = user

    await query.edit_message_reply_markup(reply_markup=build_keyboard(cid))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Чек-лист бот готов 🚀")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(toggle))

    print("Checklist bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
