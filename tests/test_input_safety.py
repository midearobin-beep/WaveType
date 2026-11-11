import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from voice_input import inject, selection
from voice_input.app import VoiceInputApp
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


if __name__ == "__main__":
    unittest.main()
