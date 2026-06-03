import os
import tempfile
import unittest
from pathlib import Path

os.environ["TRANSCRIBE_DISABLE_KEYRING"] = "1"
os.environ["TRANSCRIBE_SKIP_MIGRATION"] = "1"

import entitlements


class FakeAuth:
    def __init__(self, authed=False, pro=False):
        self.is_authenticated = authed
        self.is_pro = pro


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


if __name__ == "__main__":
    unittest.main()
