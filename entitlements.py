"""
Tier + entitlement logic for Transcribe.

Three tiers:
  * guest - not signed in. A 10-minute total recording trial (all models), so a
            new user can experience the product before creating an account
            (lower friction → higher activation; see onboarding research).
  * free  - signed in, no active subscription. Unlimited local dictation; Pro
            features (meetings, smart actions, managed cloud) are locked behind
            contextual upgrade prompts.
  * pro   - signed in with an active subscription. Everything unlocked.

Guest usage is metered locally (no account) in usage.json. Pro entitlement is
always confirmed server-side by auth.AuthManager - this module only maps that
state to tiers + feature gates. It never decides Pro on its own.
"""

import logging

import storage

logger = logging.getLogger("transcribe.entitlements")

GUEST_FREE_SECONDS = 600  # 10 minutes of recording for guests
FREE_SMART_ACTION_TRIES = 5  # free/guest users get 5 Smart Actions to try, then it locks

TIER_GUEST = "guest"
TIER_FREE = "free"
TIER_PRO = "pro"

# Admin status is now server-authoritative (profiles.is_admin, set only via SQL)
# and delivered through the my_entitlement RPC as auth.is_admin. There is NO admin
# email in the client source, so editing the open-source app can't make anyone an
# admin. The local "force tier" control is purely a preview toggle for admins and
# never grants real server Pro (the cloud proxy still verifies entitlement).

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
    # Server-authoritative: comes from profiles.is_admin via the entitlement RPC.
    return bool(auth is not None and getattr(auth, "is_admin", False))


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


def has_pro_access(auth, cfg=None):
    """Pro access used for FEATURE GATING. A genuine Pro/trial entitlement (from
    the server) is NEVER downgraded - so a real Pro user can never be blocked or
    upsold. An admin can also preview Pro by forcing it. The admin force-tier's
    guest/free options only change the displayed badge, not real Pro access; they
    still gate a non-Pro admin (useful for testing on a free account)."""
    if auth is not None and getattr(auth, "is_pro", False):
        return True
    return _override_tier(auth, cfg) == TIER_PRO


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
    """The guest recording cap applies ONLY to users who are not signed in.
    Anyone signed in (free, pro, or an admin previewing the guest tier via the
    force-tier control) always has unlimited local dictation - we gate on real
    authentication, never on the displayed/forced tier."""
    if auth is not None and getattr(auth, "is_authenticated", False):
        return True
    return guest_seconds_remaining() > 0


def feature_allowed(auth, feature, cfg=None):
    """Pro-only features require real Pro access; everything else is allowed."""
    if feature in PRO_FEATURES:
        return has_pro_access(auth, cfg)
    return True


# ── Smart Actions free-trial counter (5 tries for non-Pro) ────────────────────
def smart_actions_used():
    try:
        return int(_load_usage().get("smart_action_uses", 0))
    except (TypeError, ValueError):
        return 0


def smart_actions_remaining(auth=None, cfg=None):
    if has_pro_access(auth, cfg):
        return FREE_SMART_ACTION_TRIES  # display value; Pro is unlimited anyway
    return max(0, FREE_SMART_ACTION_TRIES - smart_actions_used())


def can_use_smart_action(auth, cfg=None):
    """Pro = unlimited; everyone else gets FREE_SMART_ACTION_TRIES total."""
    if has_pro_access(auth, cfg):
        return True
    return smart_actions_used() < FREE_SMART_ACTION_TRIES


def add_smart_action_use():
    data = _load_usage()
    used = smart_actions_used() + 1
    data["smart_action_uses"] = used
    try:
        storage.atomic_write_json(_USAGE_PATH, data)
    except Exception:
        logger.debug("Could not persist smart-action usage", exc_info=True)
    return used
