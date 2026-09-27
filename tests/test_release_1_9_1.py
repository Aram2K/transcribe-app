"""Regression tests for the v1.9.1 release review fixes that live in main.py,
ui/settings.py and ui/live_assist.py (pure logic - no display needed)."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestUpdateFlag(unittest.TestCase):
    def test_only_newer_releases_count(self):
        import main
        with patch.object(main, "APP_VERSION", "1.9.1"):
            self.assertTrue(main.AppController._is_newer("v1.9.2"))
            self.assertFalse(main.AppController._is_newer("v1.9.1"))
            self.assertFalse(main.AppController._is_newer("v1.9.0"))

    def test_prompt_for_the_running_version_clears_the_flag(self):
        # Declined the 1.9.1 popup in 1.9.0, then installed 1.9.1 by hand.
        import main
        me = SimpleNamespace(_update_prompt_open=False, is_rec=False,
                             cfg={"pending_update_version": "v1.9.1"},
                             save_config=MagicMock(), track=MagicMock(),
                             _is_newer=main.AppController._is_newer)
        with patch.object(main, "APP_VERSION", "1.9.1"), \
             patch.object(main.QMessageBox, "question") as ask:
            main.AppController._prompt_update(me, "v1.9.1")
        ask.assert_not_called()
        self.assertEqual(me.cfg["pending_update_version"], "")
        me.save_config.assert_called_once()


class TestManagedDictationFallback(unittest.TestCase):
    def test_offline_pro_dictation_uses_the_local_model(self):
        import main
        me = SimpleNamespace(get_auth_token=lambda: None,
                             _fallback_to_local_or_error=MagicMock(return_value=("hello", "en")))
        out = main.AudioRecorder._run_managed(me, b"")
        self.assertEqual(out, ("hello", "en"))
        me._fallback_to_local_or_error.assert_called_once()


class TestOfflineAccountBucket(unittest.TestCase):
    def test_signed_in_but_offline_keeps_the_users_keys(self):
        import main
        me = SimpleNamespace(auth=SimpleNamespace(is_authenticated=True, user_id=None),
                             cfg={"secrets_owner": "user:X"}, save_config=MagicMock())
        me._user_secret_id = lambda: "guest"
        with patch.object(main.entitlements, "reconcile_user_secrets") as reconcile:
            self.assertFalse(main.AppController._reconcile_user_secrets(me))
        reconcile.assert_not_called()
        self.assertEqual(me.cfg["secrets_owner"], "user:X")


class TestSettingsWriteBack(unittest.TestCase):
    def test_background_keys_never_ride_back_from_the_snapshot(self):
        from ui.settings import Settings
        me = SimpleNamespace(_BACKGROUND_KEYS=Settings._BACKGROUND_KEYS, cfg_working={
            "whisper_model": "large-v3", "secrets_owner": "user:X", "user_secrets": {},
            "last_known_pro": {"X": False}, "pending_update_version": "v1.9.2",
            "live_assist_auto_answer": False, "known_emails": ["a@b.c"]})
        self.assertEqual(Settings._staged_settings(me), {"whisper_model": "large-v3"})

    def test_auto_answer_uses_a_new_key(self):
        # 1.9.0 wrote "live_assist_auto": false into every config.json.
        import main
        self.assertIn("live_assist_auto_answer", main.DEFAULT)
        self.assertNotIn("live_assist_auto", main.DEFAULT)
        self.assertTrue(main.DEFAULT["live_assist_auto_answer"])


class TestAutoAnswerNeverPreemptsTheUser(unittest.TestCase):
    def _overlay(self, **state):
        base = dict(app=SimpleNamespace(cfg={}), _suggesting=False, _suggest_auto=False,
                    _hold_until=0.0, _pending_auto=False)
        base.update(state)
        return SimpleNamespace(**base)

    def test_a_heard_question_waits_while_the_users_answer_streams(self):
        from ui import live_assist
        me = self._overlay(_suggesting=True, _suggest_auto=False)
        live_assist.LiveAssistOverlay.suggest(me, "", auto=True)
        self.assertTrue(me._pending_auto)          # queued, the user's answer keeps going

    def test_a_heard_question_waits_while_the_user_reads_their_answer(self):
        from ui import live_assist
        me = self._overlay(_hold_until=time.time() + 5)
        live_assist.LiveAssistOverlay.suggest(me, "", auto=True)
        self.assertTrue(me._pending_auto)

    def test_solve_screen_in_privacy_mode_is_not_sent(self):
        from ui import live_assist
        box = MagicMock()
        me = self._overlay(app=SimpleNamespace(cfg={"privacy_mode": True}), _live_text="hi",
                           btn_screen=SimpleNamespace(isChecked=lambda: True),
                           txt_suggestion=box, _gen=3)
        live_assist.LiveAssistOverlay.suggest(me, live_assist.SOLVE_SCREEN, force_screen=True)
        self.assertEqual(me._gen, 3)                # nothing was started
        self.assertIn("Privacy Mode", box.setPlainText.call_args[0][0])


class TestPartialMeetingTranscript(unittest.TestCase):
    def test_a_failed_chunk_keeps_everything_that_did_transcribe(self):
        import threading
        import main
        me = SimpleNamespace(_chunk_threads=[], _abort=False, _record_error="",
                             _chunk_lock=threading.Lock(), _chunk_errors=["!managed:HTTP 503"],
                             _chunk_frames=[], _chunk_idx=3,
                             _chunk_results={0: "Intro and agenda.", 2: "Bob ships Friday."},
                             on_finalising=None, on_lang_detected=None, partial_text="")
        # No tail audio (other test modules replace numpy with a stub).
        with patch.object(main.np, "frombuffer", return_value=SimpleNamespace(copy=lambda: [])):
            text, err = main.AudioRecorder.transcribe(me)
        self.assertEqual((text, err), ("", "!managed:HTTP 503"))     # still reported
        self.assertEqual(me.partial_text, "Intro and agenda. Bob ships Friday.")


class TestAuthResilience(unittest.TestCase):
    def _resp(self, status, body=None, text=""):
        r = MagicMock(status_code=status, text=text)
        if isinstance(body, Exception):
            r.json.side_effect = body
        else:
            r.json.return_value = body
        return r

    def test_captive_portal_200_keeps_the_session(self):
        import auth
        m = auth.AuthManager()
        m._refresh_token = "rt"
        with patch.object(auth.requests, "post", return_value=self._resp(200, ValueError("html"))),              patch.object(auth.storage, "write_secret") as write:
            self.assertFalse(m._refresh_access_token())
        self.assertTrue(m.is_authenticated)
        write.assert_not_called()

    def test_entitlement_answer_after_sign_out_is_dropped(self):
        import auth
        m = auth.AuthManager()
        m._refresh_token, m.user_id = "rt", "X"
        m._access_token, m._access_expires_at = "at", time.time() + 600

        def late_answer(*a, **k):
            m._clear_local()                      # signed out while in flight
            return self._resp(200, [{"is_pro": True}])
        with patch.object(auth.requests, "post", side_effect=late_answer),              patch.object(auth.storage, "write_secret"):
            self.assertFalse(m.refresh_entitlement())
        self.assertFalse(m.is_pro)
        self.assertFalse(m.entitlement_known)


if __name__ == "__main__":
    unittest.main()
