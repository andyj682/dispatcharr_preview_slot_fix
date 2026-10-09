# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-10-08

Required for Dispatcharr 0.32.0. One build supports both 0.31.0 and 0.32.0.

### Fixed

- **Installs on Dispatcharr 0.32.0.** 1.0.0 refuses to install there (one
  `NOT patched` error at startup; **Check status** shows the `Stream` patches as
  not patched), so Dispatcharr runs its stock preview code, and that code still
  has the bug (reported upstream as Dispatcharr#1773).
- On Dispatcharr 0.32.0, 1.0.0 is left half-installed: its preview-metadata
  patch still loads, and it blanks the stream and provider that stock previews
  record. Stream stats lose both, and core's backup release has nothing left to
  release. 1.1.0 installs all of its patches or none, and the metadata patch
  does nothing unless the other two are active.

### Changed

- Dispatcharr 0.32.0 added profile-scoped previews, which keep their own
  records and don't have this bug. The plugin leaves them to stock Dispatcharr.
  It still fixes default previews, the only kind the web UI starts.
- Calls are routed by the *value* of 0.32.0's new preview parameter, not by its
  presence: core passes it on every call, empty for a default preview.
- A Dispatcharr release with signatures other than 0.31.0's or 0.32.0's is still
  refused at install, as before.

### Notes

- Verified against Dispatcharr 0.32.0; still supports 0.31.0.
- Dispatcharr 0.32.0 fixed the double release itself, using the same method as
  this plugin. Both run, harmlessly; the plugin's copy is what protects 0.31.0.
- Dispatcharr 0.32.0 can also give up on releasing a slot under heavy contention
  and log `Gave up releasing connection slots for profile N`. A provider that
  stays fuller than it should after that line is not this bug.

## [1.0.0] - 2026-10-06

First release.

### Fixed

- Previewing a stream from the Streams view no longer frees the provider
  connection of a channel that started on that stream and is still playing.
  Previews keep their own connection record instead of sharing one with live
  channels.
- A preview of a channel's starting stream no longer picks up the provider the
  channel failed over to; previews only use their own stream's provider.
- Previewing a stream no longer overwrites the record of a channel that happens
  to share its numeric ID.
- A finished preview is released once, not twice. Under Dispatcharr's default
  channel shutdown delay of 0 the second release could free another channel's
  slot on a multi-connection provider.

### Added

- **Check status** action: whether the fix is active, and the preview
  connections the plugin currently holds.

### Notes

- Verified against Dispatcharr 0.31.0.
- Mirrors the approach upstream development code already takes for its newer
  profile-scoped previews. Calls in that newer form are passed to stock
  Dispatcharr unchanged.
