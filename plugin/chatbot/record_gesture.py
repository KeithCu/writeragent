# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Press / hold / release decisions for hands-free sidebar Record.

The Send button is one control whose label is Record, Stop Rec, or Send.
A normal click is mousePressed, mouseReleased, then ActionEvent. Hands-free
mode is a ~2s hold. The mouse path must dispatch ``RECORD_CLICKED`` itself:
if it waits for ActionEvent, the label has already flipped to Stop Rec and
that event sends immediately.

Sticky lives here, not in ``send_state.next_state``. The FSM stays free of
timers. Silence auto-stop is still ``STOP_REC_CLICKED``; this module only
decides when the panel should set or clear the flag and when to arm Record
again after the reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

# Hold Record this long to arm hands-free. A shorter release is a normal click.
RECORD_HOLD_MS = 2000

# UI-thread poll gap while TTS is still speaking. Not a silence threshold.
STICKY_TTS_POLL_MS = 200

# English msgid. Panel passes it through gettext. Matches the literal below.
HANDS_FREE_STATUS = "Hands-free recording..."

# UpdateUIEffect status from RECORD_CLICKED in send_state.next_state.
RECORDING_STATUS = "Recording audio..."


def hands_free_status_text() -> str:
    """Sidebar status while a sticky take is capturing."""
    from plugin.framework.i18n import _

    return _("Hands-free recording...")


class StickyRestart(Enum):
    """What to do with the mic after a turn finishes."""

    NONE = auto()
    RECORD = auto()
    WAIT_FOR_TTS = auto()


@dataclass(frozen=True)
class RecordGesture:
    """One Send-button press, plus the sticky flag that outlives it.

    ``holding`` is true from mousePressed on Record until mouseReleased.
    ``fired`` means this press already dispatched ``RECORD_CLICKED`` (the
    hold timer). ``action_seen`` means ActionEvent arrived while the button
    was still down, so release must not expect a second one.
    ``suppress_action`` eats the ActionEvent that follows a mouse dispatch,
    including when the label is already Stop Rec.
    """

    holding: bool = False
    fired: bool = False
    action_seen: bool = False
    suppress_action: bool = False
    sticky: bool = False


@dataclass(frozen=True)
class GestureStep:
    gesture: RecordGesture
    dispatch_record: bool = False
    start_timer: bool = False
    cancel_timer: bool = False
    swallowed_action: bool = False


def gesture_press(gesture: RecordGesture, *, label_is_record: bool) -> GestureStep:
    """mousePressed. Starts the hold timer only when the label is Record.

    A new press drops a stale ``suppress_action``. Otherwise a lost
    ActionEvent from the previous click would swallow Stop Rec.
    """
    if gesture.holding:
        return GestureStep(gesture)
    if not label_is_record:
        return GestureStep(RecordGesture(sticky=gesture.sticky))
    return GestureStep(
        RecordGesture(holding=True, sticky=gesture.sticky),
        start_timer=True,
    )


def gesture_hold_elapsed(gesture: RecordGesture) -> GestureStep:
    """Hold timer fired. Arm sticky and start Record once, if still held."""
    if not gesture.holding or gesture.fired:
        return GestureStep(gesture)
    return GestureStep(
        RecordGesture(
            holding=True,
            fired=True,
            action_seen=gesture.action_seen,
            suppress_action=not gesture.action_seen,
            sticky=True,
        ),
        dispatch_record=True,
    )


def gesture_release(gesture: RecordGesture) -> GestureStep:
    """mouseReleased. Short press records once; long press does not record again."""
    if not gesture.holding:
        return GestureStep(gesture)
    dispatch = not gesture.fired
    if dispatch:
        suppress = not gesture.action_seen
    elif gesture.action_seen:
        suppress = False
    else:
        suppress = gesture.suppress_action
    return GestureStep(
        RecordGesture(suppress_action=suppress, sticky=gesture.sticky),
        dispatch_record=dispatch,
        cancel_timer=True,
    )


def gesture_action(gesture: RecordGesture) -> GestureStep:
    """ActionEvent on the Send button, whatever the label says now.

    Swallow when this click already dispatched, or when the button is still
    down (release or the hold timer will dispatch). Keyboard activation has
    neither flag, so the caller handles Record / Stop Rec / Send as before.
    """
    if gesture.suppress_action:
        return GestureStep(
            RecordGesture(sticky=gesture.sticky),
            swallowed_action=True,
        )
    if gesture.holding:
        return GestureStep(
            RecordGesture(
                holding=True,
                fired=gesture.fired,
                action_seen=True,
                suppress_action=gesture.suppress_action,
                sticky=gesture.sticky,
            ),
            swallowed_action=True,
        )
    return GestureStep(gesture)


def exit_sticky(gesture: RecordGesture) -> RecordGesture:
    """Leave hands-free. Swallow an in-flight Record click so it cannot re-arm.

    Stop, Clear, cancel, and an aborting error use this. Stop Rec does not:
    that send should still restart Record when the reply finishes.
    """
    swallow = gesture.holding or gesture.fired or gesture.suppress_action or gesture.action_seen
    return RecordGesture(suppress_action=swallow)


def sticky_restart(*, sticky: bool, speaking: bool) -> StickyRestart:
    """After send completes: record now, wait out TTS, or do nothing."""
    if not sticky:
        return StickyRestart.NONE
    if speaking:
        return StickyRestart.WAIT_FOR_TTS
    return StickyRestart.RECORD
