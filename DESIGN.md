# Design notes

Why this plugin is built the way it is. Written against Dispatcharr **0.31.0**,
cross-checked against upstream `dev`.

## The collision

Channels record a provider assignment as `channel_stream:{channel_id}` and
`stream_profile:{stream_id}`, where `stream_id` is the stream the channel
**started** on. `channel_stream` is never updated on failover or a manual
switch; `Channel.update_stream_profile()` rewrites the value under the starting
stream's key instead. So a channel's profile lives under its starting stream for
as long as the channel runs.

Direct stream previews (`Stream.get_stream()` / `Stream.release_stream()` in
`apps/channels/models.py`) use the same `stream_profile:{stream_id}` key. If it
exists, `get_stream()` returns it as the preview's own reservation without
reserving anything, and `release_stream()` deletes it and releases the profile
it names. Preview the stream a running channel started on, and the preview's
teardown frees the channel's slot. The provider reads as free while in use, and
the channel's next failover hits `update_stream_profile()`'s "no profile found"
exit, so its counter can no longer move.

`get_stream()` also writes `channel_stream:{stream.id}` — a stream id in a
namespace keyed by channel id — so previewing stream N overwrites channel N's
record.

## The double release

`ChannelService.initialize_channel()` records the preview's profile in its
channel metadata. Under the default `channel_shutdown_delay` of 0 the output
generator calls `release_stream()` when the last viewer leaves; teardown then
calls `Stream.release_stream()` again, finds the key gone, and falls back to
`_release_profile_slot_from_redis_metadata()`, releasing the same provider a
second time. `Channel.release_stream()` clears those metadata fields to prevent
exactly this, and upstream `dev`'s newer profile-scoped preview release does too
(*"Clear worker metadata so stop-chain metadata fallback does not DECR again"*).
The default preview path never got the same treatment.

## The fix

Do for the default preview what `dev` already does for profile-scoped previews.

1. **`Stream.get_stream`** keeps the preview's reservation under its own key,
   `preview_slot_fix:stream_profile:{stream_id}`, outside both shared
   namespaces, and never writes `stream_profile:{id}` or `channel_stream:{id}`.
   Reservation order matches core: default profile first, inactive profiles
   skipped, the slot taken with core's own atomic `reserve_profile_slot()`. The
   key is claimed with `SET NX`; if a concurrent request for the same preview
   claimed it first, our reservation is returned and theirs is used.
2. **Reuse only while live.** An existing preview key is reused only if the
   preview's metadata is absent (the gap between reserve and initialize) or in a
   live state — the rule in `dev`'s `_scoped_preview_assignment_is_reusable()`.
   Otherwise it's a leftover: whoever deletes the key releases its slot once,
   then a fresh reservation is taken. Releasing a leftover is correct because the
   key and the counter live in the same Redis, which in the all-in-one container
   persists across a plain container restart: they survive or vanish together.
3. **`Stream.release_stream`** releases only the plugin's key, clears the
   preview's `STREAM_ID` / `M3U_PROFILE` metadata, and releases the slot only if
   it was the caller that deleted the key. If another path deleted it first, it
   reports the release as handled, so nobody falls back to the metadata and
   releases again. If there was no key at all, it returns `False` — and does
   **not** run stock code, which would read the shared key (possibly a running
   channel's). Core's teardown then uses the preview's own metadata, which covers
   a preview that started before the plugin loaded.
4. **`ChannelService.initialize_channel`**. For a preview, the stream request
   handler reads the preview's stream and profile back from the shared keys,
   inline in `stream_ts` where a plugin can't reach. Under the plugin those keys
   are absent; in the collision case they belong to a channel. The wrapper
   recognizes a preview (addressed by stream hash, not channel UUID) and replaces
   both values with the preview's own, so its metadata is accurate. That keeps
   stats correct, and it means a preview reserved under the plugin still releases
   through stock code's metadata fallback if the plugin is disabled mid-preview.

All three targets are class attributes, resolved at call time, so no module
rebinding is needed. All three run only in the uWSGI workers serving the live
proxy; Celery is not involved.

## Every path that touches a preview's slot

| path | under the plugin |
|---|---|
| `generate_stream_url()` → `stream.get_stream()` | own key; own provider only |
| `generate_stream_url()` error paths → `release_stream()` if `slot_reserved` | `slot_reserved` is True only for a fresh reservation, so a reused one is never released by a failing joiner |
| generator `_cleanup` (shutdown delay 0) → `release_stream()` | releases once, clears metadata |
| teardown `_release_stream_resources()` → `release_stream()` | `False` after the generator released, then the fallback finds cleared metadata: no second release |
| teardown metadata fallback | only reached for a preview with no key of ours, using that preview's own metadata |
| plugin disabled mid-preview | stock `release_stream()` finds no shared key, falls back to the accurate metadata: released once |

## Signature drift

The wrappers accept `*args, **kwargs`. Anything beyond the v0.31.0 parameters
(`requester` for `get_stream`, nothing for `release_stream`) is handed to stock
code unchanged, with one warning per process. `dev` already adds
`preferred_profile_id` / `m3u_profile_id` for profile-scoped previews, which
don't collide — stock code is correct for those calls. `install()` also checks
the signatures up front, and patches the two `Stream` methods only as a pair: a
patched `get_stream` with a stock `release_stream` would strand reservations.

## Known edges

- Enabling or disabling without a restart, while a stock preview is open, can
  leave a stale shared `stream_profile:{id}` key. Harmless while the plugin is
  installed (it never reads that key); if the plugin is later removed, a stock
  preview of that stream could adopt it. Hence "restart with no previews open."
- Stale `channel_stream:{stream_id}` keys written by stock previews before the
  plugin was installed are left alone. Stock never deleted them either, and core
  already treats them as stale when the matching channel next starts.

## Testing

`test_logic.py` runs off-server with fakes that follow core's
`reserve_profile_slot()` arithmetic:

```
python test_logic.py
```

It covers the collision, failover-then-preview, the double release, liveness and
leftovers, the concurrent-start race, profile order, signature drift, the
initialize wrapper's pass-through and fail-open behavior, and manifest/class
parity. The guards were mutation-checked: reintroducing the shared key, skipping
the metadata clear, always treating a preview as live, claiming the key without
NX, and disabling the drift sensor each fail tests.

On a Dispatcharr instance, the acceptance tests in the Dispatcharr clone
(`apps/proxy/live_proxy/tests/test_preview_slot_fix_acceptance.py`) re-run the
seven collision scenarios against real core with the patches installed, plus the
double release, disable-mid-preview and clean-uninstall guarantees.
