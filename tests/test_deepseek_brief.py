import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import requests

from scripts.update_news import (
    build_deepseek_brief_prompt,
    deepseek_status_base,
    maybe_generate_daily_sections_brief,
    parse_deepseek_brief_response,
)


NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)


def make_story(title, url, latest_at="2026-06-19T10:00:00Z", **extra):
    story = {
        "title": title,
        "url": url,
        "source_name": "Example",
        "source_count": 1,
        "category": "model_release",
        "latest_at": latest_at,
    }
    story.update(extra)
    return story


class DeepseekStatusBaseTests(unittest.TestCase):
    def test_disabled_by_default_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            status = deepseek_status_base(NOW)
        self.assertTrue(status["enable_toggle"])
        self.assertFalse(status["api_key_present"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["enabled_by"], "no_api_key")

    def test_enabled_when_key_present(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
            status = deepseek_status_base(NOW)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["enabled_by"], "ready")

    def test_toggle_can_force_off_even_with_key(self):
        env = {"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_ENABLED": "0"}
        with patch.dict("os.environ", env, clear=True):
            status = deepseek_status_base(NOW)
        self.assertFalse(status["enable_toggle"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["enabled_by"], "disabled_by_toggle")


class BuildDeepseekBriefPromptTests(unittest.TestCase):
    def test_prompt_includes_story_fields_and_respects_max_items(self):
        stories = [
            make_story("Older story", "https://example.com/a", latest_at="2026-06-19T01:00:00Z"),
            make_story("Newer story", "https://example.com/b", latest_at="2026-06-19T09:00:00Z"),
        ]
        prompt = build_deepseek_brief_prompt(stories, max_items=1)
        self.assertIn("Newer story", prompt)
        self.assertIn("https://example.com/b", prompt)
        self.assertNotIn("Older story", prompt)

    def test_prompt_skips_stories_missing_title_or_url(self):
        stories = [
            {"title": "", "url": "https://example.com/a"},
            {"title": "No URL"},
            make_story("Valid story", "https://example.com/c"),
        ]
        prompt = build_deepseek_brief_prompt(stories, max_items=10)
        self.assertIn("Valid story", prompt)
        self.assertNotIn("No URL", prompt)


class ParseDeepseekBriefResponseTests(unittest.TestCase):
    def test_parses_all_four_sections(self):
        raw = {
            "tech": [{"title": "T1", "url": "https://example.com/t1", "summary": "s1"}],
            "finance": [{"title": "F1", "url": "https://example.com/f1", "summary": "s2"}],
            "academic": [],
            "gossip": [{"title": "G1", "url": "https://example.com/g1", "summary": "s3"}],
        }
        sections = parse_deepseek_brief_response(raw)
        self.assertEqual(sections["tech"]["label"], "科技")
        self.assertEqual(len(sections["tech"]["items"]), 1)
        self.assertEqual(sections["academic"]["items"], [])
        self.assertEqual(sections["gossip"]["items"][0]["title"], "G1")

    def test_missing_keys_and_malformed_entries_fall_back_to_empty_lists(self):
        raw = {"tech": [{"title": "T1"}, {"title": "T2", "url": "https://example.com/t2"}, "not-a-dict"]}
        sections = parse_deepseek_brief_response(raw)
        self.assertEqual(len(sections["tech"]["items"]), 1)
        self.assertEqual(sections["finance"]["items"], [])
        self.assertEqual(sections["academic"]["items"], [])
        self.assertEqual(sections["gossip"]["items"], [])

    def test_non_dict_response_returns_none(self):
        self.assertIsNone(parse_deepseek_brief_response(["not", "a", "dict"]))
        self.assertIsNone(parse_deepseek_brief_response(None))


class MaybeGenerateDailySectionsBriefTests(unittest.TestCase):
    def setUp(self):
        self.stories = [make_story("Story one", "https://example.com/one")]

    def test_disabled_does_not_call_network_and_does_not_touch_previous_payload(self):
        class NoNetworkSession:
            def post(self, *args, **kwargs):
                raise AssertionError("DeepSeek should stay offline unless explicitly enabled")

        previous = {"generated_at": "yesterday", "sections": {}}
        with patch.dict("os.environ", {}, clear=True):
            payload, status = maybe_generate_daily_sections_brief(
                NoNetworkSession(), NOW, self.stories, None, previous
            )
        self.assertIsNone(payload)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["disabled_reason"], "no_api_key")

    def test_throttled_returns_previous_payload_without_calling_network(self):
        class NoNetworkSession:
            def post(self, *args, **kwargs):
                raise AssertionError("should wait for the daily run window, not call now")

        previous = {"generated_at": "yesterday", "sections": {"tech": {"label": "科技", "items": []}}}
        paid_source_state = {
            "sources": {"deepseek_brief": {"last_run_at": "2026-06-19T11:00:00Z"}}
        }
        env = {"DEEPSEEK_API_KEY": "sk-test"}
        with patch.dict("os.environ", env, clear=True):
            payload, status = maybe_generate_daily_sections_brief(
                NoNetworkSession(), NOW, self.stories, paid_source_state, previous
            )
        self.assertEqual(payload, previous)
        self.assertTrue(status["skipped"])

    def test_successful_call_returns_new_payload(self):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "tech": [
                                            {
                                                "title": "Story one",
                                                "url": "https://example.com/one",
                                                "summary": "摘要",
                                            }
                                        ],
                                        "finance": [],
                                        "academic": [],
                                        "gossip": [],
                                    }
                                )
                            }
                        }
                    ]
                }

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def post(self, *args, **kwargs):
                self.calls += 1
                return FakeResponse()

        session = FakeSession()
        env = {"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_FORCE_RUN": "1"}
        with patch.dict("os.environ", env, clear=True):
            payload, status = maybe_generate_daily_sections_brief(session, NOW, self.stories, None, None)
        self.assertEqual(session.calls, 1)
        self.assertTrue(status["ok"])
        self.assertEqual(status["item_count"], 1)
        self.assertEqual(payload["sections"]["tech"]["items"][0]["title"], "Story one")

    def test_api_failure_falls_back_to_previous_payload(self):
        class FailingSession:
            def post(self, *args, **kwargs):
                raise ConnectionError("boom")

        previous = {"generated_at": "yesterday", "sections": {"tech": {"label": "科技", "items": []}}}
        env = {"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_FORCE_RUN": "1"}
        with patch.dict("os.environ", env, clear=True):
            payload, status = maybe_generate_daily_sections_brief(
                FailingSession(), NOW, self.stories, None, previous
            )
        self.assertEqual(payload, previous)
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "ConnectionError")

    def test_http_error_captures_status_code_and_body_for_debugging(self):
        class FakeErrorResponse:
            status_code = 401
            text = "Authentication Fails, Your api key: ******abcd is invalid"

            def raise_for_status(self):
                raise requests.HTTPError(response=self)

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeErrorResponse()

        env = {"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_FORCE_RUN": "1"}
        with patch.dict("os.environ", env, clear=True):
            payload, status = maybe_generate_daily_sections_brief(
                FakeSession(), NOW, self.stories, None, None
            )
        self.assertIsNone(payload)
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "HTTPError")
        self.assertEqual(status["http_status"], 401)
        self.assertIn("Authentication Fails", status["error_detail"])


if __name__ == "__main__":
    unittest.main()
