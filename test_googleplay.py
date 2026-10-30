"""Проверки пагинации, экспорта и совместимости консольных скриптов без сети."""

import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bulk_reviews
import googleplay_reviews as gp
import ratings


def review(review_id, score=1):
    return {"reviewId": review_id, "score": score, "content": "Текст отзыва",
            "userName": "Автор", "at": datetime(2026, 1, 2), "reviewCreatedVersion": None}


class GooglePlayTests(unittest.TestCase):
    def test_url_and_package_validation(self):
        self.assertEqual(gp.extract_app_id("https://play.google.com/store/apps/details?id=com.example.app&hl=ru"), "com.example.app")
        self.assertEqual(gp.extract_app_id("com.example.app"), "com.example.app")
        for value in ("../../file", "https://evil.test/store/apps/details?id=com.example", "https://play.google.com/store/apps/details?hl=ru", "1234"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gp.extract_app_id(value)

    def test_pagination_deduplication_filter_and_limit(self):
        first = SimpleNamespace(token="page2")
        with patch.object(gp, "reviews", side_effect=[
            ([review("a"), review("b", 5)], first),
            ([review("a"), review("c"), review("d")], SimpleNamespace(token=None)),
        ]) as fetch:
            rows = gp.fetch_reviews("com.example", "ru", "ru", "1", max_reviews=2, sleep_sec=0)
        self.assertEqual([r["review_id"] for r in rows], ["a", "c"])
        self.assertEqual(rows[0]["date"], "2026-01-02T00:00:00")
        self.assertEqual(fetch.call_args.kwargs["continuation_token"], first)
        self.assertEqual(fetch.call_args.kwargs["filter_score_with"], 1)
        self.assertEqual(fetch.call_args.kwargs["lang"], "ru")

    def test_empty_result_and_repeated_cursor(self):
        with patch.object(gp, "reviews", return_value=([], SimpleNamespace(token=None))):
            self.assertEqual(gp.fetch_reviews("com.example"), [])
        with patch.object(gp, "reviews", return_value=([review("a")], SimpleNamespace(token="same"))):
            with self.assertRaises(RuntimeError):
                gp.fetch_reviews("com.example", sleep_sec=0)

    def test_markdown_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                with patch.object(gp, "fetch_rating", return_value={"name": "Приложение"}), patch.object(gp, "reviews", return_value=([review("a")], SimpleNamespace(token=None))):
                    filename, count = gp.download_reviews_to_md_file("com.example", "ru", "1", "ru")
                self.assertEqual(count, 1)
                self.assertIn("_ru_ru_1.md", filename)
                self.assertIn("Текст отзыва", Path(filename).read_text(encoding="utf-8"))
            finally:
                os.chdir(previous)

    def test_cli_routes_each_store(self):
        for url, answers, target in [
            ("https://apps.apple.com/us/app/test/id123", ["us", "1"], "ratings.download_reviews_to_md_file"),
            ("https://play.google.com/store/apps/details?id=com.example", ["ru", "ru", "1"], "googleplay_reviews.download_reviews_to_md_file"),
        ]:
            with self.subTest(url=url), patch("builtins.input", side_effect=[url, *answers]), patch(target, return_value=("out.md", 1)) as download, patch("sys.stdout", new=io.StringIO()):
                ratings.main()
                download.assert_called_once()

    def test_bulk_exports_and_preserves_appstore_default(self):
        for store in ("appstore", "googleplay"):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as tmp:
                args = ["bulk_reviews.py", "--ids", "com.example" if store == "googleplay" else "123", "--countries", "ru", "--out", tmp]
                if store == "googleplay":
                    args += ["--store", store, "--lang", "ru"]
                rows = [{"review_id": "a", "text": "Текст", "rating": 1}]
                with patch("sys.argv", args), patch("sys.stdout", new=io.StringIO()), patch.object(bulk_reviews, "fetch_ratings", return_value={"123": {"name": "App"}}), patch.object(bulk_reviews, "fetch_reviews", return_value=rows) as apple, patch.object(gp, "fetch_rating", return_value={"name": "App"}), patch.object(gp, "fetch_reviews", return_value=rows) as google:
                    bulk_reviews.main()
                (google if store == "googleplay" else apple).assert_called_once()
                (apple if store == "googleplay" else google).assert_not_called()
                self.assertEqual(json.loads(Path(tmp, "reviews.json").read_text()), rows)
                with Path(tmp, "reviews.csv").open(encoding="utf-8-sig") as file:
                    self.assertEqual(list(csv.DictReader(file))[0]["text"], "Текст")


if __name__ == "__main__":
    unittest.main()
