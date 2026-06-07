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
    """Pro access used for FEATURE GATING.

    When a super admin sets the force-tier preview, that choice is AUTHORITATIVE
    here - forcing guest/free actually locks Pro features and forcing pro unlocks
    them - so an admin can see exactly what each tier looks like. The override
    only ever applies to super admins (see _override_tier), so a normal Pro user
    is never affected: with no override active, their real server entitlement
    always wins and can never be downgraded or upsold."""
    ov = _override_tier(auth, cfg)
    if ov is not None:
        return ov == TIER_PRO
    if auth is not None and getattr(auth, "is_pro", False):
        return True
    return False


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
    """The guest recording cap applies to users who are not signed in. Signed-in
    free/pro users have unlimited local dictation. An admin can preview the guest
    cap by forcing the guest tier (so the 10-minute limit is honored even though
    they're signed in); forcing free/pro keeps dictation unlimited as expected."""
    if _override_tier(auth, cfg) == TIER_GUEST:
        return guest_seconds_remaining() > 0
    if auth is not None and getattr(auth, "is_authenticated", False):
        return True
    return guest_seconds_remaining() > 0


def feature_allowed(auth, feature, cfg=None):
    """Pro-only features require real Pro access; everything else is allowed."""
    if feature in PRO_FEATURES:
        return has_pro_access(auth, cfg)
    return True


# ── Smart Actions free-trial counter (5 tries for non-Pro) ────────────────────
# The trial follows the USER, not the device: signed-in users are keyed by their
# server user id, so signing in as a fresh account on a shared computer always
# starts at 5/5 and switching users never shares a count. Guests share a single
# on-device "guest" bucket.
def _usage_key(auth):
    uid = getattr(auth, "user_id", None) if auth is not None else None
    return f"user:{uid}" if uid else "guest"


def smart_actions_used(auth=None):
    data = _load_usage()
    key = _usage_key(auth)
    by_user = data.get("smart_action_uses_by_user")
    if isinstance(by_user, dict) and key in by_user:
        try:
            return int(by_user.get(key, 0))
        except (TypeError, ValueError):
            return 0
    # Backward-compat: a pre-existing global counter (from before per-user
    # buckets) belongs to whoever is using the app right now if not signed in.
    if key == "guest":
        try:
            return int(data.get("smart_action_uses", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def smart_actions_remaining(auth=None, cfg=None):
    if has_pro_access(auth, cfg):
        return FREE_SMART_ACTION_TRIES  # display value; Pro is unlimited anyway
    return max(0, FREE_SMART_ACTION_TRIES - smart_actions_used(auth))


def can_use_smart_action(auth, cfg=None):
    """Pro = unlimited; everyone else gets FREE_SMART_ACTION_TRIES total (per user)."""
    if has_pro_access(auth, cfg):
        return True
    return smart_actions_used(auth) < FREE_SMART_ACTION_TRIES


# ── Per-user API key / engine isolation ──────────────────────────────────────
# BYO API keys and the cloud-engine config that depends on them are scoped to the
# user who entered them. On a shared computer, signing in as a different account
# never exposes the previous user's keys; the same user gets theirs back.
USER_SCOPED_SECRET_KEYS = (
    "google_api_key", "action_api_key", "mistral_api_key",
    "action_model", "action_api_provider", "action_api_base_url",
    "action_api_model", "backend", "managed_provider",
)
USER_SCOPED_SECRET_DEFAULTS = {
    "google_api_key": "", "action_api_key": "", "mistral_api_key": "",
    "action_model": "rule_based", "action_api_provider": "openai_compatible",
    "action_api_base_url": "", "action_api_model": "",
    "backend": "local", "managed_provider": "gemini",
}
_SECRET_VALUE_KEYS = ("google_api_key", "action_api_key", "mistral_api_key")


def user_secret_id(auth):
    """Stable per-user key for scoping secrets (guests share one bucket)."""
    uid = getattr(auth, "user_id", None) if auth is not None else None
    return f"user:{uid}" if uid else "guest"


def reconcile_user_secrets(cfg, current_owner):
    """Swap the per-user API keys + engine config in `cfg` so they belong to
    `current_owner`. Mutates `cfg` in place. Returns True only when the active
    keys were actually swapped for a *different* user (so callers can refresh UI).

    - same owner: no-op.
    - first run (no recorded owner): adopt whatever keys are already in cfg for
      the current user, without clearing.
    - different owner: stash the outgoing user's keys, load the incoming user's
      (or clean defaults for a brand-new account on this device).
    """
    prev = cfg.get("secrets_owner")
    if prev == current_owner:
        return False
    store = cfg.get("user_secrets")
    if not isinstance(store, dict):
        store = {}
    if prev is None:
        snapshot = {k: cfg.get(k, USER_SCOPED_SECRET_DEFAULTS[k]) for k in USER_SCOPED_SECRET_KEYS}
        if any(snapshot.get(k) for k in _SECRET_VALUE_KEYS):
            store[current_owner] = snapshot
        cfg["user_secrets"] = store
        cfg["secrets_owner"] = current_owner
        return False
    store[prev] = {k: cfg.get(k, USER_SCOPED_SECRET_DEFAULTS[k]) for k in USER_SCOPED_SECRET_KEYS}
    incoming = store.get(current_owner) or dict(USER_SCOPED_SECRET_DEFAULTS)
    for k in USER_SCOPED_SECRET_KEYS:
        cfg[k] = incoming.get(k, USER_SCOPED_SECRET_DEFAULTS[k])
    cfg["user_secrets"] = store
    cfg["secrets_owner"] = current_owner
    return True


def add_smart_action_use(auth=None):
    data = _load_usage()
    key = _usage_key(auth)
    by_user = data.get("smart_action_uses_by_user")
    if not isinstance(by_user, dict):
        by_user = {}
    base = by_user.get(key)
    if base is None and key == "guest":
        # Seed the guest bucket from any legacy global counter on first migration.
        try:
            base = int(data.get("smart_action_uses", 0))
        except (TypeError, ValueError):
            base = 0
    used = int(base or 0) + 1
    by_user[key] = used
    data["smart_action_uses_by_user"] = by_user
    try:
        storage.atomic_write_json(_USAGE_PATH, data)
    except Exception:
        logger.debug("Could not persist smart-action usage", exc_info=True)
    return used
