"""Полная сетка стран/языков, пагинация, дедупликация и частичные результаты."""

import importlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timezone
from itertools import product
from pathlib import Path

import requests

from appstore_reviews import extract_app_id as apple_id
from googleplay_reviews import extract_app_id as google_id

gp = importlib.import_module("google_play_scraper.features.reviews")
CATALOG = json.loads(Path(__file__).with_name("store_locales.json").read_text())
APPLE_COUNTRIES = tuple(CATALOG["appstore_countries"])
GOOGLE_COUNTRIES = tuple(CATALOG["googleplay_countries"])
GOOGLE_LANGUAGES = tuple(CATALOG["googleplay_languages"])


class Stopped(Exception):
    pass


class Unavailable(Exception):
    pass


def query_locales(store, country="all", lang="all"):
    countries = APPLE_COUNTRIES if store == "appstore" else GOOGLE_COUNTRIES
    countries = countries if country == "all" else (country,)
    # Начинаем с часто используемых вариантов, но обходим каталог целиком.
    countries = sorted(countries, key=lambda c: (c != "us", c))
    languages = GOOGLE_LANGUAGES if lang == "all" else (lang,)
    if store == "appstore":
        languages = ("",)  # Apple RSS выдаёт все языки внутри страны.
    languages = sorted(languages, key=lambda l: (l != "en", l))
    return list(product(countries, languages))


def _request(session, method, url, stop, **kwargs):
    for attempt in range(3):
        if stop.is_set():
            raise Stopped()
        try:
            response = session.request(method, url, timeout=(10, 25), **kwargs)
            if response.status_code in (400, 404):
                return response
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == 2:
                raise
            if stop.wait(2 ** attempt):
                raise Stopped()


def google_pages(app_id, country, lang, rating, stop):
    token = None
    seen_tokens = set()
    with requests.Session() as session:
        while True:
            response = _request(
                session, "POST", gp.Formats.Reviews.build(lang=lang, country=country), stop,
                data=gp.Formats.Reviews.build_body(app_id, gp.Sort.NEWEST.value, 200,
                                                "null" if rating == "all" else int(rating), "null", token),
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            matches = gp.Regex.REVIEWS.findall(response.text)
            if not matches:
                raise ValueError("Google Play: отсутствует блок отзывов")
            payload = json.loads(json.loads(matches[0])[0][2])
            if not isinstance(payload, list):
                raise ValueError("Google Play: неизвестный формат ответа")
            items = payload[0] if payload else None
            if items is not None and not isinstance(items, list):
                raise ValueError("Google Play: неизвестный формат списка отзывов")
            if not items:
                return
            rows = []
            for item in items:
                raw = {k: spec.extract_content(item) for k, spec in gp.ElementSpecs.Review.items()}
                if rating != "all" and raw["score"] != int(rating):
                    continue
                rows.append({
                    "id": raw["reviewId"], "author": raw["userName"], "rating": raw["score"],
                    "date": raw["at"].astimezone(timezone.utc).isoformat() if raw["at"] else "",
                    "title": "", "text": raw["content"] or "", "reply": raw["replyContent"] or "",
                })
            yield rows
            try:
                token = payload[-2][-1]
            except (TypeError, IndexError):
                token = None
            if not token or isinstance(token, list):
                return
            if token in seen_tokens:
                raise ValueError("Google Play повторяет страницу")
            seen_tokens.add(token)
            if stop.wait(0.2):
                raise Stopped()


def apple_pages(app_id, country, lang, rating, stop):
    with requests.Session() as session:
        for page in range(1, 11):
            response = _request(session, "GET", f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json", stop)
            if response.status_code in (400, 404):
                if page == 1:
                    raise Unavailable("RSS недоступен в этой стране")
                return
            data = response.json()
            if not isinstance(data.get("feed"), dict):
                raise ValueError("Apple: отсутствует лента отзывов")
            entries = data["feed"].get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            entries = [e for e in entries if "im:rating" in e]
            if not entries:
                return
            rows = []
            for e in entries:
                score = int(e["im:rating"]["label"])
                if rating != "all" and score != int(rating):
                    continue
                rows.append({
                    "id": e["id"]["label"], "author": e.get("author", {}).get("name", {}).get("label", ""),
                    "rating": score, "date": e.get("updated", {}).get("label", ""),
                    "title": e.get("title", {}).get("label", ""),
                    "text": e.get("content", {}).get("label", ""), "reply": "",
                })
            yield rows
            if stop.wait(0.2):
                raise Stopped()


@dataclass
class ArchiveResult:
    files: list[Path]
    count: int
    completed: int
    total: int
    errors: int
    stopped: bool


def download_archive(app_url, store, country="all", lang="all", rating_input="all",
                     output_dir=".", stop=None, progress=None):
    """Каждая пара страна × язык запрашивается отдельно; ошибки сохраняются в отчёте."""
    if store not in {"appstore", "googleplay"}:
        raise ValueError("Неизвестный магазин")
    countries = APPLE_COUNTRIES if store == "appstore" else GOOGLE_COUNTRIES
    if country != "all" and country not in countries:
        raise ValueError("Страна отсутствует в каталоге магазина")
    if store == "googleplay" and lang != "all" and lang not in GOOGLE_LANGUAGES:
        raise ValueError("Язык отсутствует в каталоге Google Play")
    if rating_input not in {"all", "1", "2", "3", "4", "5"}:
        raise ValueError("Оценка должна быть all или 1..5")
    stop = stop if stop is not None else threading.Event()
    app_id = apple_id(app_url) if store == "appstore" else google_id(app_url)
    locales = query_locales(store, country, lang)
    pending = iter(locales)
    root = Path(output_dir)
    lock = threading.Lock()
    db = sqlite3.connect(root / "reviews.sqlite", check_same_thread=False)
    db.executescript("CREATE TABLE reviews(id TEXT PRIMARY KEY, date TEXT, data TEXT); CREATE TABLE sources(id TEXT, country TEXT, lang TEXT, PRIMARY KEY(id,country,lang));")
    status = {"completed": 0, "total": len(locales), "reviews": 0, "errors": 0}
    reports = []
    consecutive_errors = 0
    automatic_stop = False

    def notify():
        if progress:
            progress(dict(status))

    def worker():
        nonlocal consecutive_errors, automatic_stop
        while not stop.is_set():
            with lock:
                locale = next(pending, None)
            if locale is None:
                return
            c, l = locale
            report = {"country": c, "language": l, "status": "completed", "reviews": 0}
            try:
                fetch = apple_pages if store == "appstore" else google_pages
                for rows in fetch(app_id, c, l, rating_input, stop):
                    with lock:
                        for row in rows:
                            cur = db.execute("INSERT OR IGNORE INTO reviews VALUES (?,?,?)", (row["id"], row["date"], json.dumps(row, ensure_ascii=False)))
                            status["reviews"] += cur.rowcount
                            db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?)", (row["id"], c, l))
                        db.commit()
                        report["reviews"] += len(rows)
                        notify()
            except Stopped:
                report["status"] = "stopped"
            except Unavailable as exc:
                report.update(status="unavailable", error=str(exc))
            except Exception as exc:
                report.update(status="error", error=str(exc))
            with lock:
                reports.append(report)
                if report["status"] == "error":
                    status["errors"] += 1
                    consecutive_errors += 1
                    if consecutive_errors >= 12:
                        automatic_stop = True
                        stop.set()
                else:
                    consecutive_errors = 0
                if report["status"] != "stopped":
                    status["completed"] += 1
                notify()
            if stop.wait(0.15):
                return

    try:
        notify()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(worker) for _ in range(3)]
            for future in futures:
                future.result()
        manifest = {
            "app_id": app_id, "store": store, "country": country, "language": lang,
            "rating": rating_input, **status, "stopped": stop.is_set(),
            "automatic_stop_after_errors": automatic_stop,
            "catalog_updated": CATALOG["updated"], "catalog_sources": CATALOG["sources"],
            "requested_locales": locales, "queries": reports,
            "notes": ["Только публично доступные отзывы; полный исторический архив не гарантируется.",
                      "Google Play: страна/язык запроса не подтверждают страну автора/язык текста.",
                      "Apple RSS: не более 10 страниц на страну; отдельного языкового фильтра нет.",
                      "unavailable: RSS не предоставлен магазином; error: запрос не завершён.",
                      "Необработанные пары = requested_locales минус queries; stopped означает частичный сбор."],
        }
        manifest_path = root / "report.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        header = (f"# Отзывы {app_id} — {store}\n\n"
                  f"Страны: {country}. Языки: {lang if store == 'googleplay' else 'все'}. Оценки: {rating_input}.\n\n"
                  f"Уникальных отзывов: {status['reviews']}. Обработано запросов: {status['completed']}/{len(locales)}. Ошибок: {status['errors']}.\n\n"
                  + ("**Частичная выгрузка: сбор остановлен или были ошибки.**\n\n" if stop.is_set() or status["errors"] else "")
                  + "Страны и языки относятся к запросам, а не к месту жительства автора. Полнота публичной выдачи не гарантируется. Подробности в report.json.\n\n")
        files = []
        handle = None
        size = 0
        try:
            for record in db.execute("SELECT id,data FROM reviews ORDER BY date DESC,id"):
                row = json.loads(record[1])
                sources = list(db.execute("SELECT country,lang FROM sources WHERE id=? ORDER BY country,lang", (record[0],)))
                countries_text = ", ".join(sorted({s[0] for s in sources}))
                languages_text = ", ".join(sorted({s[1] for s in sources if s[1]}))
                block = (f"## {row['date']} · {row['rating']}/5 · {row['author']}\n\n"
                         f"Страны запроса: {countries_text}\n\n"
                         + (f"Языки запроса: {languages_text}\n\n" if languages_text else "")
                         + (f"**{row['title']}**\n\n" if row['title'] else "")
                         + f"{row['text']}\n\n"
                         + (f"**Ответ разработчика:** {row['reply']}\n\n" if row['reply'] else "")
                         + f"ID: {row['id']}\n\n---\n\n")
                block_size = len(block.encode("utf-8"))
                if handle is None or size + block_size > 20 * 1024 * 1024:
                    if handle:
                        handle.close()
                    path = root / f"{store}_{app_id}_reviews_{len(files)+1:03}.md"
                    files.append(path)
                    handle = path.open("w", encoding="utf-8")
                    handle.write(header)
                    size = len(header.encode("utf-8"))
                handle.write(block)
                size += block_size
            if not files:
                path = root / f"{store}_{app_id}_reviews_001.md"
                path.write_text(header + "Отзывы не получены. См. report.json.\n", encoding="utf-8")
                files.append(path)
        finally:
            if handle:
                handle.close()
        return ArchiveResult(files + [manifest_path], status["reviews"], status["completed"], len(locales), status["errors"], stop.is_set())
    finally:
        db.close()
