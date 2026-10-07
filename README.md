# Dispatcharr Preview Slot Fix

Stops a stream preview from freeing the provider connection of a channel that is
still playing.

## The problem it solves

When a channel starts, Dispatcharr reserves one of the provider's connection
slots and records it under the stream the channel started on. Previews from the
**Streams** view keep their own record under the **same** key name. So if you
preview the stream a playing channel started on, the preview takes over the
channel's record, and when you close the preview, Dispatcharr releases the
channel's slot — while the channel is still playing.

From then on, Dispatcharr believes that provider has a free slot when it
doesn't. The next channel that starts, or fails over, can be sent to it, and the
provider rejects or drops one of the two connections. The playing channel has
also lost its own record, so its later failovers can't update the slot count.

It tends to happen at the worst moment. A channel misbehaves, you open the
Streams view to preview its streams and see which ones work, and the first one
you try is the one it started on.

Two related problems on the same code path are fixed too:

- **Previewing a stream can overwrite a channel's record.** Previews also write
  a key named after the stream's ID into the space where channels' records are
  named after the channel's ID. When a stream and a channel happen to share the
  same number, the preview overwrites that channel's record.
- **A finished preview can be released twice.** With Dispatcharr's default
  channel shutdown delay of 0, a preview's slot is released when its last viewer
  leaves, and then released again during teardown. On a provider with room for
  two or more connections, the second release frees someone else's slot.

## What it does

Previews get their own connection record, separate from live channels':

- A preview never reads, reuses or deletes a channel's record, so closing one
  can't free a channel's slot.
- A preview reserves a slot from its **own** provider only. Before, a preview of
  a channel's starting stream could pick up whichever provider the channel had
  failed over to.
- Each preview is released exactly once.
- A preview left behind by a crash or restart is recognized as a leftover and
  cleaned up the next time that stream is previewed, the same way current
  Dispatcharr development code handles its newer preview mode.

Live channels, recordings and failover are untouched; the plugin only changes
how previews keep their records.

**One visible change:** a preview of a stream whose provider is already at its
connection limit now won't play. The preview player shows no error, it just
doesn't start (behind the scenes Dispatcharr refuses it with "All active M3U
profiles have reached maximum connection limits"). Without the plugin that
preview *appeared* to work, because it borrowed a playing channel's slot: in
reality it opened a second connection the provider doesn't allow, and closing
it freed the channel's slot. Close something using that provider, or preview a
different stream, and it plays normally.

## Install

1. Download `dispatcharr_preview_slot_fix.zip` from the
   [releases page](https://github.com/andyj682/dispatcharr_preview_slot_fix/releases).
2. In Dispatcharr, go to **Plugins** and import the zip.
3. Enable it, then **restart the Dispatcharr container**.

Restart with no previews open. A preview that is open at the moment the plugin
is enabled or disabled is still released correctly, but it can leave a stale key
behind that only matters if the plugin is later removed.

## Check status

**Check status** shows whether the fix is active and lists the preview
connections the plugin is holding right now. Any marked *leftover* belong to a
preview that ended without releasing; the next preview of that stream cleans
them up.

## Requirements and limits

- Verified against **Dispatcharr 0.31.0**.
- If a future Dispatcharr release changes the methods this plugin wraps, it
  stops applying the fix to those calls and passes them to stock Dispatcharr
  unchanged, logging a warning so the change is easy to spot. Your previews keep
  working; you just lose the protection until the plugin is updated.
- This fixes the preview problem only. Two smaller problems in live-channel
  failover are out of scope: two channels failing over
  at the same moment can both take a provider's last slot, and a failover whose
  slot update fails still completes.
- Upstream is reworking this area. Once a Dispatcharr release fixes it, this
  plugin becomes unnecessary and can be removed.

## Uninstall

Disable or delete the plugin and restart. Previews go back to Dispatcharr's
built-in behavior, bug included.

## License

MIT — see [LICENSE](LICENSE).
