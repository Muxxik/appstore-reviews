from appstore_reviews import download_reviews_to_md_file


def main() -> None:
    app_url = input("Вставь URL игры из App Store и нажми Enter:\n> ").strip()
    if not app_url:
        print("URL не может быть пустым.")
        return

    country = input(
        "Из какой страны нужны отзывы? Введи двухбуквенный код (например: us, ru, de, fr).\n"
        "Если оставить пустым, будет 'us'.\n> "
    ).strip()

    rating_input = input(
        "Отзывы с какой оценкой нужны? Введи 1..5.\n"
        "Если оставить пустым — возьмём все (будет 'all').\n> "
    ).strip()

    filename = download_reviews_to_md_file(
        app_url=app_url,
        country=country,
        rating_input=rating_input,
    )
    print(f"Готово! Файл: {filename}")


if __name__ == "__main__":
    main()
