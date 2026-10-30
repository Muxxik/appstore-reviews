# App Store / Google Play Reviews Downloader

Консольные скрипты скачивают отзывы App Store и Google Play в Markdown или JSON/CSV. Telegram-бот поддерживает только App Store.

## Возможности

- Telegram-бот:
  - диалог: URL → страна → рейтинг (с кнопками для дефолтов)
  - скачивает отзывы через публичное RSS-API Apple
  - формирует `.md` файл и отправляет его в чат
  - во время скачивания остаётся отзывчивым
  - после отправки удаляет файл локально
- CLI-скрипт (`ratings.py`):
  - распознаёт магазин по ссылке и сохраняет отзывы в Markdown
  - для Google Play позволяет выбрать страну, язык и оценку
- Массовая выгрузка (`bulk_reviews.py`): несколько приложений и стран в JSON/CSV

## Формат выходного файла

Файл создаётся по шаблону:

`{app_id}_reviews_{country.lower()}_{rating_input}.md`

Примеры:

- `1195601592_reviews_us_all.md`
- `1195601592_reviews_ru_5.md`

`rating_input`:
- `1..5` — только отзывы с этой оценкой
- `all` — все оценки

Для Google Play: `googleplay_{package_id}_reviews_{country}_{lang}_{rating}.md`.
Например: `googleplay_com.duolingo_reviews_us_en_1.md`.

## Структура проекта

- `appstore_reviews.py` — ядро: скачивание отзывов и генерация `.md`
- `googleplay_reviews.py` — загрузка отзывов Google Play, пагинация и генерация `.md`
- `bulk_reviews.py` — массовая выгрузка в JSON/CSV
- `bot.py` — Telegram-бот (polling)
- `ratings.py` — CLI-версия
- `requirements.txt` — зависимости
- `.env` — токен бота (не коммитится)

## Установка (локально)

Требуется Python 3.10 или новее.

1) Клонировать репозиторий и перейти в папку проекта:

```bash
git clone <REPO_URL>
cd <REPO_FOLDER>
```

2) Создать и активировать виртуальное окружение:

```bash
python3 -m venv venv
source venv/bin/activate
```

3) Установить зависимости:

```bash
pip install -r requirements.txt
```

## Запуск CLI

```bash
source venv/bin/activate
python ratings.py
```

Скрипт спросит:
- URL приложения (App Store или `https://play.google.com/store/apps/details?id=com.duolingo`)
- код страны (`us`, `ru`, `de`, ...)
- язык отзывов для Google Play (`en`, `ru`, `de`, `tr`, ...; по умолчанию `en`)
- рейтинг (`1..5` или `all`)

При первом запуске после обновления установи зависимости: `python -m pip install -r requirements.txt`.

## Массовая выгрузка

App Store (прежняя команда работает):

```bash
python bulk_reviews.py --ids 6746055015,6760271556 --countries us,de,ru --out data/appstore
```

Google Play (в `--ids` указываются package ID или ссылки через запятую):

```bash
python bulk_reviews.py --store googleplay --ids com.duolingo --countries ru,kz --lang ru --rating 1 --out data/googleplay-ru
```

Необязательный `--max-reviews 1000` ограничивает число отзывов на приложение и страну; по умолчанию лимита нет. `--rating all` собирает все оценки. Сначала загружаются самые свежие отзывы.

В выходной папке создаются `reviews.json`, `reviews.csv` и `ratings.json` (название, число оценок, средний балл). Повторный запуск в ту же папку перезаписывает эти файлы; для разных выгрузок используй разные папки. Google Play добавляет поле `language`; поле `title` пустое, поскольку отдельного заголовка у этих отзывов нет.

## Запуск Telegram-бота (локально)

1) Создать `.env` в корне проекта:

```env
BOT_TOKEN=ваш_токен_бота
```

2) Запустить:

```bash
source venv/bin/activate
python bot.py
```

В Telegram:
- `/start`
- следовать шагам бота


## Примечания

- Apple RSS-API отдаёт доступный набор отзывов для выбранной страны; скрипт скачивает всё, что отдаёт этот API.
- Google Play использует неофициальную библиотеку [google-play-scraper](https://github.com/JoMingyu/google-play-scraper). Ключи и доступ к аккаунту разработчика не нужны. Полнота выгрузки зависит от публичной выдачи Google Play; библиотека может завершить выдачу при сетевой ошибке, поэтому пустой или короткий результат не гарантирует отсутствие других отзывов.
- Для Google Play `country` — страна запроса, а не подтверждённая страна автора. Страна и язык задаются независимо; отзывы могут пересекаться между выгрузками. Внутри одного запроса дубликаты удаляются по `review_id`; между странами сохраняются для сравнения выдачи.
- Логи выводятся в консоль/панель хостинга и помогают понимать, сколько времени заняла загрузка и сколько отзывов скачано.
