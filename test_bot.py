"""Проверки диалогов и отправки файлов без подключения к Telegram."""

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

with patch.dict(os.environ, {"BOT_TOKEN": "test-token"}):
    import bot

import appstore_reviews
import googleplay_reviews


def update_for(text="", user_id=1):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.sessions.clear()
        self.tasks = []

        def create_task(coro, **kwargs):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        self.context = SimpleNamespace(
            application=SimpleNamespace(create_task=Mock(side_effect=create_task)),
            bot=SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock()),
        )

    async def asyncTearDown(self):
        await asyncio.gather(*self.tasks)
        bot.sessions.clear()

    async def say(self, text):
        update = update_for(text)
        await bot.handle_text(update, self.context)
        return update

    async def test_googleplay_flow_validates_and_dispatches_selected_language(self):
        await bot.start(update_for(), self.context)
        await self.say("https://play.google.com/store/apps/details?hl=en&id=com.ululab.numbers&gl=us")
        self.assertEqual(bot.sessions[1].store, "googleplay")
        await self.say("Mexico")
        self.assertEqual(bot.sessions[1].step, "country")
        await self.say("mx")
        self.assertEqual(bot.sessions[1].step, "language")
        await self.say("Spanish")
        self.assertEqual(bot.sessions[1].step, "language")
        await self.say("es")
        await self.say("6")
        self.assertEqual(bot.sessions[1].step, "rating")
        with patch.object(bot, "_download_and_send", new_callable=AsyncMock) as download:
            await self.say("1")
            await asyncio.gather(*self.tasks)
            download.assert_awaited_once()
            session = download.call_args.args[3]
            self.assertEqual((session.store, session.country, session.lang, session.rating_input), ("googleplay", "mx", "es", "1"))
            await self.say("another request")
            self.assertEqual(self.context.application.create_task.call_count, 1)

    async def test_appstore_flow_skips_language(self):
        await bot.start(update_for(), self.context)
        await self.say("https://apps.apple.com/us/app/example/id123456789")
        await self.say("default (us)")
        self.assertEqual(bot.sessions[1].step, "rating")
        with patch.object(bot, "_download_and_send", new_callable=AsyncMock) as download:
            await self.say("all")
            await asyncio.gather(*self.tasks)
            session = download.call_args.args[3]
            self.assertEqual((session.store, session.country, session.rating_input), ("appstore", "us", "all"))

    async def test_cancel_and_start_reset_dialog(self):
        await bot.start(update_for(), self.context)
        await self.say("https://play.google.com/store/apps/details?id=com.example")
        await bot.cancel(update_for(), self.context)
        self.assertNotIn(1, bot.sessions)
        await bot.start(update_for(), self.context)
        self.assertEqual(bot.sessions[1].step, "url")

    async def test_each_store_sends_its_file_and_removes_temp_directory(self):
        for store in ("appstore", "googleplay"):
            with self.subTest(store=store):
                session = bot.UserSession(step="downloading", url="url", store=store, country="fr", lang="fr")
                bot.sessions[1] = session
                paths = []

                def download(**kwargs):
                    path = Path(kwargs["output_dir"], "reviews.md")
                    path.write_text("review text", encoding="utf-8")
                    paths.append(path)
                    return str(path), 1

                async def send(**kwargs):
                    self.assertEqual(kwargs["document"].read(), b"review text")
                    self.assertEqual(kwargs["filename"], "reviews.md")
                    self.assertIn("Отзывов: 1", kwargs["caption"])
                    self.assertEqual("Язык запроса: fr" in kwargs["caption"], store == "googleplay")

                self.context.bot.send_document.side_effect = send
                with patch.object(bot, "download_reviews_to_md_file", side_effect=download) as apple, patch.object(bot, "download_googleplay", side_effect=download) as google:
                    await bot._download_and_send(self.context, 1, 1, session)
                    selected, other = (google, apple) if store == "googleplay" else (apple, google)
                    selected.assert_called_once()
                    other.assert_not_called()
                    if store == "googleplay":
                        self.assertEqual(selected.call_args.kwargs["lang"], "fr")
                self.assertFalse(paths[0].parent.exists())
                self.assertNotIn(1, bot.sessions)

    async def test_failure_cleans_files_without_erasing_new_session(self):
        old = bot.UserSession(step="downloading", store="googleplay")
        new = bot.UserSession()
        bot.sessions[1] = new
        folders = []

        def fail(**kwargs):
            path = Path(kwargs["output_dir"])
            (path / "partial.md").write_text("partial")
            folders.append(path)
            raise RuntimeError("network error")

        with patch.object(bot, "download_googleplay", side_effect=fail), self.assertLogs(bot.logger, level="ERROR"):
            await bot._download_and_send(self.context, 1, 1, old)
        self.assertIs(bot.sessions[1], new)
        self.assertFalse(folders[0].exists())
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.send_document.assert_not_awaited()

    async def test_concurrent_identical_requests_use_separate_files(self):
        barrier = threading.Barrier(2, timeout=5)
        paths = []

        def download(**kwargs):
            path = Path(kwargs["output_dir"], "same.md")
            path.write_text("same review")
            paths.append(path)
            barrier.wait()
            return str(path), 1

        async def send(**kwargs):
            self.assertEqual(kwargs["document"].read(), b"same review")

        self.context.bot.send_document.side_effect = send
        sessions = [bot.UserSession(step="downloading", store="googleplay") for _ in range(2)]
        bot.sessions.update(enumerate(sessions, 1))
        with patch.object(bot, "download_googleplay", side_effect=download):
            await asyncio.gather(*(bot._download_and_send(self.context, i, i, s) for i, s in enumerate(sessions, 1)))
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual(self.context.bot.send_document.await_count, 2)
        self.assertTrue(all(not path.parent.exists() for path in paths))


class InputAndExportTests(unittest.TestCase):
    def test_store_validation_and_language_defaults(self):
        for url in ("https://evil.test/?url=https://apps.apple.com/us/app/test/id123", "https://play.google.com/store/apps/details?hl=es", "https://play.google.com.evil.test/store/apps/details?id=com.example", "com.example"):
            with self.subTest(url=url):
                self.assertIsNone(bot._detect_store(url))
        self.assertEqual(bot._normalize_language("default (en)"), "en")
        self.assertEqual(bot._normalize_language("he"), "iw")

    def test_real_exporters_respect_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(googleplay_reviews, "fetch_rating", return_value={"name": "App"}), patch.object(googleplay_reviews, "fetch_reviews", return_value=[]):
                filename, count = googleplay_reviews.download_reviews_to_md_file("com.example", "us", "all", output_dir=tmp)
                self.assertEqual(Path(filename).parent, Path(tmp))
                self.assertEqual(count, 0)
                self.assertTrue(Path(filename).exists())
            response = Mock(status_code=200)
            response.json.return_value = {"feed": {"entry": []}}
            with patch.object(appstore_reviews.requests, "get", return_value=response):
                filename, count = appstore_reviews.download_reviews_to_md_file("https://apps.apple.com/us/app/test/id123", "us", "all", output_dir=tmp)
                self.assertEqual(Path(filename).parent, Path(tmp))
                self.assertEqual(count, 0)
                self.assertTrue(Path(filename).exists())


if __name__ == "__main__":
    unittest.main()
