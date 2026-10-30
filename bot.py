import asyncio
import os
import re
import logging
import time
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from dataclasses import dataclass
from appstore_reviews import download_reviews_to_md_file
from googleplay_reviews import (
    download_reviews_to_md_file as download_googleplay,
    extract_app_id as extract_googleplay_id,
    normalize_options as normalize_googleplay_options,
)

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
logger = logging.getLogger("reviews-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)




@dataclass
class UserSession:
    step: str = "url"          # url -> country -> [language] -> rating -> downloading
    url: str = ""
    store: str = "appstore"
    country: str = ""
    lang: str = "en"
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

LANGUAGE_KB = ReplyKeyboardMarkup(
    [["en", "ru", "es", "fr"], ["de", "it", "pt", "tr"], ["pl", "cs", "iw", "ar"], ["default (en)"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def _detect_store(text: str) -> str | None:
    try:
        url = urlparse(text.strip())
        if url.scheme not in {"http", "https"}:
            return None
        if url.hostname == "play.google.com":
            extract_googleplay_id(text)
            return "googleplay"
        if url.hostname in {"apps.apple.com", "itunes.apple.com"} and re.search(r"/app/(?:[^/]+/)?id\d+/?$", url.path):
            return "appstore"
    except ValueError:
        pass
    return None


def _normalize_language(text: str) -> str | None:
    lang = (text or "").strip().lower()
    if not lang or lang in {"default", "default (en)", "-"}:
        return "en"
    if lang == "he":
        lang = "iw"  # код иврита в Google Play
    try:
        return normalize_googleplay_options("us", lang, "all")[1]
    except ValueError:
        return None


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
        "Пришли ссылку на приложение из App Store или Google Play.\n"
        "Примеры:\n"
        "https://apps.apple.com/us/app/.../id123456789\n"
        "https://play.google.com/store/apps/details?id=com.ululab.numbers\n\n"
        "Можно в любой момент написать /cancel чтобы сбросить диалог.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    previous = sessions.pop(user_id, None)
    text = "Ок, сбросил. Напиши /start чтобы начать заново."
    if previous and previous.step == "downloading":
        text += " Уже начатая выгрузка продолжится, файл придёт отдельно."
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

async def _download_and_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, session: UserSession) -> None:
    loop = asyncio.get_running_loop()
    
    started = time.monotonic()
    logger.info(
        "download_start user_id=%s chat_id=%s store=%s country=%s lang=%s rating=%s url=%s",
        user_id, chat_id, session.store, session.country, session.lang, session.rating_input, session.url
    )


    try:
        # Одинаковые запросы разных пользователей не должны перезаписывать файлы.
        with TemporaryDirectory(prefix="reviews-bot-") as output_dir:
            kwargs = dict(app_url=session.url, country=session.country,
                          rating_input=session.rating_input, output_dir=output_dir)
            downloader = download_reviews_to_md_file
            store_name = "App Store"
            if session.store == "googleplay":
                downloader = download_googleplay
                store_name = "Google Play"
                kwargs["lang"] = session.lang
            filename, reviews_count = await loop.run_in_executor(
                None, lambda: downloader(**kwargs),
            )
            caption = (
                f"Готово ✅ — {store_name}\n"
                f"Страна{' запроса' if session.store == 'googleplay' else ''}: {session.country}\n"
                + (f"Язык запроса: {session.lang}\n" if session.store == "googleplay" else "")
                + f"Оценка: {session.rating_input}\nОтзывов: {reviews_count}"
            )
            if session.store == "googleplay":
                caption += "\nСтрана запроса не подтверждает страну автора."
            if reviews_count == 0:
                caption += "\nОтзывы не получены. Попробуй другие параметры или повтори позже."
            with open(filename, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, document=f,
                    filename=os.path.basename(filename), caption=caption,
                )
        elapsed = time.monotonic() - started
        logger.info(
            "download_done user_id=%s reviews=%s elapsed_sec=%.2f file=%s",
            user_id, reviews_count, elapsed, os.path.basename(filename)
        )
    except Exception as e:
        logger.exception(
            "download_error user_id=%s store=%s country=%s rating=%s error=%s",
            user_id, session.store, session.country, session.rating_input, type(e).__name__
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="Упс, не получилось скачать отзывы 😕 Проверь ссылку и параметры или повтори позже. Напиши /start для новой попытки.",
        )

    finally:
        # /start или /cancel могли открыть новый диалог во время загрузки.
        if sessions.get(user_id) is session:
            sessions.pop(user_id, None)



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
        store = _detect_store(text)
        if store is None:
            await update.message.reply_text(
                "Пришли ссылку на приложение из App Store или Google Play:\n"
                "https://apps.apple.com/us/app/.../id123456789\n"
                "https://play.google.com/store/apps/details?id=com.ululab.numbers"
            )
            return

        s.url = text
        s.store = store
        s.step = "country"
        await update.message.reply_text(
            f"Шаг 2/{4 if store == 'googleplay' else 3}: выбери страну {'выдачи Google Play' if store == 'googleplay' else 'App Store'}.\n"
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
        if s.store == "googleplay":
            s.step = "language"
            await update.message.reply_text(
                "Шаг 3/4: выбери язык выдачи отзывов.\n"
                "Нажми кнопку или введи код языка: en — английский, ru — русский, "
                "es — испанский, fr — французский.\n"
                "По умолчанию — en. Язык выбирается независимо от страны.",
                reply_markup=LANGUAGE_KB,
            )
            return
        s.step = "rating"
        await update.message.reply_text(
            "Шаг 3/3: какая оценка отзывов нужна?\n"
            "Нажми кнопку 1–5 или all (все оценки).",
            reply_markup=RATING_KB,
        )
        return

    if s.step == "language":
        lang = _normalize_language(text)
        if lang is None:
            await update.message.reply_text("Введи код языка (например: en, ru, es, fr) или нажми кнопку.")
            return
        s.lang = lang
        s.step = "rating"
        await update.message.reply_text(
            "Шаг 4/4: какая оценка отзывов нужна?\nНажми кнопку 1–5 или all (все оценки).",
            reply_markup=RATING_KB,
        )
        return

    # Последний шаг: Rating
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
        context.application.create_task(_download_and_send(context, chat_id, user_id, s), update=update)

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
