import re
import json
import time
import requests

def extract_app_id(app_url: str) -> str:
    """
    Извлекаем числовой id приложения из URL App Store.
    Пример: https://apps.apple.com/us/app/monster-math-kids-fun-games/id931943412
    -> 931943412
    """
    match = re.search(r'id(\d+)', app_url)
    if not match:
        raise ValueError("Не удалось найти id приложения в URL")
    return match.group(1)

def fetch_reviews(app_url: str, country: str = "us", sleep_sec: float = 0.5, rating_filter=None):
    """
    Забираем ВСЕ отзывы для приложения и сохраняем в текстовый файл.
    
    :param app_url: URL приложения в App Store
    :param country: страна (us, ru, de, fr и т.д.)
    :param sleep_sec: пауза между запросами, чтобы не спамить API
    """
    app_id = extract_app_id(app_url)
    print(f"App ID: {app_id}")

    # Формат endpoint’а:
    # https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortBy=mostRecent/json
    page = 1
    all_reviews = []
    
    while True:
        rss_url = (
            f"https://itunes.apple.com/{country}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortBy=mostRecent/json"
        )
        print(f"Запрашиваю страницу {page}: {rss_url}")
        
        resp = requests.get(rss_url, timeout=15)
        if resp.status_code != 200:
            print(f"Страница {page}: status {resp.status_code}, прекращаю.")
            break
        
        data = resp.json()
        entries = data.get("feed", {}).get("entry", [])

        # На первой странице первый entry — это само приложение, не отзыв
        if page == 1 and entries:
            entries = entries[1:]
        
        if not entries:
            print("Отзывов больше нет, выхожу из цикла.")
            break
        
        for entry in entries:
            # Текст отзыва
            content = entry.get("content", {}).get("label", "").strip()
            title = entry.get("title", {}).get("label", "").strip()
            author = entry.get("author", {}).get("name", {}).get("label", "").strip()
            rating = entry.get("im:rating", {}).get("label", "")
            date = entry.get("updated", {}).get("label", "")

            # 🔹 Фильтр по оценке
            if rating_filter is not None and rating != str(rating_filter):
                continue
            
            review_text = (
                f"Автор: {author}\n"
                f"Оценка: {rating}\n"
                f"Дата: {date}\n"
                f"Заголовок: {title}\n"
                f"Текст: {content}\n"
                "---------------------------\n"
            )
            all_reviews.append(review_text)
        
        page += 1
        time.sleep(sleep_sec)

    if not all_reviews:
        print("Отзывы не найдены.")
        return
    
    filename = f"reviews_{app_id}_{country.lower()}_{rating_input}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        for r in all_reviews:
            f.write(r)
    
    print(f"Готово! Сохранено {len(all_reviews)} отзывов в файл {filename}")

if __name__ == "__main__":
    # Спрашиваем у пользователя URL
    app_url = input("Вставь URL игры из App Store и нажми Enter:\n> ").strip()

    if not app_url:
        print("URL не может быть пустым. Попробуй ещё раз.")
        exit(1)

    # Спрашиваем у пользователя страну
    country = input(
        "Из какой страны нужны отзывы? "
        "Введи двухбуквенный код магазина (например: us, ru, de, fr).\n"
        "Если оставить пустым, будет использовано 'us'.\n> "
    ).strip().lower()

    if not country:
        country = "us"
        print("Страна не введена, использую по умолчанию: us")
    elif len(country) != 2:
        print("Некорректный код страны, использую по умолчанию: us")
        country = "us"

    rating_input = input(
        "Отзывы с какой оценкой нужны? Введи число от 1 до 5.\n"
        "Если оставить пустым, возьмём все оценки.\n> "
    ).strip()

    rating_filter = None
    if rating_input:
        if rating_input in {"1", "2", "3", "4", "5"}:
            rating_filter = int(rating_input)
            print(f"Буду сохранять только отзывы с оценкой {rating_filter}.")
        else:
            print("Некорректная оценка, буду брать отзывы с любой оценкой.")

    fetch_reviews(app_url, country=country, rating_filter=rating_filter)

