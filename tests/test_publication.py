import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

spec = importlib.util.spec_from_file_location("publisher", Path(__file__).parents[1] / "src/wechat_publisher.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class FakeClient:
    def __init__(self, failure=None, statuses=None):
        self.calls = []
        self.failure = failure
        self.statuses = iter(statuses or [0])
        self.article = None

    def upload(self, path, cover=False):
        self.calls.append(("cover" if cover else "image", {}))
        if self.failure == "image" and not cover:
            raise publisher.PublisherError("image failed")
        return {"media_id": "cover-id", "url": "https://mmbiz.qpic.cn/test.jpg"}

    def request(self, endpoint, payload=None, **kwargs):
        self.calls.append((endpoint, payload))
        if endpoint == self.failure:
            raise publisher.UnknownResult("result unknown")
        if endpoint == "draft/add":
            self.article = payload["articles"][0]
            return {"media_id": "draft-id"}
        if endpoint == "draft/get":
            return {"news_item": [self.article]}
        if endpoint == "draft/update":
            self.article = payload["articles"]
            return {"errcode": 0, "errmsg": "ok"}
        if endpoint == "freepublish/submit":
            return {"publish_id": "publish-id"}
        if endpoint == "freepublish/get":
            return {"publish_status": next(self.statuses, 0), "article_id": "article-id", "article_detail": {"item": [{"article_url": "https://mp.weixin.qq.com/s/test"}]}}
        raise AssertionError(endpoint)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        # These colored fixtures are API tests, never offered as generated artwork.
        Image.new("RGB", (900, 384), "navy").save(self.source / "cover.png")
        Image.new("RGB", (900, 600), "green").save(self.source / "inline.png")
        self.body = "## 观点\n\n口述内容整理为文章。**保留原意**。\n\n![示意图](inline.png)\n\n[来源](https://example.com/a?q=1&b=2)"
        self.write_article(self.body)
        self.config = {"app_id": "account-a", "app_secret": "secret"}
        self.state_root = self.root / "journals"

    def write_article(self, body):
        (self.source / "article.md").write_text(body)
        (self.source / "article.json").write_text(json.dumps({"title": "口述成稿测试", "cover": "cover.png", "markdown": "article.md", "digest": "测试摘要"}, ensure_ascii=False))

    def build(self, output=None):
        result = publisher.build(self.source / "article.json", output or self.root / "output")
        return Path(result["job"])

    def run_job(self, job, fake, publish=True):
        return publisher.run(job, self.config, publish=publish, wait=0, client=fake, state_root=self.state_root)

    def test_end_to_end_upload_replacement_and_public_url(self):
        job, fake = self.build(), FakeClient()
        result = self.run_job(job, fake)
        self.assertTrue(result["published"])
        self.assertEqual(result["article_urls"], ["https://mp.weixin.qq.com/s/test"])
        payload = next(payload for endpoint, payload in fake.calls if endpoint == "draft/add")
        article = payload["articles"][0]
        self.assertNotIn('src="asset-', article["content"])
        self.assertIn('src="https://mmbiz.qpic.cn/test.jpg"', article["content"])
        self.assertEqual(article["thumb_media_id"], "cover-id")
        self.assertIn("style=", article["content"])
        self.assertEqual(article["pic_crop_235_1"].split("_")[0], "0.000000")
        self.assertNotIn("secret", (job / "发布记录/result.json").read_text())

    def test_same_article_even_different_output_never_republishes(self):
        fake = FakeClient()
        self.run_job(self.build(), fake)
        self.run_job(self.build(self.root / "other-output"), fake)
        self.assertEqual(sum(name == "freepublish/submit" for name, _ in fake.calls), 1)
        self.assertEqual(sum(name == "draft/add" for name, _ in fake.calls), 1)

    def test_unknown_submit_cannot_be_replayed(self):
        job, fake = self.build(), FakeClient("freepublish/submit")
        with self.assertRaises(publisher.UnknownResult):
            self.run_job(job, fake)
        safe = FakeClient()
        with self.assertRaisesRegex(publisher.PublisherError, "结果未知"):
            self.run_job(job, safe)
        self.assertEqual(safe.calls, [])

    def test_unknown_draft_cannot_be_replayed(self):
        job, fake = self.build(), FakeClient("draft/add")
        with self.assertRaises(publisher.UnknownResult):
            self.run_job(job, fake)
        safe = FakeClient()
        with self.assertRaises(publisher.PublisherError):
            self.run_job(job, safe)
        self.assertEqual(safe.calls, [])

    def test_submission_is_not_reported_as_published(self):
        fake = FakeClient(statuses=[1, 0])
        job = self.build()
        first = self.run_job(job, fake)
        self.assertFalse(first["published"])
        self.assertEqual(first["stage"], "publishing")
        second = self.run_job(job, fake)
        self.assertTrue(second["published"])
        self.assertNotEqual(first["receipt"], second["receipt"])
        self.assertFalse(publisher.read_json(first["receipt"])["published"])
        self.assertTrue(publisher.read_json(second["receipt"])["published"])
        self.assertEqual(sum(name == "freepublish/submit" for name, _ in fake.calls), 1)

    def test_image_failure_prevents_draft_and_publication(self):
        fake = FakeClient("image")
        with self.assertRaises(publisher.PublisherError):
            self.run_job(self.build(), fake)
        self.assertEqual([name for name, _ in fake.calls], ["image"])

    def test_image_change_invalidates_package(self):
        job = self.build()
        manifest = publisher.read_json(job / "manifest.json")
        (job / manifest["images"][0]["path"]).write_bytes(b"tampered")
        fake = FakeClient()
        with self.assertRaisesRegex(publisher.PublisherError, "图片发生变化"):
            self.run_job(job, fake)
        self.assertEqual(fake.calls, [])

    def test_body_change_invalidates_package(self):
        job = self.build()
        (job / "文章/article.html").write_text("modified")
        with self.assertRaisesRegex(publisher.PublisherError, "任务内容发生变化"):
            self.run_job(job, FakeClient())

    def test_script_and_external_images_rejected(self):
        for body in ['<script>alert(1)</script>', '![remote](https://example.com/image.png)', '![escape](../image.png)']:
            with self.subTest(body=body):
                self.write_article(body)
                with self.assertRaises(publisher.PublisherError):
                    self.build()

    def test_theme_and_inline_preview_are_offline(self):
        job = self.build()
        preview = (job / "预览/公众号预览.html").read_text()
        self.assertIn("data:image/png;base64,", preview)
        self.assertNotIn("<script", preview)
        self.assertNotIn("<link", preview)
        self.assertNotIn("asset-", preview)

    def test_known_publication_failure_is_not_success(self):
        result = self.run_job(self.build(), FakeClient(statuses=[4]))
        self.assertFalse(result["ok"])
        self.assertFalse(result["published"])
        self.assertEqual(result["publish_status"], 4)

    def test_draft_only_does_not_submit(self):
        fake = FakeClient()
        result = self.run_job(self.build(), fake, publish=False)
        self.assertEqual(result["stage"], "draft_ready")
        self.assertNotIn("freepublish/submit", [name for name, _ in fake.calls])

    def test_personal_account_delivers_draft_even_if_publish_requested(self):
        self.config.update(subject_type="personal", publish_api_available=False, delivery="draft")
        fake = FakeClient()
        result = self.run_job(self.build(), fake, publish=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["published"])
        self.assertTrue(result["manual_publish_required"])
        self.assertTrue(result["publish_requested"])
        self.assertNotIn("freepublish/submit", [name for name, _ in fake.calls])

    def test_personal_profile_does_not_bypass_unknown_submission(self):
        job = self.build()
        with self.assertRaises(publisher.UnknownResult):
            self.run_job(job, FakeClient("freepublish/submit"))
        self.config["publish_api_available"] = False
        fake = FakeClient()
        with self.assertRaises(publisher.PublisherError):
            self.run_job(job, fake)
        self.assertEqual(fake.calls, [])

    def test_delivery_change_preserves_credentials(self):
        path = self.root / "mode/account.json"
        original = dict(self.config, delivery="publish", theme="warm")
        publisher.save_config(original, path=path)
        result = publisher.set_delivery(path, "draft", "personal")
        new = publisher.read_json(path)
        self.assertEqual(new["app_secret"], original["app_secret"])
        self.assertEqual(new["app_id"], original["app_id"])
        self.assertEqual(new["theme"], "warm")
        self.assertFalse(result["publish_api_available"])
        self.assertTrue(list(path.parent.joinpath("备份").glob("*.json")))
        with self.assertRaises(publisher.PublisherError):
            publisher.set_delivery(path, "publish")

    def test_saved_draft_does_not_imply_collection_selected(self):
        self.config["default_collection"] = "成长"
        result = self.run_job(self.build(), FakeClient(), publish=False)
        self.assertTrue(result["draft_saved"])
        self.assertFalse(result["editor_settings_complete"])
        self.assertEqual(result["delivery_status"], "awaiting_editor_settings")
        self.assertEqual(result["editor_settings"]["collection"]["requested_name"], "成长")
        self.assertFalse(result["editor_settings"]["collection"]["selection_verified"])
        self.assertTrue(result["editor_actions"])

    def test_article_collection_overrides_account_default_without_api_field(self):
        self.config["default_collection"] = "成长"
        data = publisher.read_json(self.source / "article.json")
        data["collection"] = "读书"
        publisher.write_json(self.source / "article.json", data)
        fake = FakeClient()
        result = self.run_job(self.build(), fake, publish=False)
        self.assertEqual(result["editor_settings"]["collection"]["requested_name"], "读书")
        self.assertFalse(result["editor_settings_complete"])
        self.assertNotIn("collection", fake.article)

    def test_content_only_draft_has_no_pending_editor_settings(self):
        result = self.run_job(self.build(), FakeClient(), publish=False)
        self.assertTrue(result["editor_settings_complete"])
        self.assertEqual(result["editor_actions"], [])
        self.assertFalse(result["published"])

    def test_confirmed_manual_publication_cannot_create_or_submit_again(self):
        self.config["default_collection"] = "成长"
        job = self.build()
        self.run_job(job, FakeClient(), publish=False)
        record = publisher.record_manual_publication(job, self.config, state_root=self.state_root)
        self.assertEqual(record["publication_evidence"], "user_confirmation")
        self.assertFalse(record["publication_verified"])
        self.assertFalse(record["manual_publish_required"])
        fake = FakeClient()
        result = self.run_job(job, fake)
        self.assertEqual(result["stage"], "published_manually")
        self.assertTrue(result["published"])
        self.assertFalse(result["editor_settings_complete"])
        self.assertEqual(fake.calls, [])

    def test_manual_confirmation_does_not_bypass_unknown_submission(self):
        job = self.build()
        with self.assertRaises(publisher.UnknownResult):
            self.run_job(job, FakeClient("freepublish/submit"))
        with self.assertRaises(publisher.PublisherError):
            publisher.record_manual_publication(job, self.config, state_root=self.state_root)

    def revision(self):
        old = self.build()
        self.write_article(self.body + "\n\n补充个人主体的接口限制。")
        data = publisher.read_json(self.source / "article.json")
        data.update(author="天聪", need_open_comment=1, request_original_declaration=True)
        publisher.write_json(self.source / "article.json", data)
        return old, self.build()

    def test_revision_reuses_draft_and_images_and_keeps_editor_action(self):
        old, new = self.revision()
        fake = FakeClient()
        self.run_job(old, fake, publish=False)
        result = publisher.update_draft(new, old, self.config, fake, self.state_root)
        self.assertEqual(result["draft_media_id"], "draft-id")
        self.assertTrue(result["editor_actions"])
        self.assertEqual(fake.article["author"], "天聪")
        self.assertEqual(fake.article["need_open_comment"], 1)
        self.assertNotIn("request_original_declaration", fake.article)
        self.assertIn("cover_info", fake.article)
        publisher.update_draft(new, old, self.config, fake, self.state_root)
        for endpoint in ["draft/add", "draft/update", "image", "cover"]:
            self.assertEqual(sum(name == endpoint for name, _ in fake.calls), 1)
        with self.assertRaisesRegex(publisher.PublisherError, "新版本"):
            self.run_job(old, fake)

    def test_unknown_revision_freezes_both_versions(self):
        old, new = self.revision()
        fake = FakeClient()
        self.run_job(old, fake, publish=False)
        fake.failure = "draft/update"
        with self.assertRaises(publisher.UnknownResult):
            publisher.update_draft(new, old, self.config, fake, self.state_root)
        safe = FakeClient()
        with self.assertRaises(publisher.PublisherError):
            publisher.update_draft(new, old, self.config, safe, self.state_root)
        for job in [old, new]:
            with self.assertRaises(publisher.PublisherError):
                self.run_job(job, safe)
        self.assertEqual(safe.calls, [])

    def test_revision_does_not_overwrite_user_edits(self):
        old, new = self.revision()
        fake = FakeClient()
        self.run_job(old, fake, publish=False)
        fake.article["content"] += "用户在后台补充的内容"
        with self.assertRaisesRegex(publisher.PublisherError, "已有改动"):
            publisher.update_draft(new, old, self.config, fake, self.state_root)
        self.assertNotIn("draft/update", [name for name, _ in fake.calls])

    def test_another_account_has_own_publication_journal(self):
        job, fake = self.build(), FakeClient()
        self.run_job(job, fake)
        self.config = {"app_id": "account-b", "app_secret": "secret"}
        self.run_job(job, fake)
        self.assertEqual(sum(name == "freepublish/submit" for name, _ in fake.calls), 2)

    def test_lock_prevents_simultaneous_submission(self):
        job = self.build()
        manifest, _ = publisher.checked_job(job)
        state = publisher.journal_path(self.config, manifest, self.state_root)
        with publisher.locked(state.with_suffix(".lock")):
            with self.assertRaisesRegex(publisher.PublisherError, "正在执行"):
                self.run_job(job, FakeClient())

    def test_network_error_never_prints_token_or_secret(self):
        client = publisher.Client(self.config)
        client.token = "private-access-token"
        with patch("urllib.request.urlopen", side_effect=OSError("https://example.com?access_token=private-access-token&secret=secret")):
            with self.assertRaises(publisher.UnknownResult) as error:
                client.request("freepublish/submit", {"media_id": "id"}, modifying=True)
        self.assertNotIn("private-access-token", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    def test_config_update_keeps_private_original_and_existing_preferences(self):
        config_path = self.root / "private/account.json"
        first = {"app_id": "test-account", "app_secret": "old-test-secret", "theme": "warm"}
        second = dict(first, app_secret="new-test-secret")
        with patch.object(publisher, "CONFIG", config_path):
            publisher.save_config(first)
            old_bytes = config_path.read_bytes()
            with self.assertRaises(publisher.PublisherError):
                publisher.save_config(second)
            publisher.save_config(second, update=True)
            self.assertEqual(publisher.read_json(config_path), second)
            backups = list(config_path.parent.joinpath("备份").glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), old_bytes)
            publisher.save_config(second, update=True)
            self.assertEqual(len(list(config_path.parent.joinpath("备份").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
