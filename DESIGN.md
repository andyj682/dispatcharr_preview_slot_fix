# Design notes

Why this plugin is built the way it is. Written against Dispatcharr **0.31.0**,
updated for **0.32.0** (plugin 1.1.0). The bug is reported upstream as
[Dispatcharr#1773](https://github.com/Dispatcharr/Dispatcharr/issues/1773).

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

### What 0.32.0 changed, and didn't

0.32.0 added **profile-scoped** previews: `get_stream(preferred_profile_id=N)`,
`release_stream(m3u_profile_id=N)`, worker id `{stream_hash}.p{N}`, keyed
`stream_profile:{id}:p{N}`. Those don't collide. But the **default** preview —
the parameter left at `None`, a bare stream-hash worker id — still reads and
deletes the bare `stream_profile:{id}` and still writes `channel_stream:{id}`.
It's the only kind the web UI starts: both preview buttons build
`/proxy/ts/stream/{stream_hash}`, and nothing in the frontend builds a `.p{N}`
id. So the collision is unchanged for UI users.

Core now passes the new keyword on **every** call, as `None` for a default
preview: `generate_stream_url()` calls `get_stream(preferred_profile_id=None)`,
and `release_worker_stream()` calls `release_stream(m3u_profile_id=None)`.

## The double release

`ChannelService.initialize_channel()` records the preview's profile in its
channel metadata. Under the default `channel_shutdown_delay` of 0 the output
generator calls `release_stream()` when the last viewer leaves; teardown then
calls `Stream.release_stream()` again, finds the key gone, and falls back to
`_release_profile_slot_from_redis_metadata()`, releasing the same provider a
second time. `Channel.release_stream()` clears those metadata fields to prevent
exactly this, and upstream `dev`'s newer profile-scoped preview release does too
(*"Clear worker metadata so stop-chain metadata fallback does not DECR again"*).
The default preview path never got the same treatment in 0.31.0.

**0.32.0 fixed it upstream**: stock `release_stream()` now clears the same two
fields before releasing, the same mechanism this plugin uses. The plugin keeps
its own clear deliberately. It is what protects 0.31.0, and on 0.32.0 `hdel` of
already-cleared fields is a no-op, so nothing is released twice.

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
   Two cases pass through untouched: a profile-scoped worker id
   (`{stream_hash}.p{N}`, whose values stock already reads correctly from the id),
   and **any** call while the plugin's `Stream` pair isn't installed. Without
   the pair, stock reserved the preview, the plugin's key doesn't exist, and
   "replacing" stock's correct values would erase them. That was 1.0.0's
   half-installed state on 0.32.0: no stream or provider in stats, and core's
   metadata fallback disarmed. `install()` now also refuses to install this
   wrapper unless the pair is installed.

All three targets are class attributes, resolved at call time, so no module
rebinding is needed. All three run only in the uWSGI workers serving the live
proxy; Celery is not involved.

## Every path that touches a preview's slot

0.32.0 routes every preview release through `url_utils.release_worker_stream()`,
which for a stream-hash worker calls `Stream.release_stream(m3u_profile_id=…)`.
`ProxyServer._release_stream_resources()` calls it and then falls back to the
metadata. So the order the release-once guarantee depends on still holds:
the `Stream` release first, then the metadata fallback.

| path (0.31.0 / 0.32.0) | under the plugin |
|---|---|
| `generate_stream_url()` → `get_stream()` / `get_stream(preferred_profile_id=None)` | own key; own provider only |
| `generate_stream_url()` error paths → `release_stream()` / `release_stream(m3u_profile_id=None)` if `slot_reserved` | `slot_reserved` is True only for a fresh reservation, so a reused one is never released by a failing joiner |
| `stream_ts` error branches → `release_worker_stream()` if `connection_allocated` (0.32.0) | the plugin's release, as above |
| generator `_cleanup` (shutdown delay 0) → `release_stream()` / `release_worker_stream()` | releases once, clears metadata |
| teardown `_release_stream_resources()` → `release_stream()` / `release_worker_stream()` | `False` after the generator released, then the fallback finds cleared metadata: no second release |
| teardown metadata fallback | only reached for a preview with no key of ours, using that preview's own metadata |
| profile-scoped preview, any of the above with `.p{N}` / a non-`None` profile (0.32.0) | stock, untouched (all three wrappers) |
| plugin disabled mid-preview | stock `release_stream()` finds no shared key, falls back to the accurate metadata: released once. Exception: see Known edges |

## Signature drift

`install()` accepts exactly two shapes per method and refuses anything else:

| | 0.31.0 | 0.32.0 |
|---|---|---|
| `get_stream` | `(self, requester)` | `(self, requester, preferred_profile_id)` |
| `release_stream` | `(self)` | `(self, m3u_profile_id)` |

It patches the two `Stream` methods only as a pair (a patched `get_stream` with
a stock `release_stream` would strand reservations), and `initialize_channel`
only together with the pair.

At call time the wrappers bind the arguments against the live original's
signature, so keyword and positional calls route the same, then route by
**value**:

- the profile-scoped parameter is not `None` → stock, unchanged (collision-free);
- `requester` → ignored, as stock's stream path ignores it;
- anything else → stock, unchanged, with one warning per process.

Routing by value, not presence, is essential. Core passes the scoped keyword on
every 0.32.0 call, so "unknown keyword → stock" (1.0.0's rule) would install
cleanly, report the fix active, and fix nothing.

## Known edges

- Disabling the plugin while a preview of a running channel's **starting**
  stream is open: stock `release_stream()` then reads the bare
  `stream_profile:{id}` key, which is the channel's. That's the original bug,
  once, for that preview. Same on 0.31.0 and 0.32.0. Hence "restart with no
  previews open."

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
leftovers, the concurrent-start race, profile order, initialize pass-through,
fail-open and gating, and manifest/class parity. The wrappers run against
stand-ins with the **real** 0.31.0 and 0.32.0 signatures and call shapes
(keyword `None`, positional, scoped, unknown). `install()` runs against stand-in
Django modules for both shapes, an unknown shape, and a mixed pair. The guards
were mutation-checked. Each of these fails tests: reintroducing the shared key;
skipping the metadata clear; always treating a preview as live; claiming the key
without NX; disabling the drift sensor; routing by presence instead of value;
sending scoped calls to the plugin; treating an unknown parameter as the plugin
path; removing the initialize gate; not passing scoped worker ids through;
installing initialize without the pair; accepting only 0.31.0's shape;
accepting any shape; and not reverting a half-installed pair.

On a Dispatcharr instance, the acceptance tests in the Dispatcharr clone
(`apps/proxy/live_proxy/tests/test_preview_slot_fix_acceptance.py`) re-run the
seven collision scenarios against real core with the patches installed, plus the
double release, disable-mid-preview and clean-uninstall guarantees. Previews are
driven with core's own call shape for the running release. On 0.32.0 they add
core's real `release_worker_stream()` teardown and the profile-scoped
pass-through.
