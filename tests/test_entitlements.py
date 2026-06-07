import os
import tempfile
import unittest
from pathlib import Path

os.environ["TRANSCRIBE_DISABLE_KEYRING"] = "1"
os.environ["TRANSCRIBE_SKIP_MIGRATION"] = "1"

import entitlements


class FakeAuth:
    def __init__(self, authed=False, pro=False, admin=False, uid=None):
        self.is_authenticated = authed
        self.is_pro = pro
        self.is_admin = admin
        self.user_id = uid


class TestEntitlements(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = entitlements._USAGE_PATH
        entitlements._USAGE_PATH = Path(self._tmp.name) / "usage.json"

    def tearDown(self):
        entitlements._USAGE_PATH = self._orig
        self._tmp.cleanup()

    def test_tier_mapping(self):
        self.assertEqual(entitlements.tier(None), entitlements.TIER_GUEST)
        self.assertEqual(entitlements.tier(FakeAuth(True, False)), entitlements.TIER_FREE)
        self.assertEqual(entitlements.tier(FakeAuth(True, True)), entitlements.TIER_PRO)

    def test_guest_meter_counts_down_and_blocks(self):
        self.assertTrue(entitlements.can_record(None))
        self.assertEqual(entitlements.guest_seconds_remaining(), 600)

        entitlements.add_guest_seconds(300)
        self.assertAlmostEqual(entitlements.guest_seconds_remaining(), 300, delta=1)
        self.assertTrue(entitlements.can_record(None))

        entitlements.add_guest_seconds(300)
        self.assertFalse(entitlements.can_record(None))   # 10 min exhausted
        self.assertEqual(entitlements.guest_seconds_remaining(), 0)

    def test_free_and_pro_are_unlimited(self):
        entitlements.add_guest_seconds(10_000)  # blow past the guest cap
        self.assertTrue(entitlements.can_record(FakeAuth(True, False)))
        self.assertTrue(entitlements.can_record(FakeAuth(True, True)))

    def test_pro_features_require_pro_tier(self):
        for feat in (entitlements.FEATURE_MEETINGS,
                     entitlements.FEATURE_SMART_ACTIONS,
                     entitlements.FEATURE_CLOUD):
            self.assertFalse(entitlements.feature_allowed(None, feat))
            self.assertFalse(entitlements.feature_allowed(FakeAuth(True, False), feat))
            self.assertTrue(entitlements.feature_allowed(FakeAuth(True, True), feat))

    def test_minutes_remaining_rounds_up(self):
        entitlements.add_guest_seconds(539)  # 61s left -> 2 minutes (ceil)
        self.assertEqual(entitlements.guest_minutes_remaining(), 2)


class TestPerUserSmartTrial(unittest.TestCase):
    """The free Smart Actions trial must follow the user, not the device."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = entitlements._USAGE_PATH
        entitlements._USAGE_PATH = Path(self._tmp.name) / "usage.json"

    def tearDown(self):
        entitlements._USAGE_PATH = self._orig
        self._tmp.cleanup()

    def test_counter_is_per_user(self):
        a = FakeAuth(True, False, uid="A")
        b = FakeAuth(True, False, uid="B")
        for _ in range(entitlements.FREE_SMART_ACTION_TRIES):
            entitlements.add_smart_action_use(a)
        # A is exhausted...
        self.assertEqual(entitlements.smart_actions_remaining(a, {}), 0)
        self.assertFalse(entitlements.can_use_smart_action(a, {}))
        # ...but B (a different account on the same device) still has a full 5.
        self.assertEqual(entitlements.smart_actions_remaining(b, {}), entitlements.FREE_SMART_ACTION_TRIES)
        self.assertTrue(entitlements.can_use_smart_action(b, {}))
        # Guests get their own bucket too.
        self.assertEqual(entitlements.smart_actions_remaining(None, {}), entitlements.FREE_SMART_ACTION_TRIES)

    def test_legacy_global_counter_maps_to_guest(self):
        import storage
        storage.atomic_write_json(entitlements._USAGE_PATH, {"smart_action_uses": 3})
        self.assertEqual(entitlements.smart_actions_used(None), 3)  # guest inherits legacy
        self.assertEqual(entitlements.smart_actions_used(FakeAuth(True, False, uid="A")), 0)


class TestUserSecretIsolation(unittest.TestCase):
    """API keys must not leak between accounts on a shared computer."""

    def test_switching_users_hides_then_restores_keys(self):
        cfg = {"google_api_key": "AAA", "action_model": "api_gemini", "backend": "local"}
        # First run as user A adopts the existing key without clearing it.
        self.assertFalse(entitlements.reconcile_user_secrets(cfg, "user:A"))
        self.assertEqual(cfg["secrets_owner"], "user:A")
        self.assertEqual(cfg["google_api_key"], "AAA")
        # User B signs in: A's key must disappear and engine resets to defaults.
        self.assertTrue(entitlements.reconcile_user_secrets(cfg, "user:B"))
        self.assertEqual(cfg["google_api_key"], "")
        self.assertEqual(cfg["action_model"], "rule_based")
        # B saves their own key, then A returns: each sees only their own key.
        cfg["google_api_key"] = "BBB"
        self.assertTrue(entitlements.reconcile_user_secrets(cfg, "user:A"))
        self.assertEqual(cfg["google_api_key"], "AAA")
        self.assertTrue(entitlements.reconcile_user_secrets(cfg, "user:B"))
        self.assertEqual(cfg["google_api_key"], "BBB")

    def test_same_user_is_noop(self):
        cfg = {"google_api_key": "AAA", "secrets_owner": "user:A"}
        self.assertFalse(entitlements.reconcile_user_secrets(cfg, "user:A"))
        self.assertEqual(cfg["google_api_key"], "AAA")

    def test_user_secret_id(self):
        self.assertEqual(entitlements.user_secret_id(None), "guest")
        self.assertEqual(entitlements.user_secret_id(FakeAuth(True, True, uid="Z")), "user:Z")


class TestAdminTierPreview(unittest.TestCase):
    """The super-admin force-tier control must actually change what features and
    gating an admin sees (preview), while NEVER affecting normal users."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = entitlements._USAGE_PATH
        entitlements._USAGE_PATH = Path(self._tmp.name) / "usage.json"

    def tearDown(self):
        entitlements._USAGE_PATH = self._orig
        self._tmp.cleanup()

    def test_admin_force_free_locks_pro_features(self):
        admin_pro = FakeAuth(True, True, admin=True)
        cfg = {"admin_tier_override": "free"}
        # Display tier AND gating both reflect "free".
        self.assertEqual(entitlements.tier(admin_pro, cfg), entitlements.TIER_FREE)
        self.assertFalse(entitlements.has_pro_access(admin_pro, cfg))
        self.assertFalse(entitlements.feature_allowed(admin_pro, entitlements.FEATURE_MEETINGS, cfg))
        # Smart Actions fall back to the 5-free-trial counter, not unlimited.
        self.assertTrue(entitlements.can_use_smart_action(admin_pro, cfg))
        for _ in range(entitlements.FREE_SMART_ACTION_TRIES):
            entitlements.add_smart_action_use()
        self.assertFalse(entitlements.can_use_smart_action(admin_pro, cfg))

    def test_admin_force_guest_applies_recording_cap(self):
        admin_pro = FakeAuth(True, True, admin=True)
        cfg = {"admin_tier_override": "guest"}
        self.assertEqual(entitlements.tier(admin_pro, cfg), entitlements.TIER_GUEST)
        self.assertFalse(entitlements.has_pro_access(admin_pro, cfg))
        # Guest cap is honored even though the admin is signed in.
        self.assertTrue(entitlements.can_record(admin_pro, cfg))
        entitlements.add_guest_seconds(10_000)
        self.assertFalse(entitlements.can_record(admin_pro, cfg))

    def test_admin_force_pro_or_auto_grants_pro(self):
        admin_pro = FakeAuth(True, True, admin=True)
        self.assertTrue(entitlements.has_pro_access(admin_pro, {"admin_tier_override": "pro"}))
        # "auto" = no override -> real entitlement wins.
        self.assertTrue(entitlements.has_pro_access(admin_pro, {"admin_tier_override": "auto"}))
        self.assertTrue(entitlements.has_pro_access(admin_pro, {}))

    def test_override_never_affects_non_admins(self):
        # A normal (non-admin) Pro user keeps Pro even if a "free" override somehow
        # appears in their config - the override only applies to super admins.
        normal_pro = FakeAuth(True, True, admin=False)
        self.assertTrue(entitlements.has_pro_access(normal_pro, {"admin_tier_override": "free"}))
        # And a normal free user is never elevated by a "pro" override.
        normal_free = FakeAuth(True, False, admin=False)
        self.assertFalse(entitlements.has_pro_access(normal_free, {"admin_tier_override": "pro"}))


if __name__ == "__main__":
    unittest.main()
