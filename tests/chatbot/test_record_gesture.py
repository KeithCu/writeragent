"""Press / hold / release decisions for hands-free Record."""

import inspect

from plugin.chatbot.record_gesture import (
    HANDS_FREE_STATUS,
    RecordGesture,
    StickyRestart,
    exit_sticky,
    gesture_action,
    gesture_hold_elapsed,
    gesture_press,
    gesture_release,
    hands_free_status_text,
    sticky_restart,
)


def _run(events: list[str], *, start_sticky: bool = False) -> tuple[RecordGesture, int]:
    gesture = RecordGesture(sticky=start_sticky)
    records = 0
    for event in events:
        if event == "press":
            step = gesture_press(gesture, label_is_record=True)
        elif event == "press-other":
            step = gesture_press(gesture, label_is_record=False)
        elif event == "release":
            step = gesture_release(gesture)
        elif event == "hold":
            step = gesture_hold_elapsed(gesture)
        elif event == "action":
            step = gesture_action(gesture)
        elif event == "exit":
            gesture = exit_sticky(gesture)
            continue
        else:
            raise AssertionError(event)
        gesture = step.gesture
        if step.dispatch_record:
            records += 1
    return gesture, records


def test_short_press_records_once_and_swallows_action() -> None:
    gesture, records = _run(["press", "release", "action"])
    assert records == 1
    assert gesture.sticky is False
    assert gesture.suppress_action is False
    assert gesture.holding is False


def test_action_before_release_records_once() -> None:
    gesture, records = _run(["press", "action", "release"])
    assert records == 1
    assert gesture.sticky is False
    assert gesture.suppress_action is False


def test_long_press_sets_sticky_and_records_once() -> None:
    gesture, records = _run(["press", "hold", "release", "action"])
    assert records == 1
    assert gesture.sticky is True
    assert gesture.holding is False
    assert gesture.suppress_action is False


def test_long_press_action_before_timer_records_once() -> None:
    gesture, records = _run(["press", "action", "hold", "release"])
    assert records == 1
    assert gesture.sticky is True


def test_second_hold_does_not_dispatch_again() -> None:
    _gesture, records = _run(["press", "hold", "hold", "release", "action"])
    assert records == 1


def test_keyboard_action_does_not_dispatch_from_the_helper() -> None:
    """No mouse press: ActionEvent is the caller's Record / Stop Rec / Send path."""
    gesture, records = _run(["action"])
    assert records == 0
    assert gesture.sticky is False
    step = gesture_action(RecordGesture())
    assert step.swallowed_action is False


def test_release_without_press_is_noop() -> None:
    gesture, records = _run(["release"])
    assert records == 0
    assert gesture == RecordGesture()


def test_press_on_stop_rec_does_not_start_a_hold() -> None:
    step = gesture_press(RecordGesture(sticky=True), label_is_record=False)
    assert step.start_timer is False
    assert step.dispatch_record is False
    assert step.gesture.sticky is True
    assert step.gesture.holding is False


def test_exit_clears_sticky_and_swallows_in_flight_hold() -> None:
    gesture, records = _run(["press", "exit", "release", "action"])
    assert records == 0
    assert gesture.sticky is False


def test_exit_while_idle_sticky_does_not_swallow_the_next_action() -> None:
    gesture = exit_sticky(RecordGesture(sticky=True))
    assert gesture.sticky is False
    assert gesture.suppress_action is False
    step = gesture_action(gesture)
    assert step.swallowed_action is False


def test_sticky_restart_matrix() -> None:
    assert sticky_restart(sticky=False, speaking=False) is StickyRestart.NONE
    assert sticky_restart(sticky=False, speaking=True) is StickyRestart.NONE
    assert sticky_restart(sticky=True, speaking=False) is StickyRestart.RECORD
    assert sticky_restart(sticky=True, speaking=True) is StickyRestart.WAIT_FOR_TTS


def test_hands_free_status_msgid() -> None:
    assert HANDS_FREE_STATUS in inspect.getsource(hands_free_status_text)
    assert hands_free_status_text() == HANDS_FREE_STATUS
