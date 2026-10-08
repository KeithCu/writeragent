"""Press / hold / release decisions for hands-free Record."""

import inspect

from plugin.chatbot.record_gesture import (
    EMPTY_TAKES_EXIT,
    HANDS_FREE_STATUS,
    RecordGesture,
    StickyRestart,
    TakeStop,
    exit_sticky,
    gesture_action,
    gesture_hold_elapsed,
    gesture_press,
    gesture_release,
    hands_free_silence_text,
    hands_free_status_text,
    stop_during_take,
    sticky_restart,
)
import pytest


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


@pytest.mark.parametrize(
    "value, value_2, value_3, expected, expected_2",
    [
        pytest.param("action", "hold", "release", 1, True, id="test_long_press_action_before_timer_records_once"),
        pytest.param("exit", "release", "action", 0, False, id="test_exit_clears_sticky_and_swallows_in_flight_hold"),
    ],
)
def test_long_press_action_before_timer_records_once(value, value_2, value_3, expected, expected_2) -> None:
    gesture, records = _run(["press", value, value_2, value_3])
    assert records == expected
    assert gesture.sticky is expected_2


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


def test_stop_during_take_steps_down_one_level() -> None:
    """Locked take: Stop drops the lock. One-shot take: Stop cancels."""
    assert stop_during_take(sticky=True) is TakeStop.EXIT_LOCK
    assert stop_during_take(sticky=False) is TakeStop.CANCEL


def test_exit_lock_keeps_the_next_stop_rec_click() -> None:
    """Stop on a settled locked take must not swallow the Stop Rec that sends it."""
    gesture, _records = _run(["press", "hold", "release", "action", "exit"])
    assert gesture.sticky is False
    assert gesture.suppress_action is False


def test_hands_free_silence_text_keeps_mode_visible() -> None:
    text = hands_free_silence_text(750)
    assert text.startswith("Hands-free")
    assert "750" in text
    assert "_(" in inspect.getsource(hands_free_silence_text)


def test_empty_takes_exit_after_two() -> None:
    assert EMPTY_TAKES_EXIT == 2
