# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Writer track-changes tools.

Ported from nelson-mcp (MPL 2.0): nelson-mcp/plugin/modules/writer/tools/tracking.py
(accept/reject combined here as manage_tracked_changes).
"""

import logging

from plugin.modules.writer.base import WriterAgentSpecialTracking

log = logging.getLogger("writeragent.writer")


class SetTrackChanges(WriterAgentSpecialTracking):
    """Enable or disable change tracking."""

    name = "set_track_changes"
    description = "Enable or disable track changes (change recording) in the document."
    parameters = {
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "description": "True to enable track changes, False to disable.",
            },
        },
        "required": ["enabled"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        enabled = kwargs.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.lower() not in ("false", "0", "no")
        ctx.doc.setPropertyValue("RecordChanges", bool(enabled))
        return {"status": "ok", "record_changes": bool(enabled)}


class GetTrackedChanges(WriterAgentSpecialTracking):
    """List all tracked changes (redlines) in the document."""

    name = "get_tracked_changes"
    description = (
        "List all tracked changes (redlines) in the document, "
        "including type, author, date, and comment."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        recording = False
        try:
            recording = doc.getPropertyValue("RecordChanges")
        except Exception:
            pass

        if not hasattr(doc, "getRedlines"):
            return {
                "status": "ok",
                "recording": recording,
                "changes": [],
                "count": 0,
                "message": "Document does not expose redlines API.",
            }

        redlines = doc.getRedlines()
        enum = redlines.createEnumeration()
        changes = []
        while enum.hasMoreElements():
            redline = enum.nextElement()
            entry = {}
            for prop in (
                "RedlineType", "RedlineAuthor",
                "RedlineComment", "RedlineIdentifier",
            ):
                try:
                    entry[prop] = redline.getPropertyValue(prop)
                except Exception:
                    pass
            try:
                dt = redline.getPropertyValue("RedlineDateTime")
                entry["date"] = "%04d-%02d-%02d %02d:%02d" % (
                    dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes
                )
            except Exception:
                pass
            changes.append(entry)

        return {
            "status": "ok",
            "recording": recording,
            "changes": changes,
            "count": len(changes),
        }


class ManageTrackedChanges(WriterAgentSpecialTracking):
    """Accept or reject all tracked changes in the document."""

    name = "manage_tracked_changes"
    description = "Accept or reject all tracked changes in the document."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["accept_all", "reject_all"],
                "description": "Action to perform: 'accept_all' or 'reject_all'.",
            },
        },
        "required": ["action"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        action = kwargs.get("action")
        if action not in ("accept_all", "reject_all"):
            return self._tool_error("Invalid action: %s" % action)

        smgr = ctx.ctx.ServiceManager
        dispatcher = smgr.createInstanceWithContext(
            "com.sun.star.frame.DispatchHelper", ctx.ctx
        )
        frame = ctx.doc.getCurrentController().getFrame()
        
        uno_cmd = ".uno:AcceptAllTrackedChanges" if action == "accept_all" else ".uno:RejectAllTrackedChanges"
        dispatcher.executeDispatch(frame, uno_cmd, "", 0, ())
        
        msg = "All tracked changes accepted." if action == "accept_all" else "All tracked changes rejected."
        return {"status": "ok", "message": msg}
