import unittest
from unittest.mock import Mock, patch

import numpy as np

from voice_input import hotkey
from voice_input.app import VoiceInputApp

FN, SHIFT, CTRL, ALT = 0x800000, 0x20000, 0x40000, 0x80000
RIGHT_ALT_BIT = 0x40
BASE = 0x100


class FnHotkeyTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.hk = hotkey.FnHotkey(
            on_press=lambda m, session: self.calls.append(("press", m)),
            on_release=lambda m, session: self.calls.append(("release", m)),
            mode="toggle",
            on_correct=lambda: self.calls.append(("correct",)),
            on_cancel=lambda session, recording: self.calls.append(("cancel",)),
            alt_key="right_option",
        )
        # 回调同步执行，免去工作线程时序
        self.hk._safe_call = lambda fn, *a: fn(*a)
        self._patch = patch.object(hotkey.Quartz, "CGEventGetFlags", side_effect=lambda e: e)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def flags(self, *seq):
        for f in seq:
            self.hk._on_flags(f)

    def escape(self, *, down=True, flags=BASE, repeating=False):
        quartz = hotkey.Quartz
        event = quartz.CGEventCreateKeyboardEvent(None, 0x35, down)
        quartz.CGEventSetIntegerValueField(event, quartz.kCGKeyboardEventAutorepeat, int(repeating))
        event_type = quartz.kCGEventKeyDown if down else quartz.kCGEventKeyUp
        with patch.object(quartz, "CGEventGetFlags", return_value=flags):
            return self.hk._on_key(event_type, event)

    def test_fn_shift_same_event_starts_translate_without_finishing(self):
        # 11-09 10:49:53 实际序列：Fn 与 Shift 按下沿同一事件 → 松 Shift → 松 Fn
        self.flags(BASE | FN | SHIFT | 0x2, BASE | FN, BASE)
        self.assertEqual(self.calls, [("press", "translate")])
        # 再点一下 Fn 收尾
        self.flags(BASE | FN, BASE)
        self.assertEqual(self.calls[-1], ("release", "translate"))

    def test_fn_first_then_shift_upgrades_to_translate(self):
        self.flags(BASE | FN, BASE | FN | SHIFT)
        self.assertEqual(self.calls,
                         [("press", "dictate"), ("cancel",), ("press", "translate")])

    def test_shift_first_then_fn(self):
        self.flags(BASE | SHIFT, BASE | SHIFT | FN)
        self.assertEqual(self.calls, [("press", "translate")])

    def test_toggle_dictate(self):
        self.flags(BASE | FN, BASE, BASE | FN, BASE)
        self.assertEqual(self.calls, [("press", "dictate"), ("release", "dictate")])

    def test_ctrl_fn_correct(self):
        self.flags(BASE | CTRL, BASE | CTRL | FN)
        self.assertEqual(self.calls, [("correct",)])

    def test_right_option_toggle(self):
        self.flags(BASE | ALT | RIGHT_ALT_BIT, BASE, BASE | ALT | RIGHT_ALT_BIT, BASE)
        self.assertEqual(self.calls, [("press", "dictate"), ("release", "dictate")])

    def test_right_option_with_cmd_ignored(self):
        self.flags(BASE | 0x100000 | ALT | RIGHT_ALT_BIT, BASE)
        self.assertEqual(self.calls, [])

    def test_escape_cancels_hold_without_finishing_on_fn_release(self):
        self.hk._mode = "hold"
        self.flags(BASE | FN)
        self.assertIsNone(self.escape(flags=BASE | FN))
        self.flags(BASE | FN | SHIFT, BASE | FN, BASE)
        self.assertIsNone(self.escape(down=False))
        self.assertTrue(self.hk._session.cancelled.is_set())
        self.assertEqual(self.calls, [("press", "dictate"), ("cancel",)])
        self.flags(BASE | FN, BASE)
        self.assertEqual(self.calls[-2:], [("press", "dictate"), ("release", "dictate")])

    def test_escape_cancels_pending_task_and_holding_it_does_not_cancel_new_recording(self):
        self.hk._start("translate")
        self.hk._finish()
        session = self.hk._session
        self.assertIsNone(self.escape())
        self.hk._start("ask")
        self.assertIsNone(self.escape(repeating=True))
        self.assertIsNone(self.escape(down=False))
        self.assertTrue(session.cancelled.is_set())
        self.assertFalse(self.hk._session.cancelled.is_set())
        self.assertEqual(self.calls, [("press", "translate"), ("release", "translate"),
                                      ("cancel",), ("press", "ask")])

    def test_idle_modified_and_already_finished_escape_pass_through(self):
        self.assertIsNotNone(self.escape())
        self.assertIsNotNone(self.escape(down=False))
        self.hk._start("ask")
        self.assertIsNotNone(self.escape(flags=BASE | CTRL))
        self.hk._finish()
        self.hk._session.finished = True
        self.assertIsNotNone(self.escape())
        self.assertIsNotNone(self.escape(down=False))
        self.assertEqual(self.calls, [("press", "ask"), ("release", "ask")])


class EmptyRecordingTests(unittest.TestCase):
    def test_empty_audio_hides_hud_and_does_not_queue(self):
        app = VoiceInputApp.__new__(VoiceInputApp)
        app._recorder = Mock()
        app._recorder.stop.return_value = np.zeros(0, dtype=np.float32)
        app._recorder.max_speech_prob = 0.0
        app._ask_context = None
        app._cursor_ctx = {"before": "", "after": ""}
        app._input_target = None
        app._statusbar = None
        app._hud = None
        app._queue = Mock()
        app._hide_hud = Mock()
        app._on_release("translate", hotkey.VoiceSession())
        app._hide_hud.assert_called_once()
        app._queue.put.assert_not_called()

    def test_cancelled_release_aborts_microphone_and_skips_processing(self):
        app = VoiceInputApp.__new__(VoiceInputApp)
        app._recorder = Mock()
        app._ask_context = None
        app._cursor_ctx = {"before": "", "after": ""}
        app._input_target = None
        app._statusbar = None
        app._queue = Mock()
        app._hide_hud = Mock()
        session = hotkey.VoiceSession()
        session.cancelled.set()
        app._on_release("translate", session)
        app._recorder.cancel.assert_called_once()
        app._recorder.stop.assert_not_called()
        app._queue.put.assert_not_called()
        self.assertTrue(session.finished)


if __name__ == "__main__":
    unittest.main()
