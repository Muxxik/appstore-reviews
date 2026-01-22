import re
import time
from typing import Optional

import requests


def extract_app_id(app_url: str) -> str:
    """
    Извлекаем числовой id приложения из URL App Store.
    Пример: https://apps.apple.com/us/app/.../id931943412 -> 931943412
    """
    match = re.search(r"id(\d+)", app_url)
    if not match:
        raise ValueError("Не удалось найти id приложения в URL")
    return match.group(1)


def _normalize_country(country: str) -> str:
    c = (country or "").strip().lower()
    if len(c) != 2:
        return "us"
    return c


def _normalize_rating_input(rating_input: str) -> str:
    """
    Возвращает:
    - "all" если пусто
    - "1".."5" если валидно
    - "all" если невалидно
    """
    r = (rating_input or "").strip().lower()
    if not r:
        return "all"
    if r in {"1", "2", "3", "4", "5"}:
        return r
    return "all"


def _format_review_md(author: str, rating: str, date: str, title: str, content: str) -> str:
    return (
        f"**Автор:** {author}\n\n"
        f"**Оценка:** {rating}\n\n"
        f"**Дата:** {date}\n\n"
        f"**Заголовок:** {title}\n\n"
        f"{content}\n\n"
        "---\n\n"
    )


def download_reviews_to_md_file(
    app_url: str,
    country: str,
    rating_input: str,
    sleep_sec: float = 0.5,
    timeout_sec: int = 15,
) -> str:
    """
    Скачивает отзывы (из публичного RSS-API Apple) и сохраняет в .md файл.

    Имя файла (как ты выбрал):
    {app_id}_reviews_{country.lower()}_{rating_input}.md

    rating_input:
    - "1".."5" -> фильтруем по оценке
    - пусто или "all" -> берём все оценки

    Возвращает: путь к созданному файлу (в текущей папке).
    """
    app_id = extract_app_id(app_url)
    country_norm = _normalize_country(country)
    rating_norm = _normalize_rating_input(rating_input)

    rating_filter: Optional[str] = None
    if rating_norm in {"1", "2", "3", "4", "5"}:
        rating_filter = rating_norm  # сравниваем со строкой из JSON

    page = 1
    parts: list[str] = []

    while True:
        rss_url = (
            f"https://itunes.apple.com/{country_norm}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortBy=mostRecent/json"
        )

        resp = requests.get(rss_url, timeout=timeout_sec)
        if resp.status_code != 200:
            break

        data = resp.json()
        entries = data.get("feed", {}).get("entry", [])

        # На первой странице первый entry — это само приложение, не отзыв
        if page == 1 and entries:
            entries = entries[1:]

        if not entries:
            break

        for entry in entries:
            content = entry.get("content", {}).get("label", "").strip()
            title = entry.get("title", {}).get("label", "").strip()
            author = entry.get("author", {}).get("name", {}).get("label", "").strip()
            rating = entry.get("im:rating", {}).get("label", "")
            date = entry.get("updated", {}).get("label", "")

            if rating_filter is not None and rating != rating_filter:
                continue

            parts.append(_format_review_md(author, rating, date, title, content))

        page += 1
        time.sleep(sleep_sec)

    filename = f"{app_id}_reviews_{country_norm.lower()}_{rating_norm}.md"
    with open(filename, "w", encoding="utf-8") as f:
        if parts:
            f.write("".join(parts))
        else:
            f.write("# Отзывы не найдены\n")

    return filename
