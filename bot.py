import asyncio
import os
import re
import logging
import time
import threading
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from dataclasses import dataclass, field
from googleplay_reviews import (
    extract_app_id as extract_googleplay_id,
    normalize_options as normalize_googleplay_options,
)
from review_archive import download_archive, APPLE_COUNTRIES, GOOGLE_COUNTRIES, GOOGLE_LANGUAGES, query_locales

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
    step: str = "url"          # url -> mode -> [settings] -> downloading
    url: str = ""
    store: str = "appstore"
    country: str = "all"
    lang: str = "all"
    rating_input: str = "all"  # "1".."5" или "all"
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    progress: dict = field(default_factory=dict, repr=False)
    progress_message: object = field(default=None, repr=False)


sessions: dict[int, UserSession] = {}

DOWNLOAD_ALL = "Скачать все отзывы"
CUSTOMIZE = "Настроить"
DOWNLOAD_SELECTED = "Скачать с этими настройками"
CHANGE_COUNTRY = "Страна"
CHANGE_LANGUAGE = "Язык"
CHANGE_RATING = "Оценки"
BACK = "Назад"

MODE_KB = ReplyKeyboardMarkup(
    [[DOWNLOAD_ALL], [CUSTOMIZE]], resize_keyboard=True, one_time_keyboard=True,
)

COUNTRY_KB = ReplyKeyboardMarkup(
    [["all", "us", "ru"], ["de", "fr", "es"], [BACK]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

RATING_KB = ReplyKeyboardMarkup(
    [["all", "5", "4"], ["3", "2", "1"], [BACK]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

LANGUAGE_KB = ReplyKeyboardMarkup(
    [["all"], ["en", "ru", "es", "fr"], ["de", "it", "pt", "tr"], ["pl", "cs", "iw", "ar"], [BACK]],
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
    if lang in {"all", "все"}:
        return "all"
    if not lang or lang in {"default", "default (en)", "-"}:
        return "en"
    if lang == "he":
        lang = "iw"  # код иврита в Google Play
    try:
        normalized = normalize_googleplay_options("us", lang, "all")[1]
        return normalized if normalized in GOOGLE_LANGUAGES else None
    except ValueError:
        return None


def _normalize_country(text: str) -> str | None:
    c = (text or "").strip().lower()
    if c in {"all", "все"}:
        return "all"
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


def _settings_summary(session: UserSession) -> str:
    text = f"Страны: {'все' if session.country == 'all' else session.country}\n"
    if session.store == "googleplay":
        text += f"Языки: {'все' if session.lang == 'all' else session.lang}\n"
    text += f"Оценки: {'все (1–5)' if session.rating_input == 'all' else session.rating_input}"
    return text


async def _show_settings(update: Update, session: UserSession) -> None:
    session.step = "settings"
    fields = [CHANGE_COUNTRY, CHANGE_RATING]
    if session.store == "googleplay":
        fields.insert(1, CHANGE_LANGUAGE)
    await update.message.reply_text(
        f"{_settings_summary(session)}\n\nИзмени нужный параметр или скачай отзывы.",
        reply_markup=ReplyKeyboardMarkup(
            [[DOWNLOAD_SELECTED], fields, [BACK]], resize_keyboard=True, one_time_keyboard=True,
        ),
    )


async def _show_mode(update: Update, session: UserSession) -> None:
    session.step = "mode"
    await update.message.reply_text(
        "Соберу доступные отзывы из всех стран"
        + (" и на всех языках" if session.store == "googleplay" else "")
        + ", со всеми оценками. Повторы уберу.\n\n"
        "Большая выгрузка может занять несколько часов. Её можно остановить и получить собранное.\n\n"
        "Для выбора страны или отдельных оценок нажми «Настроить».",
        reply_markup=MODE_KB,
    )


async def _begin_download(update: Update, context: ContextTypes.DEFAULT_TYPE, session: UserSession) -> None:
    session.step = "downloading"
    session.progress_message = await update.message.reply_text(
        f"Скачиваю отзывы.\n{_settings_summary(session)}\n\n"
        "/status — прогресс\n/stop — остановить и получить уже собранное",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.application.create_task(
        _download_and_send(context, update.effective_chat.id, update.effective_user.id, session), update=update,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id in sessions and sessions[user_id].step == "downloading":
        await update.message.reply_text("Выгрузка уже идёт. /status — прогресс, /stop — остановить и получить собранное.")
        return
    sessions[user_id] = UserSession(step="url")
    await update.message.reply_text(
        "Привет! Помогу скачать отзывы.\n\n"
        "Пришли ссылку на приложение из App Store или Google Play.\n"
        "Примеры:\n"
        "https://apps.apple.com/us/app/.../id123456789\n"
        "https://play.google.com/store/apps/details?id=com.ululab.numbers\n\n"
        "Можно в любой момент написать /cancel чтобы сбросить диалог или остановить сбор.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id in sessions and sessions[user_id].step == "downloading":
        await stop_download(update, context)
        return
    sessions.pop(user_id, None)
    text = "Ок, сбросил. Напиши /start чтобы начать заново."
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())


def _progress_text(session: UserSession) -> str:
    p = session.progress
    total = p.get("total") or len(query_locales(session.store, session.country, session.lang))
    return (f"Обработано запросов: {p.get('completed', 0)}/{total}\n"
            f"Уникальных отзывов: {p.get('reviews', 0)}\nОшибок: {p.get('errors', 0)}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = sessions.get(update.effective_user.id)
    await update.message.reply_text(_progress_text(session) if session and session.step == "downloading" else "Активной выгрузки нет. Напиши /start.")


async def stop_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = sessions.get(update.effective_user.id)
    if session and session.step == "downloading":
        session.stop.set()
        await update.message.reply_text("Останавливаю сбор. Дождусь текущих запросов и пришлю уже собранные отзывы.")
    else:
        await update.message.reply_text("Активной выгрузки нет. Напиши /start.")

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
            def progress(snapshot):
                session.progress = snapshot

            future = loop.run_in_executor(
                None, lambda: download_archive(
                    session.url, session.store, session.country, session.lang, session.rating_input,
                    output_dir=output_dir, stop=session.stop, progress=progress,
                ),
            )
            last_progress = None
            try:
                while not future.done():
                    done, _ = await asyncio.wait([future], timeout=30)
                    text = _progress_text(session) + "\n/stop — остановить и получить собранное"
                    if not done and session.progress_message and text != last_progress:
                        try:
                            await session.progress_message.edit_text(text)
                            last_progress = text
                        except Exception:
                            logger.warning("progress_update_failed user_id=%s", user_id)
                result = await future
            except asyncio.CancelledError:
                session.stop.set()
                # Не удаляем папку до завершения записывающего потока.
                await asyncio.shield(future)
                raise
            store_name = "Google Play" if session.store == "googleplay" else "App Store"
            partial = result.stopped or result.errors or result.completed < result.total
            caption = (
                f"{'Частичная выгрузка' if partial else 'Готово ✅'} — {store_name}\n"
                f"{_settings_summary(session)}\nОтзывов без повторов: {result.count}\n"
                f"Обработано запросов: {result.completed}/{result.total}\nОшибок: {result.errors}"
            )
            if session.store == "googleplay":
                caption += "\nСтрана запроса не подтверждает страну автора."
            if result.count == 0:
                caption += "\nОтзывы не получены. Попробуй другие параметры или повтори позже."
            for path in result.files:
                with path.open("rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id, document=f, filename=path.name,
                        caption="Отчёт об охвате и ошибках выгрузки" if path.name == "report.json" else caption,
                    )
        elapsed = time.monotonic() - started
        logger.info(
            "download_done user_id=%s reviews=%s elapsed_sec=%.2f",
            user_id, result.count, elapsed,
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
        await _show_mode(update, s)
        return

    if s.step == "mode":
        if text == DOWNLOAD_ALL:
            s.country = "all"
            s.lang = "all"
            s.rating_input = "all"
            await _begin_download(update, context, s)
        elif text == CUSTOMIZE:
            await _show_settings(update, s)
        else:
            await update.message.reply_text("Нажми «Скачать все отзывы» или «Настроить».", reply_markup=MODE_KB)
        return

    if s.step == "settings":
        if text == DOWNLOAD_SELECTED:
            await _begin_download(update, context, s)
        elif text == CHANGE_COUNTRY:
            s.step = "country"
            await update.message.reply_text("Выбери страну или введи двухбуквенный код (us, ru, de, fr…). all — все страны.", reply_markup=COUNTRY_KB)
        elif text == CHANGE_LANGUAGE and s.store == "googleplay":
            s.step = "language"
            await update.message.reply_text("Выбери язык или введи код: en — английский, ru — русский, es — испанский, fr — французский. all — все языки.", reply_markup=LANGUAGE_KB)
        elif text == CHANGE_RATING:
            s.step = "rating"
            await update.message.reply_text("Выбери оценку 1–5 или all — все оценки.", reply_markup=RATING_KB)
        elif text == BACK:
            s.country = "all"
            s.lang = "all"
            s.rating_input = "all"
            await _show_mode(update, s)
        else:
            await _show_settings(update, s)
        return

    if s.step in {"country", "language", "rating"} and text == BACK:
        await _show_settings(update, s)
        return

    if s.step == "country":
        country = _normalize_country(text)
        countries = APPLE_COUNTRIES if s.store == "appstore" else GOOGLE_COUNTRIES
        if country is None or (country != "all" and country not in countries):
            await update.message.reply_text(
                "Выбери поддерживаемый код страны (us, ru, de, fr…) или all — все страны.\n"
                "Попробуй ещё раз."
            )
            return

        s.country = country
        await _show_settings(update, s)
        return

    if s.step == "language":
        lang = _normalize_language(text)
        if lang is None:
            await update.message.reply_text("Введи код языка (например: en, ru, es, fr) или нажми кнопку.")
            return
        s.lang = lang
        await _show_settings(update, s)
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
        await _show_settings(update, s)
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
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop_download))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
