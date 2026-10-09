"""Pure-logic tests for the preview slot fix.

Runs off-server: no Django, no Dispatcharr imports.
    python test_logic.py

The fake reserve/release below follow core's ``reserve_profile_slot`` /
``release_profile_slot`` contract (INCR first, compare against max_streams,
DECR back on overflow; release never goes below zero), so the counter
arithmetic here is the same arithmetic the server does.
"""

import logging
import sys
import types
import unittest

import patch

logging.getLogger("plugins.dispatcharr_preview_slot_fix").setLevel(logging.CRITICAL)

STATE_FIELD = "state"
LIVE_STATES = ("active", "waiting_for_clients", "buffering", "initializing", "connecting")
META_FIELDS = ("stream_id", "m3u_profile")


class FakeRedis:
    def __init__(self):
        self.strings = {}
        self.hashes = {}

    def get(self, key):
        value = self.strings.get(key)
        return None if value is None else str(value).encode()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.strings:
                del self.strings[key]
                removed += 1
            if key in self.hashes:
                del self.hashes[key]
                removed += 1
        return removed

    def exists(self, key):
        return key in self.strings or key in self.hashes

    def hget(self, key, field):
        value = self.hashes.get(key, {}).get(field)
        return None if value is None else str(value).encode()

    def hset(self, key, field=None, value=None, mapping=None):
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
        if field is not None:
            bucket[field] = value

    def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        for field in fields:
            bucket.pop(field, None)

    def incr(self, key):
        self.strings[key] = int(self.strings.get(key, 0)) + 1
        return self.strings[key]

    def decr(self, key):
        self.strings[key] = int(self.strings.get(key, 0)) - 1
        return self.strings[key]

    def scan_iter(self, match=None, count=None):
        prefix = (match or "").rstrip("*")
        return [k.encode() for k in list(self.strings) if k.startswith(prefix)]


def counter_key(profile_id):
    return f"profile_connections:{profile_id}"


def fake_reserve(profile, redis):
    key = counter_key(profile.id)
    count = redis.incr(key)
    if profile.max_streams and count > profile.max_streams:
        redis.decr(key)
        return False, count - 1, "profile_full"
    return True, count, None


def fake_release(profile_id, redis):
    key = counter_key(profile_id)
    if int(redis.strings.get(key, 0)) > 0:
        redis.decr(key)


def stock_metadata_fallback(redis, metadata_key):
    """Core's `_release_profile_slot_from_redis_metadata`, reduced to its effect."""
    profile_id = redis.hget(metadata_key, "m3u_profile")
    if not profile_id:
        return False
    redis.hdel(metadata_key, "stream_id", "m3u_profile")
    fake_release(int(profile_id), redis)
    return True


class Profile(types.SimpleNamespace):
    pass


def P(pid, max_streams=1, is_active=True, is_default=True):
    return Profile(id=pid, max_streams=max_streams, is_active=is_active, is_default=is_default)


STREAM = 30001
META = "live:channel:hash-30001:metadata"


def acquire(redis, profiles, stream_id=STREAM, metadata_key=META):
    return patch.acquire_preview_slot(
        stream_id=stream_id,
        metadata_key=metadata_key,
        profiles=profiles,
        redis=redis,
        reserve=fake_reserve,
        release=fake_release,
        state_field=STATE_FIELD,
        live_states=LIVE_STATES,
    )


def release(redis, stream_id=STREAM, metadata_key=META):
    return patch.release_preview_slot(
        stream_id=stream_id,
        metadata_key=metadata_key,
        redis=redis,
        release=fake_release,
        metadata_fields=META_FIELDS,
    )


def count(redis, profile_id):
    return int(redis.strings.get(counter_key(profile_id), 0))


class CollisionWithARunningChannelTests(unittest.TestCase):
    """The bug: a channel that STARTED on stream S files its profile under
    `stream_profile:S`. A preview of S must neither reuse nor delete it."""

    def setUp(self):
        self.redis = FakeRedis()
        self.channel_profile = P(7)
        fake_reserve(self.channel_profile, self.redis)          # channel holds 1/1
        self.redis.set(f"stream_profile:{STREAM}", 7)            # channel's ledger
        self.redis.set("channel_stream:42", STREAM)              # channel 42's record

    def test_preview_does_not_borrow_the_channels_reservation(self):
        result = acquire(self.redis, [P(7)])
        self.assertEqual(result, (None, None, patch.FULL_ERROR, False),
                         "provider is full with the channel; the preview must be refused")

    def test_preview_leaves_the_channels_slot_and_ledger_alone(self):
        acquire(self.redis, [P(7)])
        release(self.redis)
        self.assertEqual(count(self.redis, 7), 1)
        self.assertEqual(self.redis.get(f"stream_profile:{STREAM}"), b"7")
        self.assertEqual(self.redis.get("channel_stream:42"), str(STREAM).encode())

    def test_after_failover_the_backup_provider_stays_counted(self):
        # The live incident: channel started on S (profile 7), failed over to a
        # provider-5 stream, so its ledger under S now says 5.
        fake_release(7, self.redis)                       # channel left provider 7 ...
        fake_reserve(P(5), self.redis)                    # ... for provider 5
        self.redis.set(f"stream_profile:{STREAM}", 5)     # ledger still under S

        sid, pid, error, reserved = acquire(self.redis, [P(7)])
        self.assertEqual(pid, 7, "the preview must use the stream's OWN provider, not 5")
        self.assertTrue(reserved)
        release(self.redis)

        self.assertEqual(count(self.redis, 5), 1, "channel is still on provider 5")
        self.assertEqual(count(self.redis, 7), 0, "the preview's own slot balanced")
        self.assertEqual(self.redis.get(f"stream_profile:{STREAM}"), b"5")

    def test_preview_never_writes_the_shared_namespaces(self):
        before = {k: v for k, v in self.redis.strings.items()
                  if k.startswith(("stream_profile:", "channel_stream:"))}
        acquire(self.redis, [P(8, max_streams=2)])
        release(self.redis)
        after = {k: v for k, v in self.redis.strings.items()
                 if k.startswith(("stream_profile:", "channel_stream:"))}
        self.assertEqual(before, after)


class PreviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()

    def test_reserve_then_release_balances(self):
        sid, pid, error, reserved = acquire(self.redis, [P(3)])
        self.assertEqual((sid, pid, error, reserved), (STREAM, 3, None, True))
        self.assertEqual(count(self.redis, 3), 1)
        self.assertTrue(release(self.redis))
        self.assertEqual(count(self.redis, 3), 0)
        self.assertIsNone(self.redis.get(patch.preview_key(STREAM)))

    def test_full_provider_is_refused_without_side_effects(self):
        fake_reserve(P(3), self.redis)
        self.assertEqual(acquire(self.redis, [P(3)]), (None, None, patch.FULL_ERROR, False))
        self.assertEqual(count(self.redis, 3), 1)
        self.assertIsNone(self.redis.get(patch.preview_key(STREAM)))

    def test_inactive_profiles_are_skipped(self):
        _sid, pid, _e, reserved = acquire(self.redis, [P(3, is_active=False), P(4)])
        self.assertEqual(pid, 4)
        self.assertTrue(reserved)
        self.assertEqual(count(self.redis, 3), 0)

    def test_second_profile_is_used_when_the_first_is_full(self):
        fake_reserve(P(3), self.redis)
        _sid, pid, _e, reserved = acquire(self.redis, [P(3), P(4)])
        self.assertEqual(pid, 4)
        self.assertTrue(reserved)

    def test_release_without_a_reservation_defers_to_stock_fallback(self):
        self.assertFalse(release(self.redis))

    def test_default_profile_is_tried_first(self):
        account = types.SimpleNamespace(profiles=types.SimpleNamespace(
            all=lambda: [P(9, is_default=False), P(3, is_default=True), P(8, is_default=False)]))
        self.assertEqual([p.id for p in patch.ordered_profiles(account)], [3, 9, 8])


class DoubleReleaseTests(unittest.TestCase):
    """Default channel_shutdown_delay=0: the generator releases at last-client
    disconnect, then teardown releases again via the metadata fallback."""

    def setUp(self):
        self.redis = FakeRedis()
        acquire(self.redis, [P(3, max_streams=2)])
        # initialize_channel recorded the preview's profile in its metadata.
        self.redis.hset(META, mapping={"stream_id": STREAM, "m3u_profile": 3, STATE_FIELD: "active"})
        # A channel also holds a slot on the same two-slot provider.
        fake_reserve(P(3, max_streams=2), self.redis)
        self.assertEqual(count(self.redis, 3), 2)

    def test_generator_release_then_teardown_releases_once(self):
        self.assertTrue(release(self.redis))                 # generator _cleanup
        if not release(self.redis):                          # teardown, ORM step
            stock_metadata_fallback(self.redis, META)        # teardown, fallback
        self.assertEqual(count(self.redis, 3), 1, "the channel's slot must survive")

    def test_release_clears_the_metadata_the_fallback_reads(self):
        release(self.redis)
        self.assertIsNone(self.redis.hget(META, "m3u_profile"))
        self.assertIsNone(self.redis.hget(META, "stream_id"))


class LivenessAndLeftoverTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        acquire(self.redis, [P(3, max_streams=2)])
        self.assertEqual(count(self.redis, 3), 1)

    def test_reused_before_metadata_exists(self):
        self.assertEqual(acquire(self.redis, [P(3, max_streams=2)]), (STREAM, 3, None, False))
        self.assertEqual(count(self.redis, 3), 1)

    def test_reused_while_live(self):
        for state in LIVE_STATES:
            self.redis.hset(META, mapping={STATE_FIELD: state})
            self.assertEqual(acquire(self.redis, [P(3, max_streams=2)])[3], False, state)
        self.assertEqual(count(self.redis, 3), 1)

    def test_leftover_is_released_once_and_replaced(self):
        self.redis.hset(META, mapping={STATE_FIELD: "error"})
        sid, pid, error, reserved = acquire(self.redis, [P(3, max_streams=2)])
        self.assertTrue(reserved, "a fresh reservation replaces the leftover")
        self.assertEqual(count(self.redis, 3), 1, "leftover released, new one taken")

    def test_metadata_without_a_state_is_a_leftover(self):
        self.redis.hset(META, mapping={"stream_id": STREAM})
        self.assertTrue(acquire(self.redis, [P(3, max_streams=2)])[3])
        self.assertEqual(count(self.redis, 3), 1)


class ConcurrentStartTests(unittest.TestCase):
    def test_losing_the_key_race_returns_the_winners_slot(self):
        redis = FakeRedis()
        real_set = redis.set

        def racing_set(key, value, nx=False, ex=None):
            if key == patch.preview_key(STREAM) and nx and key not in redis.strings:
                fake_reserve(P(3, max_streams=2), redis)     # the other request
                real_set(key, 3)
            return real_set(key, value, nx=nx)

        redis.set = racing_set
        sid, pid, error, reserved = acquire(redis, [P(3, max_streams=2)])
        self.assertEqual((pid, reserved), (3, False))
        self.assertEqual(count(redis, 3), 1, "only the winner's slot remains counted")


class SmallHelperTests(unittest.TestCase):
    def test_stream_hash_vs_channel_uuid(self):
        self.assertTrue(patch.looks_like_stream_hash("cd" * 32))
        self.assertFalse(patch.looks_like_stream_hash("00000000-0000-4000-8000-000000000001"))
        self.assertFalse(patch.looks_like_stream_hash(None))
        self.assertFalse(patch.looks_like_stream_hash(""))

    def test_resolve_preview_initialization(self):
        redis = FakeRedis()
        self.assertEqual(patch.resolve_preview_initialization(stream_id=None, redis=redis), (None, None))
        self.assertEqual(patch.resolve_preview_initialization(stream_id=STREAM, redis=redis), (None, None))
        redis.set(patch.preview_key(STREAM), 3)
        self.assertEqual(patch.resolve_preview_initialization(stream_id=STREAM, redis=redis), (STREAM, 3))

    def test_preview_key_is_outside_the_shared_namespaces(self):
        key = patch.preview_key(STREAM)
        self.assertFalse(key.startswith(("stream_profile:", "channel_stream:")))


# --------------------------------------------------------------------------- #
# Stock stand-ins with core's REAL signatures. The wrappers bind against the
# original's signature, so these must match Dispatcharr exactly:
#   v0.31.0  get_stream(self, requester=None)        release_stream(self)
#   v0.32.0  get_stream(self, requester=None,        release_stream(self,
#                       preferred_profile_id=None)                  m3u_profile_id=None)
# --------------------------------------------------------------------------- #

def stock_v031(calls):
    def get_stream(self, requester=None):
        calls.append(("stock get_stream", {"requester": requester}))
        return "stock"

    def release_stream(self):
        calls.append(("stock release_stream", {}))
        return "stock"

    return get_stream, release_stream


def stock_v032(calls):
    def get_stream(self, requester=None, preferred_profile_id=None):
        calls.append(("stock get_stream", {"preferred_profile_id": preferred_profile_id}))
        return "stock"

    def release_stream(self, m3u_profile_id=None):
        calls.append(("stock release_stream", {"m3u_profile_id": m3u_profile_id}))
        return "stock"

    return get_stream, release_stream


def fake_runtime(redis):
    return {
        "redis": redis,
        "reserve": fake_reserve,
        "release": fake_release,
        "metadata_key": lambda stream_hash: f"live:channel:{stream_hash}:metadata",
        "state_field": STATE_FIELD,
        "metadata_fields": META_FIELDS,
        "live_states": LIVE_STATES,
    }


def stream_obj(profiles):
    return types.SimpleNamespace(
        id=STREAM,
        stream_hash="hash-30001",          # metadata key == META
        m3u_account=types.SimpleNamespace(
            profiles=types.SimpleNamespace(all=lambda: list(profiles))),
    )


class _WrapperHarness(unittest.TestCase):
    """Runs the real Stream wrappers against a stock stand-in and FakeRedis."""

    stock = staticmethod(stock_v032)

    def setUp(self):
        self.calls = []
        self.redis = FakeRedis()
        self.saved = dict(patch._originals)
        self.saved_runtime = patch._runtime
        get_stream, release_stream = self.stock(self.calls)
        patch._originals["get_stream"] = get_stream
        patch._originals["release_stream"] = release_stream
        patch._runtime = lambda: fake_runtime(self.redis)
        patch._drift_logged.clear()

    def tearDown(self):
        patch._originals.clear()
        patch._originals.update(self.saved)
        patch._runtime = self.saved_runtime
        patch._drift_logged.clear()

    def get(self, *args, profiles=(), **kwargs):
        stream = stream_obj(profiles or [P(3, max_streams=2)])
        return patch.patched_get_stream(stream, *args, **kwargs)

    def rel(self, *args, **kwargs):
        return patch.patched_release_stream(stream_obj([]), *args, **kwargs)


class V032CallShapeTests(_WrapperHarness):
    """Core v0.32.0 passes the new keywords on EVERY call -- as None for a
    default preview (url_utils.generate_stream_url / release_worker_stream)."""

    def test_default_preview_get_with_none_keyword_takes_the_plugin_path(self):
        self.assertEqual(self.get(preferred_profile_id=None), (STREAM, 3, None, True))
        self.assertEqual(self.calls, [], "stock must not run for a default preview")
        self.assertEqual(self.redis.get(patch.preview_key(STREAM)), b"3")
        self.assertIsNone(self.redis.get(f"stream_profile:{STREAM}"))

    def test_default_preview_release_with_none_keyword_takes_the_plugin_path(self):
        self.get(preferred_profile_id=None)
        self.assertTrue(self.rel(m3u_profile_id=None))
        self.assertEqual(self.calls, [])
        self.assertEqual(count(self.redis, 3), 0)

    def test_positional_none_is_the_plugin_path_too(self):
        self.assertEqual(self.get(None, None)[3], True)
        self.assertTrue(self.rel(None))
        self.assertEqual(self.calls, [])

    def test_the_collision_is_fixed_with_v032_call_shapes(self):
        fake_reserve(P(7), self.redis)                       # channel holds 1/1
        self.redis.set(f"stream_profile:{STREAM}", 7)        # channel's ledger
        self.assertEqual(self.get(preferred_profile_id=None, profiles=[P(7)]),
                         (None, None, patch.FULL_ERROR, False))
        self.assertFalse(self.rel(m3u_profile_id=None))
        self.assertEqual(count(self.redis, 7), 1, "the channel's slot must survive")
        self.assertEqual(self.redis.get(f"stream_profile:{STREAM}"), b"7")
        self.assertEqual(self.calls, [])

    def test_scoped_get_goes_to_stock_without_a_warning(self):
        self.assertEqual(self.get(preferred_profile_id=3), "stock")
        self.assertEqual(self.calls, [("stock get_stream", {"preferred_profile_id": 3})])
        self.assertEqual(patch._drift_logged, set())
        self.assertEqual(self.redis.strings, {})

    def test_scoped_release_goes_to_stock_without_a_warning(self):
        self.assertEqual(self.rel(m3u_profile_id=3), "stock")
        self.assertEqual(self.calls, [("stock release_stream", {"m3u_profile_id": 3})])
        self.assertEqual(patch._drift_logged, set())

    def test_scoped_positional_goes_to_stock(self):
        self.assertEqual(self.get(None, 3), "stock")
        self.assertEqual(self.rel(3), "stock")

    def test_unknown_keyword_is_drift_reported_once(self):
        # Handed to stock unchanged -- including stock's own TypeError.
        for _ in range(3):
            with self.assertRaises(TypeError):
                self.get(lease=1)
        self.assertEqual(patch._drift_logged, {"Stream.get_stream"})
        self.assertEqual(self.redis.strings, {})


class V031CallShapeTests(_WrapperHarness):
    stock = staticmethod(stock_v031)

    def test_v031_calls_take_the_plugin_path(self):
        self.assertEqual(self.get()[3], True)
        self.assertEqual(self.get(requester="someone")[3], False, "reused while live")
        self.assertTrue(self.rel())
        self.assertEqual(self.calls, [])

    def test_v032_keyword_on_a_v031_core_is_drift(self):
        # Exactly what stock v0.31.0 does with such a call: raise.
        with self.assertRaises(TypeError):
            self.get(preferred_profile_id=None)
        with self.assertRaises(TypeError):
            self.rel(m3u_profile_id=None)
        self.assertEqual(patch._drift_logged, {"Stream.get_stream", "Stream.release_stream"})
        self.assertEqual(self.redis.strings, {})


class ClassifyCallTests(unittest.TestCase):
    def test_routes(self):
        get32, rel32 = stock_v032([])
        cases = [
            ((get32, "get_stream", (), {}), patch.ROUTE_PLUGIN),
            ((get32, "get_stream", (), {"requester": "u"}), patch.ROUTE_PLUGIN),
            ((get32, "get_stream", (), {"preferred_profile_id": None}), patch.ROUTE_PLUGIN),
            ((get32, "get_stream", (), {"preferred_profile_id": 0}), patch.ROUTE_SCOPED),
            ((get32, "get_stream", (), {"preferred_profile_id": "4"}), patch.ROUTE_SCOPED),
            ((get32, "get_stream", (None, None, None), {}), patch.ROUTE_DRIFT),
            ((rel32, "release_stream", (), {}), patch.ROUTE_PLUGIN),
            ((rel32, "release_stream", (), {"m3u_profile_id": None}), patch.ROUTE_PLUGIN),
            ((rel32, "release_stream", (), {"m3u_profile_id": 2}), patch.ROUTE_SCOPED),
            ((rel32, "release_stream", (), {"force": True}), patch.ROUTE_DRIFT),
        ]
        for (original, name, args, kwargs), expected in cases:
            with self.subTest(name=name, args=args, kwargs=kwargs):
                self.assertEqual(
                    patch.classify_call(original, name, object(), args, kwargs), expected)

    def test_a_parameter_the_plugin_does_not_know_is_drift(self):
        # A future core whose original accepts something new: the call binds,
        # but the plugin can't know what it means, even when it is None.
        def get_stream(self, requester=None, preferred_profile_id=None, lease=None):
            return "stock"

        for kwargs in ({"lease": 1}, {"lease": None}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(
                    patch.classify_call(get_stream, "get_stream", object(), (), kwargs),
                    patch.ROUTE_DRIFT)
        self.assertEqual(
            patch.classify_call(get_stream, "get_stream", object(), (), {}),
            patch.ROUTE_PLUGIN)

    def test_scoped_worker_ids(self):
        self.assertEqual(patch.scoped_preview_profile("ab" * 32 + ".p3"), 3)
        self.assertIsNone(patch.scoped_preview_profile("ab" * 32))
        self.assertIsNone(patch.scoped_preview_profile("00000000-0000-4000-8000-000000000001"))
        self.assertIsNone(patch.scoped_preview_profile(".p3"))
        self.assertIsNone(patch.scoped_preview_profile("abc.pX"))
        self.assertIsNone(patch.scoped_preview_profile(None))


# --------------------------------------------------------------------------- #
# install() against stand-in Django modules
# --------------------------------------------------------------------------- #

def _initialize_channel(channel_id, stream_url, user_agent, transcode=False,
                        stream_profile_value=None, stream_id=None,
                        m3u_profile_id=None, channel_name=None, stream_name=None):
    return (channel_id, stream_id, m3u_profile_id)


class _FakeModules:
    """Temporarily puts stand-in modules into sys.modules."""

    def __init__(self, **attrs_by_module):
        self.attrs_by_module = attrs_by_module

    def __enter__(self):
        names = set()
        for dotted in self.attrs_by_module:
            parts = dotted.split(".")
            names.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        self.saved = {n: sys.modules.get(n) for n in names}
        for n in names:
            sys.modules[n] = types.ModuleType(n)
        for dotted, attrs in self.attrs_by_module.items():
            for key, value in attrs.items():
                setattr(sys.modules[dotted], key, value)
        return self

    def __exit__(self, *exc):
        for n, mod in self.saved.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod


class _FakeDjango(_FakeModules):
    """`apps.channels.models.Stream` and `ChannelService` stand-ins for install()."""

    def __init__(self, get_stream, release_stream):
        self.Stream = type("Stream", (), {"get_stream": get_stream,
                                          "release_stream": release_stream})
        self.ChannelService = type("ChannelService", (), {
            "initialize_channel": staticmethod(_initialize_channel)})
        super().__init__(**{
            "apps.channels.models": {"Stream": self.Stream},
            "apps.proxy.live_proxy.services.channel_service":
                {"ChannelService": self.ChannelService},
        })

    def __enter__(self):
        super().__enter__()
        self.saved_originals = dict(patch._originals)
        patch._originals.clear()
        return self

    def __exit__(self, *exc):
        patch.uninstall()
        patch._originals.clear()
        patch._originals.update(self.saved_originals)
        super().__exit__(*exc)


def stream_lookup(stream_id):
    """`Stream.objects.filter(...).values_list(...).first()` -> stream_id."""
    result = types.SimpleNamespace(first=lambda: stream_id)
    query = types.SimpleNamespace(values_list=lambda *a, **k: result)
    stream_cls = type("Stream", (), {
        "objects": types.SimpleNamespace(filter=lambda **k: query)})
    return _FakeModules(**{"apps.channels.models": {"Stream": stream_cls}})


ALL_ON = {"Stream.get_stream": True, "Stream.release_stream": True,
          "ChannelService.initialize_channel": True}
ALL_OFF = {k: False for k in ALL_ON}


class InstallTests(unittest.TestCase):
    def test_installs_on_v031(self):
        with _FakeDjango(*stock_v031([])):
            self.assertEqual(patch.install(), ALL_ON)
            self.assertEqual(patch.patch_state(), ALL_ON)
            self.assertTrue(patch.stream_pair_active())
            self.assertEqual(patch.install(), ALL_ON, "idempotent")

    def test_installs_on_v032(self):
        with _FakeDjango(*stock_v032([])):
            self.assertEqual(patch.install(), ALL_ON)
            self.assertEqual(patch.patch_state(), ALL_ON)

    def test_uninstall_restores_the_originals(self):
        get_stream, release_stream = stock_v032([])
        with _FakeDjango(get_stream, release_stream) as fake:
            patch.install()
            patch.uninstall()
            self.assertIs(fake.Stream.__dict__["get_stream"], get_stream)
            self.assertIs(fake.Stream.__dict__["release_stream"], release_stream)
            self.assertIs(fake.ChannelService.__dict__["initialize_channel"].__func__,
                          _initialize_channel)
            self.assertFalse(patch.stream_pair_active())

    def test_refuses_an_unknown_shape_entirely(self):
        def get_stream(self, requester=None, preferred_profile_id=None, lease=None):
            return "stock"

        _get, release_stream = stock_v032([])
        with _FakeDjango(get_stream, release_stream) as fake:
            self.assertEqual(patch.install(), ALL_OFF,
                             "no half-installed state: initialize must not be patched alone")
            self.assertIs(fake.Stream.__dict__["release_stream"], release_stream)
            self.assertFalse(patch.stream_pair_active())

    def test_refuses_when_only_release_stream_is_unknown(self):
        get31, _rel31 = stock_v031([])

        def release_stream(self, m3u_profile_id=None, force=False):
            return "stock"

        with _FakeDjango(get31, release_stream) as fake:
            self.assertEqual(patch.install(), ALL_OFF)
            self.assertIs(fake.Stream.__dict__["get_stream"], get31, "pair reverted")

    def test_supported_shapes(self):
        get31, rel31 = stock_v031([])
        get32, rel32 = stock_v032([])
        self.assertTrue(patch.signature_supported(get31, patch.SUPPORTED_GET_STREAM_PARAMS))
        self.assertTrue(patch.signature_supported(get32, patch.SUPPORTED_GET_STREAM_PARAMS))
        self.assertTrue(patch.signature_supported(rel31, patch.SUPPORTED_RELEASE_STREAM_PARAMS))
        self.assertTrue(patch.signature_supported(rel32, patch.SUPPORTED_RELEASE_STREAM_PARAMS))
        self.assertFalse(patch.signature_supported(rel32, patch.SUPPORTED_GET_STREAM_PARAMS))
        self.assertFalse(patch.signature_supported(None, patch.SUPPORTED_GET_STREAM_PARAMS))


class InitializeWrapperTests(unittest.TestCase):
    HASH = "ab" * 32

    def setUp(self):
        self.seen = []

        def initialize_channel(channel_id, stream_url, user_agent, transcode=False,
                               stream_profile_value=None, stream_id=None,
                               m3u_profile_id=None, channel_name=None, stream_name=None):
            self.seen.append((channel_id, stream_id, m3u_profile_id))
            return True

        self.wrapper = patch._make_initialize_wrapper(initialize_channel)
        self.redis = FakeRedis()
        self.saved = dict(patch._originals)
        self.saved_runtime = patch._runtime
        patch._runtime = lambda: fake_runtime(self.redis)
        self.set_pair_active(True)

    def tearDown(self):
        patch._originals.clear()
        patch._originals.update(self.saved)
        patch._runtime = self.saved_runtime

    def set_pair_active(self, active):
        for name in ("get_stream", "release_stream"):
            if active:
                patch._originals[name] = lambda *a, **k: None
            else:
                patch._originals.pop(name, None)

    def test_channels_pass_through_unchanged(self):
        uid = "00000000-0000-4000-8000-000000000001"
        self.assertTrue(self.wrapper(uid, "u", "ua", False, 1, 30, 7, channel_name="x"))
        self.assertEqual(self.seen, [(uid, 30, 7)])

    def test_fails_open_when_the_lookup_is_unavailable(self):
        # Off-server the Django import fails: the call must still go through,
        # with the caller's own arguments.
        self.assertTrue(self.wrapper(self.HASH, "u", "ua", False, 1, None, None))
        self.assertEqual(self.seen, [(self.HASH, None, None)])

    def test_default_preview_records_its_own_key(self):
        self.redis.set(patch.preview_key(STREAM), 3)
        with stream_lookup(STREAM):
            self.wrapper(self.HASH, "u", "ua", False, 1, STREAM, 7)
        self.assertEqual(self.seen, [(self.HASH, STREAM, 3)],
                         "the channel's profile 7 must be replaced by the preview's own")

    def test_inert_while_the_stream_pair_is_not_installed(self):
        # Half-installed: stock reserved the preview, so the caller's values
        # are stock's correct ones and must not be erased.
        self.set_pair_active(False)
        with stream_lookup(STREAM):
            self.wrapper(self.HASH, "u", "ua", False, 1, STREAM, 7)
        self.assertEqual(self.seen, [(self.HASH, STREAM, 7)])

    def test_scoped_preview_passes_through_unchanged(self):
        worker = self.HASH + ".p4"
        with stream_lookup(STREAM):
            self.wrapper(worker, "u", "ua", False, 1, STREAM, 4)
        self.assertEqual(self.seen, [(worker, STREAM, 4)])


class ManifestParityTests(unittest.TestCase):
    """plugin.json and the Plugin class both declare the UI, so they must agree."""

    @classmethod
    def setUpClass(cls):
        import json
        import os

        import plugin

        cls.cls = plugin.Plugin
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "plugin.json"), encoding="utf-8") as fh:
            cls.man = json.load(fh)

    def test_top_level_matches(self):
        for key in ("name", "version", "description", "author", "help_url"):
            if key in self.man or hasattr(self.cls, key):
                self.assertEqual(self.man.get(key), getattr(self.cls, key, None), key)

    def test_fields_match(self):
        self.assertEqual(self.man["fields"], self.cls.fields)

    def test_actions_match(self):
        self.assertEqual(self.man["actions"], self.cls.actions)

    def test_every_action_is_handled(self):
        import inspect

        src = inspect.getsource(self.cls.run)
        for action in self.cls.actions:
            self.assertIn(f'"{action["id"]}"', src, action["id"])

    def test_plugin_key_matches_the_folder(self):
        import os

        folder = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(patch.PLUGIN_KEY, folder)


if __name__ == "__main__":
    unittest.main(verbosity=2)
