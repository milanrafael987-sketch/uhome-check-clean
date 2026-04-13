import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

checklists = {}  # message_id -> data

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

def next_status(s):
    order = ["⚪", "✅", "❌", "⚠️"]
    return order[(order.index(s) + 1) % len(order)]

def smart_merge(old_items, new_items, old_status, old_users):
    new_status = []
    new_users = []

    for item in new_items:
        if item in old_items:
            idx = old_items.index(item)
            new_status.append(old_status[idx])
            new_users.append(old_users[idx])
        else:
            new_status.append("⚪")
            new_users.append(None)

    return new_status, new_users

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text.startswith("!!!"):
        return

    title, items = parse_checklist(text)
    cid = str(update.message.message_id)

    # если редактирование
    if cid in checklists:
        old = checklists[cid]
        status, users = smart_merge(old["items"], items, old["status"], old["users"])
    else:
        status = ["⚪"] * len(items)
        users = [None] * len(items)

    checklists[cid] = {
        "title": title,
        "items": items,
        "status": status,
        "users": users
    }

    await update.message.reply_text(title, reply_markup=build_keyboard(cid))

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.edited_message
    text = message.text

    if not text or not text.startswith("!!!"):
        return

    cid = str(message.message_id)

    if cid not in checklists:
        return

    title, items = parse_checklist(text)
    old = checklists[cid]

    status, users = smart_merge(old["items"], items, old["status"], old["users"])

    checklists[cid] = {
        "title": title,
        "items": items,
        "status": status,
        "users": users
    }

    # обновляем последнее сообщение бота (reply)
    try:
        await message.reply_text("Обновлено", reply_markup=build_keyboard(cid))
    except:
        pass

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
    await update.message.reply_text("Чек-лист бот с редактированием готов 🚀")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edit))
    app.add_handler(CallbackQueryHandler(toggle))

    print("Advanced checklist bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
