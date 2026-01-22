import os
import asyncio
import re
from dataclasses import dataclass
from appstore_reviews import download_reviews_to_md_file

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Создай .env и добавь BOT_TOKEN=...")


@dataclass
class UserSession:
    step: str = "url"          # url -> country -> rating -> done
    url: str = ""
    country: str = ""
    rating_input: str = "all"  # "1".."5" или "all"


sessions: dict[int, UserSession] = {}


def _looks_like_appstore_url(text: str) -> bool:
    # Достаточно мягкая проверка: домен + /app/ + id123...
    t = (text or "").strip()
    return bool(re.search(r"apps\.apple\.com/.+/app/.+id\d+", t))


def _normalize_country(text: str) -> str | None:
    c = (text or "").strip().lower()
    if not c:
        return "us"
    if re.fullmatch(r"[a-z]{2}", c):
        return c
    return None


def _normalize_rating(text: str) -> str | None:
    r = (text or "").strip().lower()
    if not r or r == "all":
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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if user_id not in sessions:
        await update.message.reply_text("Напиши /start чтобы начать.")
        return

    s = sessions[user_id]

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
            "Введи двухбуквенный код (например: us, ru, de, fr).\n"
            "Если отправишь пустое сообщение — будет us."
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
            "Введи 1, 2, 3, 4 или 5.\n"
            "Или напиши all (или оставь пусто), чтобы взять все оценки."
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
        s.step = "done"

        await update.message.reply_text(
            "Принято ✅ Начинаю скачивать отзывы и готовить .md файл…"
        )

        # Важно: скачивание может занять время, поэтому выполняем в фоне (в отдельном потоке)
        loop = asyncio.get_running_loop()
        filename = None

        try:
            filename = await loop.run_in_executor(
                None,
                lambda: download_reviews_to_md_file(
                    app_url=s.url,
                    country=s.country,
                    rating_input=s.rating_input,
                ),
            )

            # Отправляем файл в Telegram
            with open(filename, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=os.path.basename(filename),
                    caption=(
                        "Готово ✅\n"
                        f"Country: {s.country}\n"
                        f"Rating: {s.rating_input}"
                    ),
                )

        except Exception as e:
            await update.message.reply_text(
                "Упс, не получилось скачать отзывы 😕\n"
                f"Ошибка: {type(e).__name__}: {e}"
            )
        finally:
            # Убираем состояние пользователя
            sessions.pop(user_id, None)

            # Удаляем файл после отправки/ошибки (чтобы не копились)
            if filename and os.path.exists(filename):
                try:
                    os.remove(filename)
                except OSError:
                    pass

        return


    # Если уже done
    await update.message.reply_text("Параметры уже собраны. Напиши /start чтобы начать заново или /cancel чтобы сбросить.")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
