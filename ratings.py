from appstore_reviews import download_reviews_to_md_file
from urllib.parse import urlparse


def main() -> None:
    app_url = input("Вставь URL приложения из App Store или Google Play и нажми Enter:\n> ").strip()
    if not app_url:
        print("URL не может быть пустым.")
        return

    host = urlparse(app_url).hostname
    if host not in {"apps.apple.com", "itunes.apple.com", "play.google.com"}:
        print("Нужна ссылка на приложение из App Store или Google Play.")
        return
    is_googleplay = host == "play.google.com"

    country = input(
        "Из какой страны нужны отзывы? Введи двухбуквенный код (например: us, ru, de, fr).\n"
        "Если оставить пустым, будет 'us'.\n> "
    ).strip()

    lang = "en"
    if is_googleplay:
        lang = input("Язык отзывов (en, ru, de, tr…), по умолчанию en:\n> ").strip() or "en"

    rating_input = input(
        "Отзывы с какой оценкой нужны? Введи 1..5.\n"
        "Если оставить пустым — возьмём все (будет 'all').\n> "
    ).strip()

    try:
        if is_googleplay:
            from googleplay_reviews import download_reviews_to_md_file as download_googleplay

            filename, reviews_count = download_googleplay(app_url, country, rating_input, lang=lang)
        else:
            filename, reviews_count = download_reviews_to_md_file(
                app_url=app_url, country=country, rating_input=rating_input,
            )
    except Exception as exc:
        raise SystemExit(f"Не удалось скачать отзывы: {exc}") from exc
    print(f"Готово! Файл: {filename} (отзывов: {reviews_count})")


if __name__ == "__main__":
    main()
