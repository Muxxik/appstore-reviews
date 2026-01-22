import asyncio
import os
import re
import logging
import time

from dataclasses import dataclass
from appstore_reviews import download_reviews_to_md_file

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Создай .env и добавь BOT_TOKEN=...")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("appstore-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)




@dataclass
class UserSession:
    step: str = "url"          # url -> country -> rating -> downloading
    url: str = ""
    country: str = ""
    rating_input: str = "all"  # "1".."5" или "all"


sessions: dict[int, UserSession] = {}

COUNTRY_KB = ReplyKeyboardMarkup(
    [["us", "ru", "de", "fr"], ["default (us)"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

RATING_KB = ReplyKeyboardMarkup(
    [["all", "5", "4"], ["3", "2", "1"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def _looks_like_appstore_url(text: str) -> bool:
    # Достаточно мягкая проверка: домен + /app/ + id123...
    t = (text or "").strip()
    return bool(re.search(r"apps\.apple\.com/.+/app/.+id\d+", t))


def _normalize_country(text: str) -> str | None:
    c = (text or "").strip().lower()
    if not c or c in {"default", "default (us)", "us default", "-"}:
        return "us"
    if re.fullmatch(r"[a-z]{2}", c):
        return c
    return None


def _normalize_rating(text: str) -> str | None:
    r = (text or "").strip().lower()
    if not r or r in {"all", "-"}:
        return "all"
    if r in {"1", "2", "3", "4", "5"}:
        return r
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    sessions[user_id] = UserSession(step="url")
    await update.message.reply_text(
        "Привет! Давай соберём параметры.\n\n"
        "Шаг 1/3: пришли URL приложения из App Store.\n"
        "Пример: https://apps.apple.com/us/app/.../id123456789\n\n"
        "Можно в любой момент написать /cancel чтобы сбросить."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    sessions.pop(user_id, None)
    await update.message.reply_text("Ок, сбросил. Напиши /start чтобы начать заново.")

async def _download_and_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, url: str, country: str, rating_input: str) -> None:
    loop = asyncio.get_running_loop()
    filename = None
    
    started = time.monotonic()
    logger.info(
        "download_start user_id=%s chat_id=%s country=%s rating=%s url=%s",
        user_id, chat_id, country, rating_input, url
    )


    try:
        filename, reviews_count = await loop.run_in_executor(
            None,
            lambda: download_reviews_to_md_file(
                app_url=url,
                country=country,
                rating_input=rating_input,
            ),
        )

        with open(filename, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=os.path.basename(filename),
                caption=(
                    "Готово ✅\n"
                    f"Country: {country}\n"
                    f"Rating: {rating_input}\n"
                    f"Reviews: {reviews_count}"
                ),
            )
        elapsed = time.monotonic() - started
        logger.info(
            "download_done user_id=%s reviews=%s elapsed_sec=%.2f file=%s",
            user_id, reviews_count, elapsed, os.path.basename(filename)
        )
    except Exception as e:
        logger.exception(
            "download_error user_id=%s country=%s rating=%s error=%s",
            user_id, country, rating_input, type(e).__name__
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="Упс, не получилось скачать отзывы 😕 Попробуй другую страну или повтори позже.",
        )

    finally:
        # очищаем сессию
        sessions.pop(user_id, None)

        # удаляем файл, чтобы не копился мусор
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except OSError:
                pass



async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if user_id not in sessions:
        await update.message.reply_text("Напиши /start чтобы начать.")
        return

    s = sessions[user_id]
    if s.step == "downloading":
        await update.message.reply_text("Я уже скачиваю отзывы 👀 Подожди немного, скоро пришлю файл.")
        return

    # Шаг 1: URL
    if s.step == "url":
        if not _looks_like_appstore_url(text):
            await update.message.reply_text(
                "Похоже, это не URL App Store. Пришли ссылку вида:\n"
                "https://apps.apple.com/us/app/.../id123456789"
            )
            return

        s.url = text
        s.step = "country"
        await update.message.reply_text(
            "Шаг 2/3: из какой страны нужны отзывы?\n"
            "Нажми кнопку или введи двухбуквенный код (us, ru, de, fr…).\n"
            "Для значения по умолчанию — нажми кнопку default (us).",
            reply_markup=COUNTRY_KB,
        )

        return

    # Шаг 2: Country
    if s.step == "country":
        country = _normalize_country(text)
        if country is None:
            await update.message.reply_text(
                "Страна должна быть двухбуквенным кодом (например: us, ru, de, fr).\n"
                "Попробуй ещё раз."
            )
            return

        s.country = country
        s.step = "rating"
        await update.message.reply_text(
            "Шаг 3/3: какая оценка отзывов нужна?\n"
            "Нажми кнопку 1–5 или all (все оценки).",
            reply_markup=RATING_KB,
        )
        return

    # Шаг 3: Rating
    if s.step == "rating":
        rating = _normalize_rating(text)
        if rating is None:
            await update.message.reply_text(
                "Оценка должна быть 1..5 или all.\n"
                "Попробуй ещё раз."
            )
            return

        s.rating_input = rating
        s.step = "downloading"

        await update.message.reply_text(
            "Принято ✅ Начинаю скачивать отзывы и готовить .md файл…",
            reply_markup=ReplyKeyboardRemove(),
        )

        chat_id = update.effective_chat.id
        # запускаем в фоне и сразу возвращаем управление, чтобы бот отвечал на новые сообщения
        asyncio.create_task(_download_and_send(context, chat_id, user_id, s.url, s.country, s.rating_input))

        return


    # Если уже done
    await update.message.reply_text(
        "Ок, сбросил. Напиши /start чтобы начать заново.",
        reply_markup=ReplyKeyboardRemove(),
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
