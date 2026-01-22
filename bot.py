import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Создай .env и добавь BOT_TOKEN=...")

# Пока просто храним состояние "ждём URL" на пользователя
waiting_for_url: set[int] = set()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    waiting_for_url.add(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Пришли мне URL приложения из App Store, и я начну с ним работать.\n"
        "Пример: https://apps.apple.com/us/app/.../id123456789"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if user_id in waiting_for_url:
        waiting_for_url.discard(user_id)
        await update.message.reply_text(
            f"Принял URL:\n{text}\n\n"
            "Следующим шагом я научусь спрашивать страну и рейтинг, а потом — скачивать отзывы."
        )
        return

    await update.message.reply_text("Напиши /start, чтобы начать.")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
