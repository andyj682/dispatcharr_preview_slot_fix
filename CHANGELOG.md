# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
