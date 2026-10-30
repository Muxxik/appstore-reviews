"""Загрузка публичных отзывов Google Play для CLI и Telegram-бота."""

import re
import ssl
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Версия 1.2.7 меняет глобальный SSL-контекст при импорте.
# Сохраняем проверку сертификатов для запросов этого процесса.
_https_context = ssl._create_default_https_context
try:
    from google_play_scraper import Sort, app, reviews
finally:
    ssl._create_default_https_context = _https_context


def extract_app_id(value: str) -> str:
    value = value.strip()
    if "://" in value:
        url = urlparse(value)
        if url.scheme not in {"http", "https"} or url.hostname != "play.google.com" or url.path != "/store/apps/details":
            raise ValueError("Нужна ссылка https://play.google.com/store/apps/details?id=…")
        value = parse_qs(url.query).get("id", [""])[0]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", value):
        raise ValueError("Нужна ссылка Google Play или package ID, например com.duolingo")
    return value


def normalize_options(country: str, lang: str, rating_input: str) -> tuple[str, str, str]:
    country = (country or "us").strip().lower()
    lang = (lang or "en").strip().lower()
    rating = (rating_input or "all").strip().lower()
    if not re.fullmatch(r"[a-z]{2}", country):
        raise ValueError("Код страны должен состоять из двух букв: us, ru, de…")
    if not re.fullmatch(r"[a-z]{2,3}(?:-(?:[a-z]{2,4}|[0-9]{3}))?", lang):
        raise ValueError("Укажи код языка: en, ru, de…")
    if rating not in {"all", "1", "2", "3", "4", "5"}:
        raise ValueError("Оценка должна быть 1..5 или all")
    return country, lang, rating


def fetch_rating(app_id: str, country: str, lang: str) -> dict:
    info = app(extract_app_id(app_id), country=country, lang=lang)
    return {
        "name": info.get("title", ""),
        "rating_count": info.get("ratings", 0),
        "rating_avg": round(info.get("score") or 0.0, 2),
    }


def fetch_reviews(
    app_id: str, country: str = "us", lang: str = "en",
    rating_input: str = "all", max_reviews: int = 0, sleep_sec: float = 0.4,
) -> list[dict]:
    """Собирает доступные отзывы; max_reviews=0 означает без лимита."""
    app_id = extract_app_id(app_id)
    country, lang, rating = normalize_options(country, lang, rating_input)
    if max_reviews < 0:
        raise ValueError("Лимит отзывов не может быть отрицательным")
    result: list[dict] = []
    seen: set[str] = set()
    seen_tokens: set[str] = set()
    token = None
    while True:
        batch, token = reviews(
            app_id, country=country, lang=lang, sort=Sort.NEWEST,
            count=min(200, max_reviews) if max_reviews else 200,
            filter_score_with=None if rating == "all" else int(rating),
            continuation_token=token,
        )
        for row in batch:
            review_id = row["reviewId"]
            if review_id in seen:
                continue
            seen.add(review_id)
            if rating != "all" and row.get("score") != int(rating):
                continue
            date = row.get("at")
            result.append({
                "review_id": review_id, "app_id": app_id,
                "country": country, "language": lang,
                "rating": row.get("score"),
                "date": date.isoformat() if date else "",
                "version": row.get("reviewCreatedVersion") or "",
                "author": row.get("userName") or "",
                "title": "", "text": row.get("content") or "",
            })
            if max_reviews and len(result) >= max_reviews:
                return result
        next_token = getattr(token, "token", None)
        if not batch or not next_token:
            break
        if next_token in seen_tokens:
            raise RuntimeError("Google Play повторяет страницу; выгрузка остановлена. Повтори позже.")
        seen_tokens.add(next_token)
        time.sleep(sleep_sec)
    return result


def download_reviews_to_md_file(
    app_url: str, country: str, rating_input: str,
    lang: str = "en", max_reviews: int = 0,
    output_dir: str | Path = ".",
) -> tuple[str, int]:
    app_id = extract_app_id(app_url)
    country, lang, rating = normalize_options(country, lang, rating_input)
    # Проверяем существование приложения: неверный ID не должен выглядеть как пустой список отзывов.
    info = fetch_rating(app_id, country, lang)
    rows = fetch_reviews(app_id, country, lang, rating, max_reviews)
    filename = str(Path(output_dir) / f"googleplay_{app_id}_reviews_{country}_{lang}_{rating}.md")
    parts = [f"# {info['name']} — Google Play\n\nСтрана запроса: {country}. Язык: {lang}. Оценка: {rating}.\n\n"]
    for row in rows:
        parts.append(
            f"**Автор:** {row['author']}\n\n**Оценка:** {row['rating']}\n\n"
            f"**Дата:** {row['date']}\n\n**Версия:** {row['version']}\n\n"
            f"{row['text']}\n\n---\n\n"
        )
    if not rows:
        parts.append("Отзывы не получены. Проверь страну и язык или повтори позже.\n")
    Path(filename).write_text("".join(parts), encoding="utf-8")
    return filename, len(rows)
