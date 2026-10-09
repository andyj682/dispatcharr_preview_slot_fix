"""Stop direct stream previews from freeing a running channel's provider slot.

THE BUG (Dispatcharr v0.31.0 and v0.32.0; reported upstream as Dispatcharr#1773)
---------------------------------------------------------------------------------
Channels record their provider assignment as ``channel_stream:{channel_id}`` and
``stream_profile:{stream_id}``, where ``stream_id`` is the stream the channel
STARTED on. ``channel_stream`` is never updated on failover, so the channel's
profile stays filed under its starting stream for the life of the channel.

Direct stream previews (the Streams view) keep their bookkeeping in the SAME
namespace, in ``apps.channels.models.Stream``:

* ``get_stream()`` reads ``stream_profile:{self.id}`` first and, if it exists,
  returns it as the preview's own reservation without reserving anything.
* ``release_stream()`` reads that key on teardown, deletes it and releases the
  profile it names.

So previewing the stream a running channel started on borrows the channel's
reservation, and closing the preview frees the channel's slot while it keeps
playing. The provider then reads as free while in use, and the channel's own
record is gone, so its later failovers can no longer move its counter.

Two more defects on the same path:

* ``get_stream()`` writes ``channel_stream:{self.id}`` -- a STREAM id in the
  namespace channels key by CHANNEL id -- so previewing stream N overwrites the
  record of channel N.
* ``release_stream()`` does not clear the preview's metadata (v0.31.0; fixed in
  v0.32.0). Under the default ``channel_shutdown_delay`` of 0 the output
  generator releases the preview when its last viewer leaves, and teardown then
  finds the key gone and falls back to the METADATA, releasing the same provider
  a second time. ``Channel.release_stream()`` already clears those fields for
  exactly this reason, and so does the profile-scoped preview release.

v0.32.0 added profile-scoped previews (``get_stream(preferred_profile_id=N)``,
``release_stream(m3u_profile_id=N)``, worker id ``{stream_hash}.p{N}``), which
keep their own ``stream_profile:{id}:p{N}`` key and do not collide. But the
default preview -- the parameter left at None, a bare stream-hash worker id --
is unchanged, and it is the only kind the web UI starts.

THE FIX
-------
Do for the default preview what upstream already does for its profile-scoped
previews:

1. ``Stream.get_stream`` keeps the preview's reservation under its own key
   (``PREVIEW_KEY_PREFIX``) and never writes ``stream_profile:{id}`` or
   ``channel_stream:{id}``. An existing preview key is reused only while that
   preview is live -- the same rule as upstream's
   ``_scoped_preview_assignment_is_reusable`` -- otherwise it is a leftover:
   released once, then replaced by a fresh reservation.
2. ``Stream.release_stream`` releases only that key, clears the preview's
   ``STREAM_ID``/``M3U_PROFILE`` metadata so the teardown fallback cannot release
   again, and releases the slot only when it was the caller that actually
   deleted the key.
3. ``ChannelService.initialize_channel`` -- for a preview, the stream request
   handler looks its stream and profile up in the shared keys this plugin no
   longer writes (and which, in the collision case, belong to a channel). The
   wrapper replaces them with the preview's own values, so the preview's
   metadata is accurate. That also keeps a preview that is live when the plugin
   is DISABLED releasable through stock code's metadata fallback.

Profile-scoped previews (v0.32.0+) are passed to stock code untouched by all
three wrappers: stock is already correct for them.

All three targets are class attributes, so callers resolve them at call time and
no module rebinding is needed. All three run only in the uWSGI workers that
serve the live proxy.

This module holds no Django imports at module scope so the pure logic can be
unit-tested off-server.
"""

from __future__ import annotations

import functools
import inspect
import logging
import uuid

logger = logging.getLogger("plugins.dispatcharr_preview_slot_fix")

# Dispatcharr's LOGGING dictConfig names the loggers it manages (`apps`,
# `celery`, `core.*`, `django.geventpool`, root) and gives each its own handler
# with propagate=False. `plugins.*` is NOT among them, so a plugin logger has no
# handler, no level, and inherits root's EFFECTIVE level. That is fine in uWSGI
# (root at INFO), but Celery's prefork pool reconfigures root in each forked
# child and leaves it at WARNING -- which discards every plugin `logger.info()`
# at the logger, before any handler sees it, making a working plugin look
# identical to an absent one. Adopting `apps`'s level fixes that while still
# honoring DISPATCHARR_LOG_LEVEL; the NOTSET guard leaves an operator (or a
# test) that deliberately set a level in control.
if logger.level == logging.NOTSET:
    logger.setLevel(logging.getLogger("apps").getEffectiveLevel() or logging.INFO)

PLUGIN_KEY = "dispatcharr_preview_slot_fix"
LOG_TAG = "[PREVIEW-SLOT-FIX]"

# The preview's own reservation. Deliberately outside both `stream_profile:*`
# and `channel_stream:*`, so no channel code path can read, reuse or delete it.
PREVIEW_KEY_PREFIX = "preview_slot_fix:stream_profile:"

FULL_ERROR = "All active M3U profiles have reached maximum connection limits"

# The signatures this plugin knows, oldest first: v0.31.0, then v0.32.0 (which
# added the profile-scoped preview parameter). install() refuses any other shape.
SUPPORTED_GET_STREAM_PARAMS = (
    ("self", "requester"),
    ("self", "requester", "preferred_profile_id"),
)
SUPPORTED_RELEASE_STREAM_PARAMS = (
    ("self",),
    ("self", "m3u_profile_id"),
)
REQUIRED_INITIALIZE_PARAMS = ("channel_id", "stream_id", "m3u_profile_id")

# How each Stream method's arguments are routed at call time:
#   ignored -- accepted and irrelevant to the preview's bookkeeping;
#   scoped  -- not None selects core's profile-scoped preview, which is
#              collision-free in stock code, so the call goes to stock.
# Core passes `scoped` on EVERY call, as None for a default preview, so routing
# must look at its value, never at whether it was passed.
# Any other argument means core changed under us: stock, plus one warning.
ROUTE_PLUGIN = "plugin"
ROUTE_SCOPED = "scoped"
ROUTE_DRIFT = "drift"
_ROUTING = {
    "get_stream": {"ignored": ("requester",), "scoped": "preferred_profile_id"},
    "release_stream": {"ignored": (), "scoped": "m3u_profile_id"},
}


def preview_key(stream_id) -> str:
    return f"{PREVIEW_KEY_PREFIX}{int(stream_id)}"


def classify_call(original, name, instance, args, kwargs) -> str:
    """Which path a call to ``Stream.<name>`` takes; see ``_ROUTING``.

    Binds against the live original's signature, so it is the same whether core
    passes an argument by keyword or by position.
    """
    rule = _ROUTING[name]
    try:
        bound = inspect.signature(original).bind(instance, *args, **kwargs)
    except TypeError:
        return ROUTE_DRIFT
    for param, value in list(bound.arguments.items())[1:]:
        if param in rule["ignored"]:
            continue
        if param == rule["scoped"]:
            if value is not None:
                return ROUTE_SCOPED
            continue
        return ROUTE_DRIFT
    return ROUTE_PLUGIN


def scoped_preview_profile(worker_id):
    """The profile id of a profile-scoped preview worker id, else None.

    Mirrors core's ``parse_preview_worker_id`` (v0.32.0+): ``{stream_hash}.p{N}``.
    """
    if not worker_id or not isinstance(worker_id, str):
        return None
    head, sep, tail = worker_id.rpartition(".p")
    if not sep or not head or not tail.isdigit():
        return None
    return int(tail)


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


def looks_like_stream_hash(channel_id) -> bool:
    """Channels are addressed by UUID; direct stream previews by stream hash."""
    if not channel_id:
        return False
    try:
        uuid.UUID(str(channel_id))
    except (ValueError, AttributeError, TypeError):
        return True
    return False


# --------------------------------------------------------------------------- #
# Pure logic. Dependencies are injected so it runs off-server.
# --------------------------------------------------------------------------- #

def preview_is_live(redis, metadata_key, state_field, live_states) -> bool:
    """Upstream's liveness rule for a preview reservation.

    No metadata yet is the gap between reserving and initializing, so the
    reservation is still in use. Metadata that exists but is not in a live
    state belongs to a preview that ended without releasing -- a leftover.
    """
    if not metadata_key:
        return False
    if not redis.exists(metadata_key):
        return True
    state = redis.hget(metadata_key, state_field)
    if state is None:
        return False
    return _decode(state) in live_states


def acquire_preview_slot(
    *,
    stream_id,
    metadata_key,
    profiles,
    redis,
    reserve,
    release,
    state_field,
    live_states,
):
    """Replacement for ``Stream.get_stream``'s bookkeeping.

    Returns core's tuple: ``(stream_id, profile_id, error_reason, slot_reserved)``.
    ``slot_reserved`` is True only when THIS call took a new slot, which is what
    tells the caller whether it must release on a later failure.
    """
    key = preview_key(stream_id)
    existing = redis.get(key)
    if existing:
        existing_profile = int(_decode(existing))
        if preview_is_live(redis, metadata_key, state_field, live_states):
            return stream_id, existing_profile, None, False
        # Leftover from a preview that never released. Its slot is still
        # counted (the key and the counter live in the same Redis), so release
        # it exactly once -- only the caller that actually removes the key does.
        if redis.delete(key):
            release(existing_profile, redis)
            logger.info(
                "%s Released leftover preview reservation for stream %s "
                "(profile %s)",
                LOG_TAG, stream_id, existing_profile,
            )

    for profile in profiles:
        if not getattr(profile, "is_active", True):
            continue
        reserved, _count, _reason = reserve(profile, redis)
        if not reserved:
            continue
        # Claim the key only if nobody else has. Two requests starting the same
        # preview at once could otherwise both reserve and one overwrite the
        # other's key, leaving a counted slot that no teardown would release.
        if redis.set(key, profile.id, nx=True):
            return stream_id, profile.id, None, True
        release(profile.id, redis)
        winner = redis.get(key)
        if winner:
            return stream_id, int(_decode(winner)), None, False
        # The winner released between our SET and GET -- vanishingly rare.
        # Report full rather than hand back a slot nobody holds.
        return None, None, FULL_ERROR, False

    return None, None, FULL_ERROR, False


def release_preview_slot(
    *,
    stream_id,
    metadata_key,
    redis,
    release,
    metadata_fields,
):
    """Replacement for ``Stream.release_stream``'s bookkeeping.

    Returns True when the preview's reservation is accounted for (released here,
    or concurrently by another caller), False when there was none -- which lets
    stock teardown fall back to the preview's metadata, exactly as before.
    """
    key = preview_key(stream_id)
    profile_id = redis.get(key)
    if not profile_id:
        return False
    if not redis.delete(key):
        # Another teardown path deleted it between our read and our delete and
        # is releasing the slot. Report it handled, so nobody falls back to the
        # metadata and releases it a second time.
        return True
    if metadata_key:
        redis.hdel(metadata_key, *metadata_fields)
    release(int(_decode(profile_id)), redis)
    return True


def resolve_preview_initialization(*, stream_id, redis):
    """The (stream_id, m3u_profile_id) a preview's metadata should record."""
    if stream_id is None:
        return None, None
    profile_id = redis.get(preview_key(stream_id))
    if not profile_id:
        return None, None
    return int(stream_id), int(_decode(profile_id))


def ordered_profiles(m3u_account):
    """Default profile first, then the rest -- core's own selection order."""
    profiles = list(m3u_account.profiles.all())
    default = [p for p in profiles if p.is_default]
    rest = [p for p in profiles if not p.is_default]
    return default + rest


# --------------------------------------------------------------------------- #
# Wrappers
# --------------------------------------------------------------------------- #

_originals = {}
_drift_logged = set()


def _log_drift_once(target, args, kwargs):
    """One trace per process when core passes parameters we don't know.

    Uses `warning` deliberately: it survives even where a pool has left root at
    WARNING, which is precisely where a plugin's INFO lines do not.
    """
    if target in _drift_logged:
        return
    _drift_logged.add(target)
    logger.warning(
        "%s %s was called with %s positional and keyword(s) %s that this plugin "
        "does not recognize -- handed to stock Dispatcharr unchanged, so the "
        "preview-slot fix is NOT applied to that call. Core's signature has "
        "changed: re-check this plugin against the current Dispatcharr release.",
        LOG_TAG, target, len(args), sorted(kwargs),
    )


def _runtime():
    """Late imports of Dispatcharr internals (only valid inside the app)."""
    from apps.m3u.connection_pool import release_profile_slot, reserve_profile_slot
    from apps.proxy.live_proxy.constants import ChannelMetadataField, ChannelState
    from apps.proxy.live_proxy.redis_keys import RedisKeys
    from core.utils import RedisClient

    live_states = (
        ChannelState.ACTIVE,
        ChannelState.WAITING_FOR_CLIENTS,
        ChannelState.BUFFERING,
        ChannelState.INITIALIZING,
        ChannelState.CONNECTING,
    )
    return {
        "redis": RedisClient.get_client(),
        "reserve": reserve_profile_slot,
        "release": release_profile_slot,
        "metadata_key": RedisKeys.channel_metadata,
        "state_field": ChannelMetadataField.STATE,
        "metadata_fields": (
            ChannelMetadataField.STREAM_ID,
            ChannelMetadataField.M3U_PROFILE,
        ),
        "live_states": live_states,
    }


def _route(name, instance, args, kwargs):
    """(original, route) for a call, logging drift once."""
    original = _originals[name]
    route = classify_call(original, name, instance, args, kwargs)
    if route == ROUTE_DRIFT:
        _log_drift_once(f"Stream.{name}", args, kwargs)
    return original, route


def patched_get_stream(self, *args, **kwargs):
    original, route = _route("get_stream", self, args, kwargs)
    if route != ROUTE_PLUGIN:
        return original(self, *args, **kwargs)
    try:
        rt = _runtime()
        m3u_account = self.m3u_account
        if not m3u_account:
            return None, None, "Stream has no M3U account", False
        return acquire_preview_slot(
            stream_id=self.id,
            metadata_key=rt["metadata_key"](self.stream_hash) if self.stream_hash else None,
            profiles=ordered_profiles(m3u_account),
            redis=rt["redis"],
            reserve=rt["reserve"],
            release=rt["release"],
            state_field=rt["state_field"],
            live_states=rt["live_states"],
        )
    except Exception:
        logger.exception(
            "%s get_stream failed for stream %s; falling back to stock",
            LOG_TAG, getattr(self, "id", "?"),
        )
        return original(self, *args, **kwargs)


def patched_release_stream(self, *args, **kwargs):
    original, route = _route("release_stream", self, args, kwargs)
    if route != ROUTE_PLUGIN:
        return original(self, *args, **kwargs)
    try:
        rt = _runtime()
        released = release_preview_slot(
            stream_id=self.id,
            metadata_key=rt["metadata_key"](self.stream_hash) if self.stream_hash else None,
            redis=rt["redis"],
            release=rt["release"],
            metadata_fields=rt["metadata_fields"],
        )
    except Exception:
        logger.exception(
            "%s release_stream failed for stream %s; falling back to stock",
            LOG_TAG, getattr(self, "id", "?"),
        )
        return original(self, *args, **kwargs)
    if released:
        return True
    # No preview reservation of ours. Do NOT fall through to stock: stock reads
    # the shared `stream_profile:{id}` key, which may be a running channel's --
    # the very bug. Returning False lets core's teardown use the preview's own
    # metadata instead, which covers a preview started before this plugin.
    return False


def stream_pair_active() -> bool:
    """True while this process runs the plugin's Stream.get_stream/release_stream."""
    return "get_stream" in _originals and "release_stream" in _originals


def _make_initialize_wrapper(original):
    signature = inspect.signature(original)

    @functools.wraps(original)
    def patched_initialize_channel(*args, **kwargs):
        # The preview's own key exists only while the Stream pair is ours.
        # Without the pair, stock reserved the preview and the caller's values
        # are stock's own correct ones -- overriding them would erase them.
        if not stream_pair_active():
            return original(*args, **kwargs)
        try:
            bound = signature.bind_partial(*args, **kwargs)
        except TypeError:
            _log_drift_once("ChannelService.initialize_channel", args, kwargs)
            return original(*args, **kwargs)
        channel_id = bound.arguments.get("channel_id")
        # Channels (UUID) and profile-scoped previews ({hash}.p{N}, v0.32.0+)
        # are stock's business; only a default preview is ours.
        if (
            not looks_like_stream_hash(channel_id)
            or scoped_preview_profile(channel_id) is not None
        ):
            return original(*args, **kwargs)
        try:
            from apps.channels.models import Stream

            stream_id = (
                Stream.objects.filter(stream_hash=channel_id)
                .values_list("id", flat=True)
                .first()
            )
            sid, pid = resolve_preview_initialization(
                stream_id=stream_id, redis=_runtime()["redis"]
            )
        except Exception:
            logger.exception(
                "%s could not resolve preview %s; initializing with stock values",
                LOG_TAG, channel_id,
            )
            return original(*args, **kwargs)
        # For a preview, its own key is the only trustworthy source. What the
        # request handler passed came from the shared keys -- absent under this
        # plugin, or a CHANNEL's record in the collision case.
        bound.arguments["stream_id"] = sid
        bound.arguments["m3u_profile_id"] = pid
        return original(*bound.args, **bound.kwargs)

    patched_initialize_channel._preview_slot_fix_wrapper = True
    return patched_initialize_channel


# --------------------------------------------------------------------------- #
# Install / revert
# --------------------------------------------------------------------------- #

_TAG = "_preview_slot_fix_wrapper"


def _params(func):
    return tuple(inspect.signature(func).parameters)


def signature_supported(func, supported) -> bool:
    return func is not None and _params(func) in supported


def install():
    """Patch all three targets. Idempotent. Returns {target: bool}."""
    from apps.channels.models import Stream
    from apps.proxy.live_proxy.services.channel_service import ChannelService

    result = {}

    for name, replacement, supported in (
        ("get_stream", patched_get_stream, SUPPORTED_GET_STREAM_PARAMS),
        ("release_stream", patched_release_stream, SUPPORTED_RELEASE_STREAM_PARAMS),
    ):
        current = Stream.__dict__.get(name)
        if getattr(current, _TAG, False):
            result[f"Stream.{name}"] = True
            continue
        if not signature_supported(current, supported):
            logger.error(
                "%s Stream.%s has signature %s, expected one of %s -- NOT patched. "
                "Re-check this plugin against the current Dispatcharr release.",
                LOG_TAG, name, _params(current) if current else None, supported,
            )
            result[f"Stream.{name}"] = False
            continue
        _originals[name] = current
        wrapped = functools.wraps(current)(replacement)
        setattr(wrapped, _TAG, True)
        setattr(Stream, name, wrapped)
        result[f"Stream.{name}"] = True

    # The two Stream patches only work as a pair: a patched get_stream with a
    # stock release_stream (or the reverse) would leave reservations nobody
    # releases. If either failed, revert both.
    if not (result.get("Stream.get_stream") and result.get("Stream.release_stream")):
        _revert_stream_pair(Stream)
        result["Stream.get_stream"] = result["Stream.release_stream"] = False

    raw = ChannelService.__dict__.get("initialize_channel")
    current = raw.__func__ if isinstance(raw, staticmethod) else raw
    if getattr(current, _TAG, False):
        result["ChannelService.initialize_channel"] = True
    elif not result["Stream.get_stream"]:
        # It reads the preview key only the patched pair writes; alone it would
        # replace stock's correct preview metadata with nothing.
        logger.error(
            "%s ChannelService.initialize_channel NOT patched because the Stream "
            "patches are not installed. Dispatcharr is running stock preview code.",
            LOG_TAG,
        )
        result["ChannelService.initialize_channel"] = False
    elif current is None or not all(
        p in _params(current) for p in REQUIRED_INITIALIZE_PARAMS
    ):
        logger.error(
            "%s ChannelService.initialize_channel lacks %s -- NOT patched "
            "(previews keep working; only their metadata would be incomplete).",
            LOG_TAG, REQUIRED_INITIALIZE_PARAMS,
        )
        result["ChannelService.initialize_channel"] = False
    else:
        _originals["initialize_channel"] = current
        ChannelService.initialize_channel = staticmethod(_make_initialize_wrapper(current))
        result["ChannelService.initialize_channel"] = True

    logger.info("%s install: %s", LOG_TAG, result)
    return result


def _revert_stream_pair(Stream):
    for name in ("get_stream", "release_stream"):
        current = Stream.__dict__.get(name)
        if getattr(current, _TAG, False) and name in _originals:
            setattr(Stream, name, _originals.pop(name))


def uninstall():
    from apps.channels.models import Stream
    from apps.proxy.live_proxy.services.channel_service import ChannelService

    _revert_stream_pair(Stream)
    raw = ChannelService.__dict__.get("initialize_channel")
    current = raw.__func__ if isinstance(raw, staticmethod) else raw
    if getattr(current, _TAG, False) and "initialize_channel" in _originals:
        ChannelService.initialize_channel = staticmethod(_originals.pop("initialize_channel"))
    _drift_logged.clear()
    logger.info("%s patches reverted", LOG_TAG)


def patch_state():
    """{target: bool} for what is patched in THIS process right now."""
    from apps.channels.models import Stream
    from apps.proxy.live_proxy.services.channel_service import ChannelService

    raw = ChannelService.__dict__.get("initialize_channel")
    init = raw.__func__ if isinstance(raw, staticmethod) else raw
    return {
        "Stream.get_stream": bool(getattr(Stream.__dict__.get("get_stream"), _TAG, False)),
        "Stream.release_stream": bool(getattr(Stream.__dict__.get("release_stream"), _TAG, False)),
        "ChannelService.initialize_channel": bool(getattr(init, _TAG, False)),
    }


def active_preview_reservations():
    """[(stream_id, profile_id, live)] for every reservation this plugin holds."""
    rt = _runtime()
    redis = rt["redis"]
    from apps.channels.models import Stream

    out = []
    for raw_key in redis.scan_iter(match=f"{PREVIEW_KEY_PREFIX}*", count=200):
        key = _decode(raw_key)
        try:
            stream_id = int(key[len(PREVIEW_KEY_PREFIX):])
            profile_id = int(_decode(redis.get(key)))
        except (TypeError, ValueError):
            continue
        stream_hash = (
            Stream.objects.filter(id=stream_id).values_list("stream_hash", flat=True).first()
        )
        live = preview_is_live(
            redis,
            rt["metadata_key"](stream_hash) if stream_hash else None,
            rt["state_field"],
            rt["live_states"],
        )
        out.append((stream_id, profile_id, live))
    return sorted(out)
