import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import review_archive as archive


def row(review_id, score=1):
    return dict(id=review_id, rating=score, date="2026-10-01", author="Автор", title="", text="Отзыв", reply="")


class ArchiveTests(unittest.TestCase):
    def test_all_means_full_country_language_product(self):
        pairs = archive.query_locales("googleplay")
        self.assertEqual(len(pairs), len(archive.GOOGLE_COUNTRIES) * len(archive.GOOGLE_LANGUAGES))
        self.assertIn(("mx", "es"), pairs)
        self.assertIn(("jp", "fr-ca"), pairs)
        self.assertEqual(archive.query_locales("googleplay", "fr", "es-419"), [("fr", "es-419")])
        self.assertEqual(len(archive.query_locales("appstore")), len(archive.APPLE_COUNTRIES))
        self.assertEqual(archive.query_locales("appstore", "fr"), [("fr", "")])

    def test_dedup_keeps_provenance_and_queries_every_pair(self):
        locales = [("us", "en"), ("fr", "en"), ("fr", "fr")]
        calls = []

        def pages(app_id, country, lang, rating, stop):
            calls.append((country, lang, rating))
            yield [row("shared")]
            yield [row("shared"), row(country + lang)]

        with tempfile.TemporaryDirectory() as tmp, patch.object(archive, "query_locales", return_value=locales), patch.object(archive, "google_pages", side_effect=pages):
            result = archive.download_archive("com.example", "googleplay", rating_input="1", output_dir=tmp)
            self.assertEqual(result.count, 4)
            self.assertEqual(result.completed, 3)
            self.assertEqual(result.errors, 0)
            self.assertEqual(set(calls), {(c, l, "1") for c, l in locales})
            content = result.files[0].read_text()
            self.assertEqual(content.count("ID: shared"), 1)
            self.assertIn("Страны запроса: fr, us", content)
            report = json.loads(result.files[-1].read_text())
            self.assertEqual(report["reviews"], 4)
            self.assertEqual(len(report["queries"]), 3)

    def test_stop_preserves_rows_already_downloaded(self):
        stop = threading.Event()

        def pages(*args):
            yield [row("saved")]
            stop.set()
            raise archive.Stopped()

        with tempfile.TemporaryDirectory() as tmp, patch.object(archive, "google_pages", side_effect=pages):
            result = archive.download_archive("com.example", "googleplay", "us", "en", output_dir=tmp, stop=stop)
            self.assertEqual(result.count, 1)
            self.assertTrue(result.stopped)
            self.assertIn("Частичная", result.files[0].read_text())

    def test_error_is_not_reported_as_successful_empty_query(self):
        def pages(*args):
            yield [row("saved")]
            raise ValueError("bad response")

        with tempfile.TemporaryDirectory() as tmp, patch.object(archive, "google_pages", side_effect=pages):
            result = archive.download_archive("com.example", "googleplay", "us", "en", output_dir=tmp)
            self.assertEqual(result.errors, 1)
            self.assertEqual(result.count, 1)
            report = json.loads(result.files[-1].read_text())
            self.assertEqual(report["queries"][0]["status"], "error")

    def test_apple_keeps_first_actual_review_and_filters_rating(self):
        entry = {"id": {"label": "first"}, "im:rating": {"label": "1"}, "content": {"label": "text"}}
        response = Mock(status_code=200)
        response.json.return_value = {"feed": {"entry": [entry]}}
        empty = Mock(status_code=200)
        empty.json.return_value = {"feed": {"entry": []}}
        with patch.object(archive, "_request", side_effect=[response, empty]) as request:
            rows = [r for page in archive.apple_pages("123", "us", "", "1", threading.Event()) for r in page]
        self.assertEqual([r["id"] for r in rows], ["first"])
        self.assertIn("sortby=mostrecent", request.call_args_list[0].args[2])

    def test_google_empty_null_payload_is_valid_but_unknown_response_fails(self):
        response = Mock(text="payload")
        with patch.object(archive, "_request", return_value=response), patch.object(archive.gp, "Regex", REVIEWS=Mock(findall=Mock(return_value=[json.dumps([["wrb.fr", "oCPfdb", "[null,null,[]]"]])]))):
            self.assertEqual(list(archive.google_pages("com.example", "us", "en", "all", threading.Event())), [])
        with patch.object(archive, "_request", return_value=response), patch.object(archive.gp, "Regex", REVIEWS=Mock(findall=Mock(return_value=[]))):
            with self.assertRaises(ValueError):
                list(archive.google_pages("com.example", "us", "en", "all", threading.Event()))


if __name__ == "__main__":
    unittest.main()
