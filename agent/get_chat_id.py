"""Send any message in the group where the bot is added — it will print the chat ID."""

import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

load_dotenv()

TOKEN = os.environ["AGENT_BOT_TOKEN"]


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    print(f"Chat ID: {chat.id}  |  Title: {chat.title}  |  Type: {chat.type}")


def main():
    print("Listening for messages... Send anything in the group where the bot is added.")
    print("Press Ctrl+C to stop.\n")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, on_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
