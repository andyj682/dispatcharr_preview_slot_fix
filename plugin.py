"""
Dispatcharr Preview Slot Fix
============================

Previewing a stream from the Streams view can free the provider connection of a
channel that is still playing: the preview shares a Redis key with the channel
that started on that stream, borrows the channel's reservation, and releases it
when the preview closes. The provider then reads as free while it is in use, and
the next channel that needs it is assigned a connection the provider refuses.

This plugin gives previews their own bookkeeping, the same approach upstream
Dispatcharr already takes for its newer profile-scoped previews. See patch.py
for the full design.

Author: andyj682
License: MIT
"""

import logging

logger = logging.getLogger("plugins.dispatcharr_preview_slot_fix")

try:
    from . import patch as _patch
except Exception:  # pragma: no cover - fall back to flat import layout
    import patch as _patch

try:
    # Import-time: apply the patches in every worker that loads plugins.
    _patch.install()
except Exception:  # never break app startup because of the plugin
    logger.exception("[PREVIEW-SLOT-FIX] auto-install on import failed")


def _format_retired(reservations):
    lines = [
        "NOT NEEDED on this Dispatcharr version: it fixes the preview bug itself "
        "(Dispatcharr#1773, fixed in 0.33.0). Nothing is patched. Disable and "
        "remove this plugin."
    ]
    if reservations:
        lines.append(
            f"Leftover preview connections from an older Dispatcharr: {len(reservations)}. "
            "They still count against their provider until Redis is cleared "
            "(recreating the Dispatcharr container does this)."
        )
    return "\n".join(lines)


def _format_status(state, reservations):
    lines = []
    all_on = all(state.values())
    lines.append("Fix: ACTIVE" if all_on else "Fix: NOT FULLY ACTIVE (see logs)")
    for target, on in state.items():
        lines.append(f"  {target}: {'patched' if on else 'NOT patched'}")
    if reservations:
        lines.append(f"Preview connections held: {len(reservations)}")
        for stream_id, profile_id, live in reservations:
            lines.append(
                f"  stream {stream_id} on profile {profile_id}"
                + ("" if live else " (leftover - released on next preview)")
            )
    else:
        lines.append("Preview connections held: none")
    return all_on, "\n".join(lines)


class Plugin:
    # UI title only. README / repo / zip keep the fuller "Dispatcharr Preview
    # Slot Fix" name; "Dispatcharr" is redundant inside the Dispatcharr UI.
    name = "Preview Slot Fix"
    version = "1.2.0"
    description = (
        "Stops a stream preview from freeing the provider connection of a "
        "channel that is still playing (Dispatcharr 0.31.0 and 0.32.0). Not "
        "needed from Dispatcharr 0.33.0, which fixes this itself: there the "
        "plugin does nothing and can be removed."
    )
    author = "andyj682"
    help_url = "https://github.com/andyj682/dispatcharr_preview_slot_fix"

    fields = [
        {
            "id": "_info",
            "label": "",
            "type": "info",
            "description": (
                "No settings needed. Enable the plugin, then restart the "
                "Dispatcharr container. Previews keep working as before; they "
                "just keep their own connection record instead of sharing one "
                "with live channels. Dispatcharr 0.33.0 and later fix this "
                "themselves; on those versions the plugin installs nothing and "
                "can be removed."
            ),
        },
    ]

    actions = [
        {
            "id": "status",
            "label": "Show status",
            "description": (
                "Report whether the fix is active and list the preview "
                "connections it is currently holding."
            ),
            "button_label": "Check status",
            "button_variant": "outline",
        },
    ]

    def run(self, action=None, params=None, context=None):
        if action == "status":
            try:
                retired = _patch.dispatcharr_fixes_it()
                state = _patch.patch_state()
                reservations = _patch.active_preview_reservations()
            except Exception as exc:
                logger.exception("[PREVIEW-SLOT-FIX] status failed")
                return {"status": "error", "message": f"Status failed: {exc}"}
            if retired:
                return {"status": "ok", "message": _format_retired(reservations)}
            ok, message = _format_status(state, reservations)
            return {"status": "ok" if ok else "error", "message": message}

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context=None):
        """Called by Dispatcharr on disable / delete / reload."""
        _patch.uninstall()
        return {"status": "ok", "message": "Preview slot fix reverted"}
