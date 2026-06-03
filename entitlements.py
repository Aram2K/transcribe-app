"""
Tier + entitlement logic for Transcribe.

Three tiers:
  * guest — not signed in. A 10-minute total recording trial (all models), so a
            new user can experience the product before creating an account
            (lower friction → higher activation; see onboarding research).
  * free  — signed in, no active subscription. Unlimited local dictation; Pro
            features (meetings, smart actions, managed cloud) are locked behind
            contextual upgrade prompts.
  * pro   — signed in with an active subscription. Everything unlocked.

Guest usage is metered locally (no account) in usage.json. Pro entitlement is
always confirmed server-side by auth.AuthManager — this module only maps that
state to tiers + feature gates. It never decides Pro on its own.
"""

import logging

import storage

logger = logging.getLogger("transcribe.entitlements")

GUEST_FREE_SECONDS = 600  # 10 minutes of recording for guests

TIER_GUEST = "guest"
TIER_FREE = "free"
TIER_PRO = "pro"

# Super admins can force any tier locally (for testing / control) via a config
# override. This only affects client-side gating on their own machine — it never
# grants real server entitlements (the cloud proxy still verifies subscriptions).
SUPER_ADMIN_EMAILS = {"aramatamian15@gmail.com"}

# Pro-only features.
FEATURE_MEETINGS = "meetings"
FEATURE_SMART_ACTIONS = "smart_actions"
FEATURE_CLOUD = "cloud_transcription"
PRO_FEATURES = {FEATURE_MEETINGS, FEATURE_SMART_ACTIONS, FEATURE_CLOUD}

_USAGE_PATH = storage.path_for("usage.json")


def _load_usage():
    data = storage.read_json(_USAGE_PATH, {})
    return data if isinstance(data, dict) else {}


def is_super_admin(auth):
    email = (getattr(auth, "user_email", None) or "") if auth is not None else ""
    return email.strip().lower() in SUPER_ADMIN_EMAILS


def _override_tier(auth, cfg):
    """A super admin's forced tier from config, or None."""
    if cfg and is_super_admin(auth):
        ov = cfg.get("admin_tier_override", "auto")
        if ov in (TIER_GUEST, TIER_FREE, TIER_PRO):
            return ov
    return None


def tier(auth, cfg=None):
    """Map the auth state to a tier. `auth` is an auth.AuthManager (or None).
    A super admin may force a tier via cfg['admin_tier_override']."""
    ov = _override_tier(auth, cfg)
    if ov:
        return ov
    if auth is not None and getattr(auth, "is_pro", False):
        return TIER_PRO
    if auth is not None and getattr(auth, "is_authenticated", False):
        return TIER_FREE
    return TIER_GUEST


def is_pro(auth, cfg=None):
    return tier(auth, cfg) == TIER_PRO


def guest_seconds_used():
    try:
        return max(0.0, float(_load_usage().get("guest_seconds_used", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def guest_seconds_remaining():
    return max(0.0, GUEST_FREE_SECONDS - guest_seconds_used())


def guest_minutes_remaining():
    """Whole minutes remaining, rounded up, for display."""
    import math
    return int(math.ceil(guest_seconds_remaining() / 60.0))


def add_guest_seconds(seconds):
    """Accumulate guest recording time. Returns the new total used."""
    if not seconds or seconds <= 0:
        return guest_seconds_used()
    data = _load_usage()
    used = guest_seconds_used() + float(seconds)
    data["guest_seconds_used"] = used
    try:
        storage.atomic_write_json(_USAGE_PATH, data)
    except Exception:
        logger.debug("Could not persist guest usage", exc_info=True)
    return used


def can_record(auth, cfg=None):
    """Guests are capped at GUEST_FREE_SECONDS of total recording; free and pro
    are unlimited for local dictation."""
    if tier(auth, cfg) == TIER_GUEST:
        return guest_seconds_remaining() > 0
    return True


def feature_allowed(auth, feature, cfg=None):
    """Pro-only features require the pro tier; everything else is allowed."""
    if feature in PRO_FEATURES:
        return tier(auth, cfg) == TIER_PRO
    return True
