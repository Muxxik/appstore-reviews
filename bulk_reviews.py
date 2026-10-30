"""Массовый сбор отзывов App Store или Google Play в JSON/CSV.

Пример:
    python bulk_reviews.py --ids 6746055015,6760271556 --countries us,de,ru --out data/run
"""

import argparse
import csv
import json
import time
from pathlib import Path

import requests

MAX_PAGES = 10  # лимит RSS Apple: 10 страниц × 50 отзывов


def fetch_ratings(app_ids: list[str], country: str, timeout_sec: int = 15) -> dict[str, dict]:
    """Возвращает название, число оценок и средний балл по каждому приложению в стране."""
    url = f"https://itunes.apple.com/lookup?id={','.join(app_ids)}&country={country}"
    resp = requests.get(url, timeout=timeout_sec)
    resp.raise_for_status()
    return {
        str(r["trackId"]): {
            "name": r.get("trackName", ""),
            "rating_count": r.get("userRatingCount", 0),
            "rating_avg": round(r.get("averageUserRating", 0.0), 2),
        }
        for r in resp.json().get("results", [])
    }


def fetch_reviews(app_id: str, country: str, sleep_sec: float = 0.4, timeout_sec: int = 15) -> list[dict]:
    """Скачивает все доступные через RSS отзывы приложения в стране."""
    reviews: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = (
            f"https://itunes.apple.com/{country}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortBy=mostRecent/json"
        )
        for attempt in range(3):
            resp = requests.get(url, timeout=timeout_sec)
            if resp.status_code not in (403, 429, 503):
                break
            time.sleep(3 * (attempt + 1))
        if resp.status_code != 200:
            break

        entries = resp.json().get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        # Отзывы отличаем от карточки самого приложения по наличию оценки
        entries = [e for e in entries if "im:rating" in e]
        if not entries:
            break

        for e in entries:
            reviews.append({
                "review_id": e.get("id", {}).get("label", ""),
                "app_id": app_id,
                "country": country,
                "rating": int(e["im:rating"]["label"]),
                "date": e.get("updated", {}).get("label", "")[:10],
                "version": e.get("im:version", {}).get("label", ""),
                "author": e.get("author", {}).get("name", {}).get("label", "").strip(),
                "title": e.get("title", {}).get("label", "").strip(),
                "text": e.get("content", {}).get("label", "").strip(),
            })
        time.sleep(sleep_sec)
    return reviews


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", required=True, help="id приложений через запятую")
    parser.add_argument("--countries", required=True, help="коды стран через запятую")
    parser.add_argument("--out", required=True, help="папка для результатов")
    parser.add_argument("--store", choices=["appstore", "googleplay"], default="appstore")
    parser.add_argument("--lang", default="en", help="язык отзывов Google Play (по умолчанию en)")
    parser.add_argument("--rating", choices=["all", "1", "2", "3", "4", "5"], default="all")
    parser.add_argument("--max-reviews", type=int, default=0, help="лимит на приложение и страну; 0 = без лимита")
    args = parser.parse_args()
    if args.max_reviews < 0:
        parser.error("--max-reviews должен быть неотрицательным")

    app_ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    countries = [x.strip().lower() for x in args.countries.split(",") if x.strip()]
    if not app_ids or not countries:
        parser.error("Нужны непустые --ids и --countries")
    if args.store == "googleplay":
        import googleplay_reviews as googleplay

        try:
            app_ids = [googleplay.extract_app_id(x) for x in app_ids]
            for country in countries:
                googleplay.normalize_options(country, args.lang, args.rating)
        except ValueError as exc:
            parser.error(str(exc))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ratings: dict[str, dict[str, dict]] = {}  # app_id -> country -> {...}
    all_reviews: list[dict] = []

    for country in countries:
        if args.store == "appstore":
            for app_id, info in fetch_ratings(app_ids, country).items():
                ratings.setdefault(app_id, {})[country] = info
        for app_id in app_ids:
            if args.store == "googleplay":
                ratings.setdefault(app_id, {})[country] = googleplay.fetch_rating(app_id, country, args.lang)
                got = googleplay.fetch_reviews(app_id, country, args.lang, args.rating, args.max_reviews)
            else:
                got = fetch_reviews(app_id, country)
                if args.rating != "all":
                    got = [row for row in got if row["rating"] == int(args.rating)]
                if args.max_reviews:
                    got = got[:args.max_reviews]
            all_reviews.extend(got)
            print(f"{country} {app_id}: {len(got)}", flush=True)

    (out / "ratings.json").write_text(json.dumps(ratings, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "reviews.json").write_text(json.dumps(all_reviews, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "reviews.csv", "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_reviews[0].keys()) if all_reviews else ["app_id"])
        writer.writeheader()
        writer.writerows(all_reviews)
    print(f"Готово: {len(all_reviews)} отзывов → {out}")


if __name__ == "__main__":
    main()
