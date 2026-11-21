import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from voice_input import inject, selection
from voice_input.app import VoiceInputApp
from voice_input.hotkey import VoiceSession
from voice_input.polish import IncompleteCompletionError, Polisher


class InputSafetyTests(unittest.TestCase):
    def test_target_checks_app_field_value_and_selection(self):
        target = selection.InputTarget(42, "App", "field", (2, 1), "abc")
        with patch.object(selection, "frontmost_pid", return_value=42), \
             patch.object(selection, "focused_element", return_value=("field", "test")), \
             patch.object(selection, "_attr", return_value=("abc", 0)), \
             patch.object(selection, "_selected_range", return_value=(2, 1)):
            self.assertTrue(selection.input_target_matches(target))
            with patch.object(selection, "frontmost_pid", return_value=43):
                self.assertFalse(selection.input_target_matches(target))
            with patch.object(selection, "focused_element", return_value=("other", "test")):
                self.assertFalse(selection.input_target_matches(target))
            with patch.object(selection, "_selected_range", return_value=(1, 1)):
                self.assertFalse(selection.input_target_matches(target))
            with patch.object(selection, "_attr", return_value=("changed", 0)):
                self.assertFalse(selection.input_target_matches(target))
        target.value = None
        with patch.object(selection, "frontmost_pid", return_value=42), \
             patch.object(selection, "focused_element", return_value=("field", "test")), \
             patch.object(selection, "_attr", return_value=(None, 0)), \
             patch.object(selection, "_selected_range", return_value=(2, 1)):
            self.assertTrue(selection.input_target_matches(target))
            with patch.object(selection, "_selected_range", return_value=(1, 0)):
                self.assertFalse(selection.input_target_matches(target))

    def test_missing_accessibility_attributes_still_checks_focus(self):
        target = selection.InputTarget(42, "App", "field", None, None)
        with patch.object(selection, "frontmost_pid", return_value=42), \
             patch.object(selection, "focused_element", return_value=("field", "test")), \
             patch.object(selection, "_attr", return_value=(None, 0)), \
             patch.object(selection, "_selected_range", return_value=None):
            self.assertTrue(selection.input_target_matches(target))
            self.assertTrue(selection.input_target_matches(target, inserted="abc"))
            with patch.object(selection, "focused_element", return_value=("other", "test")):
                self.assertFalse(selection.input_target_matches(target, inserted="abc"))

    def test_unavailable_focus_uses_stable_window(self):
        with patch.object(selection, "frontmost_pid", return_value=42), \
             patch.object(selection, "frontmost_app_name", return_value="Browser"), \
             patch.object(selection, "focused_element", return_value=(None, "unavailable")), \
             patch.object(selection, "_focused_window", return_value=("window", 0)):
            target = selection.capture_input_target()
            self.assertTrue(selection.input_target_matches(target))
            with patch.object(selection, "_focused_window", return_value=("other", 0)):
                self.assertFalse(selection.input_target_matches(target))
            with patch.object(selection, "frontmost_pid", return_value=43):
                self.assertFalse(selection.input_target_matches(target))
            with patch.object(selection, "_focused_window", return_value=(None, -1)):
                self.assertIsNone(selection.capture_input_target())
                self.assertFalse(selection.input_target_matches(target))

    def test_utf16_selected_text(self):
        target = selection.InputTarget(1, "App", "field", (1, 2), "a😀b")
        self.assertEqual(selection.target_selected_text(target), "😀")
        target.selection_range = (1, 1)
        self.assertIsNone(selection.target_selected_text(target))

    def test_stop_injection_and_return_remainder(self):
        posted = []
        with patch.object(inject, "_post_text", side_effect=posted.append), \
             patch.object(inject.time, "sleep"):
            rest = inject.type_text("a" * 20 + "b" * 5, can_type=lambda sent: not sent)
        self.assertEqual(posted, ["a" * 20])
        self.assertEqual(rest, "b" * 5)

    def test_cancelling_during_injection_stops_before_the_next_chunk(self):
        cancelled = Event()

        def post(_text):
            cancelled.set()

        with patch.object(inject, "_post_text", side_effect=post) as posted:
            rest = inject.type_text("a" * 20 + "tail", initial_delay_ms=0, cancel_event=cancelled)
        posted.assert_called_once_with("a" * 20)
        self.assertEqual(rest, "tail")

    def test_recheck_cursor_and_text_after_partial_insertion(self):
        target = selection.InputTarget(42, "App", "field", (1, 1), "abc")
        with patch.object(selection, "frontmost_pid", return_value=42), \
             patch.object(selection, "focused_element", return_value=("field", "test")), \
             patch.object(selection, "_attr", return_value=("a😀c", 0)), \
             patch.object(selection, "_selected_range", return_value=(3, 0)):
            self.assertTrue(selection.input_target_matches(target, inserted="😀"))
            with patch.object(selection, "_selected_range", return_value=(0, 0)):
                self.assertFalse(selection.input_target_matches(target, inserted="😀"))
            with patch.object(selection, "_attr", return_value=("a😀c!", 0)):
                self.assertFalse(selection.input_target_matches(target, inserted="😀"))
            with patch.object(selection, "focused_element", return_value=("new field", "test")):
                self.assertFalse(selection.input_target_matches(target, inserted="😀"))

    def test_unicode_event_length_in_utf16(self):
        with patch.object(inject.Quartz, "CGEventSourceCreate"), \
             patch.object(inject.Quartz, "CGEventCreateKeyboardEvent", return_value=object()), \
             patch.object(inject.Quartz, "CGEventKeyboardSetUnicodeString") as setter, \
             patch.object(inject.Quartz, "CGEventPost"):
            inject._post_text("a😀")
        self.assertEqual(setter.call_args.args[1:], (3, "a😀"))

    def test_ask_cap_and_truncation(self):
        p = Polisher(api_key="test")
        p._complete = Mock(return_value="EDIT\n完整改写")
        self.assertEqual(p.ask("改得专业", "原文" * 500)[0], "edit")
        self.assertGreater(p._complete.call_args.kwargs["max_tokens"], 1000)
        p._complete.side_effect = IncompleteCompletionError()
        action, content = p.ask("改写", "原文")
        self.assertEqual(action, "answer")
        self.assertIn("截断", content)

    def test_large_ask_selection_is_rejected_before_request(self):
        p = Polisher(api_key="test", ask_max_tokens=256)
        p._complete = Mock()
        action, content = p.ask("改写", "长文" * 200)
        self.assertEqual(action, "answer")
        self.assertIn("分段", content)
        p._complete.assert_not_called()

    def test_openai_length_finish_reason_rejected(self):
        p = Polisher(api_key="test")
        response = Mock()
        response.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": "EDIT\n部分"}}]}
        with patch("httpx.Client") as client:
            client.return_value.__enter__.return_value.post.return_value = response
            with self.assertRaises(IncompleteCompletionError):
                p._complete("system", "user")

    def test_ollama_length_finish_reason_rejected(self):
        p = Polisher(api_key="test")
        p._provider = "ollama"
        p._client = SimpleNamespace(chat=Mock(return_value=SimpleNamespace(
            done_reason="length", message=SimpleNamespace(content="部分"))))
        with self.assertRaises(IncompleteCompletionError):
            p._complete("system", "user")

    def test_missing_finish_reason_never_replaces_selection(self):
        p = Polisher(api_key="test")
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "EDIT\n部分"}}]}
        with patch("httpx.Client") as client:
            client.return_value.__enter__.return_value.post.return_value = response
            self.assertEqual(p.ask("改写", "原文")[0], "answer")

    def test_ask_does_not_replace_if_original_selection_disagrees(self):
        app = VoiceInputApp.__new__(VoiceInputApp)
        app._asr = SimpleNamespace(transcribe=Mock(return_value="润色"))
        app._polisher = SimpleNamespace(ask=Mock(return_value=("edit", "改写后")))
        app._commands_enabled = False
        app._markdown_apps = set()
        app._stage_label = Mock()
        app._retain_result = Mock()
        app._safe_type = Mock()
        target = selection.InputTarget(1, "App", "field", (0, 2), "原文")
        app._process([0], "ask", "不同选区", {"target": target})
        app._safe_type.assert_not_called()
        app._retain_result.assert_called_once()

    def test_short_acknowledgements_and_list_retry(self):
        p = Polisher(api_key="test")
        for word in ("嗯", "哦", "噢", "啊"):
            self.assertEqual(p.polish(word), word)
        source = "第一点准备报告第二点检查成本第三点提交审核"
        p._complete = Mock(side_effect=["准备报告，检查成本，提交审核。", "1. 准备报告\n2. 检查成本\n3. 提交审核"])
        self.assertIn("\n2.", p.polish(source))
        self.assertEqual(p._complete.call_count, 2)


class TranslationConfigTests(unittest.TestCase):
    def test_next_translation_uses_saved_language_in_same_app(self):
        with TemporaryDirectory() as td:
            config = Path(td) / "config.yaml"
            config.write_text("llm: {api_key: test}\nautolearn: {enabled: false}\n"
                              "translation: {target_language: English}\n", encoding="utf-8")
            app = VoiceInputApp(config)
            app._asr = SimpleNamespace(transcribe=Mock(return_value="你好"))
            app._polisher._complete = Mock(return_value="Hello")
            app._safe_type = Mock(return_value=True)

            app._process([0], "translate")
            self.assertIn("英语（English）", app._polisher._complete.call_args.args[0])

            config.write_text("translation: {target_language: Serbian}\n", encoding="utf-8")
            app._process([0], "translate")
            self.assertIn("翻译成Serbian", app._polisher._complete.call_args.args[0])

            config.write_text("translation: [", encoding="utf-8")
            with self.assertLogs("voice_input.app", level="WARNING"):
                app._process([0], "translate")
            self.assertIn("翻译成Serbian", app._polisher._complete.call_args.args[0])


class AskWebSearchTests(unittest.TestCase):
    def test_native_deepseek_search_request_and_cited_answer(self):
        p = Polisher(api_key="test", disable_thinking=False)
        p.set_terms(["私人术语"])
        p.set_style_profile("私人偏好")
        p.set_usage_logger(Mock())
        valid_source = {"type": "url_citation", "url_citation": {
            "url": "https://example.com/release", "title": "Release notes"}}
        response = Mock()
        response.json.return_value = {
            "choices": [{"finish_reason": "stop", "message": {
                "content": "ANSWER\n已发布新版本。",
                "annotations": [valid_source, valid_source, {"type": "url_citation", "url_citation": {
                    "url": "file:///private/secret", "title": "Unsafe link"}}],
            }}],
            "usage": {"prompt_tokens": 2300, "completion_tokens": 80},
        }
        with patch("httpx.Client") as client:
            post = client.return_value.__enter__.return_value.post
            post.return_value = response
            action, answer = p.ask("最新版本是什么", app="私人应用", window_title="私人窗口", web_search=True)
            payload = post.call_args.kwargs["json"]
            self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/chat/completions")
            self.assertEqual(payload["tools"], [{"type": "deepseek_web_search"}])
            self.assertEqual(payload["thinking"], {"type": "disabled"})
            for private in ("私人术语", "私人偏好", "私人应用", "私人窗口"):
                self.assertNotIn(private, payload["messages"][0]["content"])
        self.assertEqual(action, "answer")
        self.assertIn("已发布新版本。\n\n来源\n1. Release notes", answer)
        self.assertEqual(answer.count("https://example.com/release"), 1)
        self.assertNotIn("file://", answer)
        p._usage.log.assert_called_once_with("ask", "deepseek-flash", 2300, 80)

    def test_search_only_for_opted_in_deepseek_ask_without_selection(self):
        for base_url, web_search, selection_text in (
            ("https://api.deepseek.com", False, None),
            ("https://api.deepseek.com", True, "待改写选区"),
            ("https://api.deepseek.com.example.org", True, None),
        ):
            with self.subTest(base_url=base_url, enabled=web_search, has_selection=bool(selection_text)):
                p = Polisher(api_key="test", base_url=base_url)
                response = Mock()
                marker = "EDIT" if selection_text else "ANSWER"
                response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {
                    "content": marker + "\n完成",
                }}]}
                with patch("httpx.Client") as client:
                    post = client.return_value.__enter__.return_value.post
                    post.return_value = response
                    action, content = p.ask("处理", selection_text, web_search=web_search)
                    self.assertNotIn("tools", post.call_args.kwargs["json"])
                self.assertEqual((action, content), (marker.lower(), "完成"))

    def test_app_rereads_search_toggle_before_next_question(self):
        with TemporaryDirectory() as td:
            config = Path(td) / "config.yaml"
            base_config = "llm: {api_key: test}\nautolearn: {enabled: false}\n"
            config.write_text(base_config, encoding="utf-8")
            app = VoiceInputApp(config)
            self.addCleanup(app._memory._conn.close)
            app._asr = SimpleNamespace(transcribe=Mock(return_value="测试问题"))
            app._polisher.ask = Mock(return_value=("answer", "测试答案"))
            app._show_card = Mock()

            for value, enabled in (("", False), ("true", True), ("false", False), ('"true"', False)):
                with self.subTest(value=value):
                    suffix = f"ask_web_search: {value}\n" if value else ""
                    config.write_text(base_config + suffix, encoding="utf-8")
                    app._process([0], "ask")
                    self.assertIs(app._polisher.ask.call_args.kwargs["web_search"], enabled)
            app._show_card.assert_called_with("测试答案", title="Ask", session=None)


class VoiceCancellationTests(unittest.TestCase):
    def test_cancellation_during_asr_prevents_model_request(self):
        session = VoiceSession()
        app = VoiceInputApp.__new__(VoiceInputApp)
        app._markdown_apps = set()
        app._stage_label = Mock()
        app._polisher = Mock()

        def transcribe(_audio):
            session.cancelled.set()
            return "测试语音"

        app._asr = SimpleNamespace(transcribe=transcribe)
        app._process([0], "translate", cursor_ctx={"session": session})
        app._polisher.translate.assert_not_called()

    def test_cancelled_llm_result_is_not_typed_or_saved(self):
        session = VoiceSession()
        app = VoiceInputApp.__new__(VoiceInputApp)
        app._markdown_apps = set()
        app._stage_label = Mock()
        app._memory = Mock()
        app._statusbar = None
        app._hud = None
        app._asr = SimpleNamespace(transcribe=Mock(return_value="测试语音"))
        app._refresh_translation_target = Mock()

        def translate(*args, **kwargs):
            session.cancelled.set()
            return "Test"

        app._polisher = SimpleNamespace(translate=translate)
        with patch("voice_input.app.type_text") as type_text:
            app._process([0], "translate", cursor_ctx={"session": session})
        type_text.assert_not_called()
        app._memory.log_history.assert_not_called()


if __name__ == "__main__":
    unittest.main()
