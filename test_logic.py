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


class SignatureDriftTests(unittest.TestCase):
    """Unknown parameters mean a newer core: hand the call to stock untouched."""

    def setUp(self):
        self.calls = []
        self.saved = dict(patch._originals)
        patch._originals["get_stream"] = lambda s, *a, **k: self.calls.append(("get", a, k)) or "stock"
        patch._originals["release_stream"] = lambda s, *a, **k: self.calls.append(("rel", a, k)) or "stock"
        patch._drift_logged.clear()

    def tearDown(self):
        patch._originals.clear()
        patch._originals.update(self.saved)
        patch._drift_logged.clear()

    def test_get_stream_with_unknown_keyword_goes_to_stock(self):
        obj = types.SimpleNamespace(id=STREAM)
        self.assertEqual(patch.patched_get_stream(obj, preferred_profile_id=3), "stock")
        self.assertEqual(self.calls, [("get", (), {"preferred_profile_id": 3})])

    def test_release_stream_with_an_argument_goes_to_stock(self):
        obj = types.SimpleNamespace(id=STREAM)
        self.assertEqual(patch.patched_release_stream(obj, m3u_profile_id=3), "stock")
        self.assertEqual(self.calls, [("rel", (), {"m3u_profile_id": 3})])

    def test_drift_is_reported_once(self):
        obj = types.SimpleNamespace(id=STREAM)
        for _ in range(3):
            patch.patched_get_stream(obj, preferred_profile_id=3)
        self.assertEqual(patch._drift_logged, {"Stream.get_stream"})


class InitializeWrapperTests(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def initialize_channel(channel_id, stream_url, user_agent, transcode=False,
                               stream_profile_value=None, stream_id=None,
                               m3u_profile_id=None, channel_name=None, stream_name=None):
            self.seen.append((channel_id, stream_id, m3u_profile_id))
            return True

        self.wrapper = patch._make_initialize_wrapper(initialize_channel)

    def test_channels_pass_through_unchanged(self):
        uid = "00000000-0000-4000-8000-000000000001"
        self.assertTrue(self.wrapper(uid, "u", "ua", False, 1, 30, 7, channel_name="x"))
        self.assertEqual(self.seen, [(uid, 30, 7)])

    def test_fails_open_when_the_lookup_is_unavailable(self):
        # Off-server the Django import fails: the call must still go through,
        # with the caller's own arguments.
        self.assertTrue(self.wrapper("ab" * 32, "u", "ua", False, 1, None, None))
        self.assertEqual(self.seen, [("ab" * 32, None, None)])


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
