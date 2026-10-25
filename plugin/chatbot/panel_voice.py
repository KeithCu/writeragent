from __future__ import annotations

import logging
from typing import Any

from plugin.chatbot.record_gesture import (
    RECORD_HOLD_MS,
    STICKY_TTS_POLL_MS,
    StickyRestart,
    TakeStop,
    exit_sticky,
    gesture_hold_elapsed,
    hands_free_silence_text,
    sticky_restart,
    stop_during_take as plan_take_stop,
)
from plugin.chatbot.send_state import SendEvent, SendEventKind

log = logging.getLogger("plugin.chatbot.panel")


def _stop_tts() -> None:
    """Stop active speech synthesis, logging errors at debug level."""
    try:
        from plugin.audio.tts_service import stop_speech

        stop_speech()
    except Exception:
        log.debug("stop_speech failed", exc_info=True)


class PanelVoiceController:
    """Voice controller for STT, TTS, and recording gestures."""

    _panel: Any

    def __init__(self, panel: Any) -> None:
        self._panel = panel

    def clear_pending_audio_wav(self) -> None:
        """Clear and delete any un-sent audio recording."""
        if self._panel.audio_wav_path:
            import os

            try:
                os.remove(self._panel.audio_wav_path)
            except OSError as exc:
                # What was wrong: except Exception: pass hid a failed delete.
                # How it happened: on Windows the recorder subprocess still
                # holds the WAV, so os.remove raises PermissionError.
                # Why this fixes it: OSError is logged and the path is still
                # dropped. Any other exception propagates.
                log.debug("clear_pending_audio_wav: failed to remove %s: %s", self._panel.audio_wav_path, exc)
            self._panel.audio_wav_path = None

    def _kill_inflight_stt(self) -> None:
        """Kill the local Whisper child when the sidebar is disposed.

        Stop does not call this: STT runs to completion and ``_do_send`` puts the
        transcript in the Ask box instead of sending it.
        """
        kill = self._panel._stt_kill
        if not callable(kill):
            return
        try:
            kill()
        except Exception:
            log.debug("STT kill failed", exc_info=True)

    def sync_audio_slice(self) -> None:
        """Mirror :attr:`audio_recorder.state` into the composite (strategy A)."""
        import dataclasses

        if self._panel.audio_recorder is None:
            return
        self._panel.sidebar_state = dataclasses.replace(self._panel.sidebar_state, audio=self._panel.audio_recorder.state)

    def exit_hands_free_record(self) -> None:
        """Drop sticky so the next reply does not arm Record again."""
        was_sticky = self._panel._record_gesture.sticky
        self._panel._record_gesture = exit_sticky(self._panel._record_gesture)
        self._panel._sticky_restart_pending = False
        self._panel._sticky_restart_gen += 1
        self._panel._record_hold_gen += 1
        self._panel._empty_take_count = 0
        if was_sticky:
            log.info("Hands-free record cleared")

    def stop_during_take(self) -> bool:
        """Stop while a take is recording. True when this click was handled.

        One step down per click. Stop disabled during every take leaves a
        hands-free loop (a take between turns) with no exit short of Clear
        (which wipes the chat) or closing the sidebar. Stop Rec cannot be
        the exit: it is the send, and loud rooms need it when silence never
        fires. A locked take becomes a one-shot take that keeps recording
        (Stop Rec or silence sends it, nothing re-arms); Stop on a one-shot
        take cancels it without sending.
        """
        from plugin.framework.i18n import _

        if not self._panel.sidebar_state.send.is_recording:
            return False
        # The method and record_gesture.stop_during_take had the same name; this calls the pure planner.
        if plan_take_stop(sticky=self._panel._record_gesture.sticky) == TakeStop.EXIT_LOCK:
            self._panel.exit_hands_free_record()
            self._panel._set_status(_("Hands-free off"))
        else:
            log.info("Stop during take: recording cancelled")
            self._panel.dispatch(SendEvent(SendEventKind.CANCEL_REC_CLICKED))
        return True

    def _apply_record_gesture(self, step: Any) -> None:
        was_sticky = self._panel._record_gesture.sticky
        self._panel._record_gesture = step.gesture
        if step.gesture.sticky and not was_sticky:
            log.info("Hands-free record armed")
        if step.cancel_timer:
            self._panel._record_hold_gen += 1
        if step.start_timer:
            self._panel._arm_record_hold_timer()
        if step.dispatch_record:
            _stop_tts()
            self._panel.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))

    def _arm_record_hold_timer(self) -> None:
        self._panel._record_hold_gen += 1
        self._panel._spawn_record_hold_wait(self._panel._record_hold_gen)

    def _spawn_timer_wait(
        self,
        delay_ms: int,
        target_fn: Any,
        gen: int,
        name: str,
        error_msg: str,
    ) -> None:
        import time

        from plugin.framework.worker_pool import run_in_background

        delay_s = delay_ms / 1000.0

        def _wait() -> None:
            time.sleep(delay_s)
            try:
                self._panel.queue_executor.post(target_fn, gen)
            except Exception:
                log.debug(error_msg, exc_info=True)

        run_in_background(_wait, name=name, dedicated=True)

    def _spawn_record_hold_wait(self, gen: int) -> None:
        """Sleep off the UI thread, then hop back through QueueExecutor.

        ``dedicated`` so a 2s hold does not pin a shared pool worker. The
        generation is checked on the UI thread; releasing early just bumps it.
        """
        p = self._panel
        self._spawn_timer_wait(RECORD_HOLD_MS, p._on_record_hold_elapsed, gen, "record-hold", "record hold timer post failed")

    def _on_record_hold_elapsed(self, gen: int) -> None:
        if gen != self._panel._record_hold_gen:
            return
        self._panel._apply_record_gesture(gesture_hold_elapsed(self._panel._record_gesture))

    def _release_open_microphone(self) -> None:
        """Stop capture and leave the Stop Rec label.

        Clear and sidebar close must stop the mic, not only
        exit_hands_free_record. Leaving AudioRecorder running keeps the
        take: recording is not is_busy, so STOP_CLICKED does not apply, and
        the next click sends it. ERROR_OCCURRED clears is_recording without
        starting a send.
        """
        recorder = getattr(self._panel, "audio_recorder", None)
        if recorder is not None:
            try:
                recorder.cleanup()
            except Exception:
                log.debug("audio recorder cleanup failed", exc_info=True)
        if self._panel.sidebar_state.send.is_recording:
            try:
                self._panel.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
            except Exception:
                log.debug("leave Stop Rec on teardown failed", exc_info=True)

    def _flush_sticky_restart(self) -> None:
        """Arm Record again if this completed turn left sticky on."""
        pending = self._panel._sticky_restart_pending
        self._panel._sticky_restart_pending = False
        if not pending or not self._panel._record_gesture.sticky:
            return
        self._panel._begin_sticky_rerecord()

    def _begin_sticky_rerecord(self) -> None:
        send = self._panel.sidebar_state.send
        if send.is_busy or send.is_recording or not send.audio_supported:
            return
        speaking = False
        try:
            from plugin.audio.tts_service import is_speaking

            speaking = bool(is_speaking())
        except Exception:
            log.debug("hands-free re-record: is_speaking failed", exc_info=True)
        decision = sticky_restart(sticky=True, speaking=speaking)
        if decision == StickyRestart.RECORD:
            log.info("Hands-free record restarting")
            self._panel.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))
        elif decision == StickyRestart.WAIT_FOR_TTS:
            self._panel._sticky_restart_gen += 1
            log.debug("Hands-free record waiting for TTS")
            self._panel._spawn_sticky_tts_wait(self._panel._sticky_restart_gen)

    def _spawn_sticky_tts_wait(self, gen: int) -> None:
        """Poll ``is_speaking`` later. Do not sleep on the UNO thread."""
        self._spawn_timer_wait(
            STICKY_TTS_POLL_MS,
            self._panel._poll_sticky_rerecord,
            gen,
            "sticky-rerecord",
            "hands-free TTS wait post failed",
        )

    def _poll_sticky_rerecord(self, gen: int) -> None:
        if gen != self._panel._sticky_restart_gen or not self._panel._record_gesture.sticky:
            return
        self._panel._begin_sticky_rerecord()

    def _on_audio_auto_stop(self) -> None:
        """Silence detector ended capture; same FSM path as clicking Stop Rec (stop + send).

        Hands-free does not replace this. The sticky flag stays set so the
        reply's ``SEND_COMPLETED`` can arm Record again.
        """
        # A post can already be queued when disposing clears the recorder hooks.
        if self._panel._panel_teardown or self._panel.ctx is None:
            return
        if not self._panel.sidebar_state.send.is_recording:
            log.info("audio auto-stop ignored (not recording)")
            return
        log.info("Audio silence pause detected — treating as Stop Rec")
        self._panel.dispatch(SendEvent(SendEventKind.STOP_REC_CLICKED))

    def _on_audio_recorder_error(self, msg: str) -> None:
        """Child error after ready, on the UI thread.

        The stdout monitor only posts here. Applying the recorder event on
        that thread raised out of ReportErrorEffect and left the button on
        Stop Rec. ERROR_OCCURRED drops the recording label.
        """
        if self._panel._panel_teardown or self._panel.ctx is None:
            return
        recorder = self._panel.audio_recorder
        if recorder is not None:
            recorder.apply_stdout_error(msg)
        self._panel.sync_audio_slice()

        # Bugfix: Any child error dispatched ERROR_OCCURRED. Because the monitor
        # posts onto the UI queue, STOP_REC_CLICKED had already set is_busy=True
        # and is_recording=False before that post ran (including during a text
        # send), clearing is_busy while the send drain was still running. Only
        # an open take may take the send-button error transition; a failed send
        # still dispatches ERROR_OCCURRED from the drain while is_busy.
        if not self._panel.sidebar_state.send.is_recording:
            log.warning("audio recorder error while not recording: %s", msg)
            return

        self._panel._start_local_error_turn("\n[Audio error: %s]\n" % msg)
        self._panel.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))

    def _on_audio_silence_progress(self, silence_ms: int) -> None:
        from plugin.framework.i18n import _

        if self._panel._panel_teardown or self._panel.ctx is None:
            return
        if self._panel.sidebar_state.send.is_recording:
            if self._panel._record_gesture.sticky:
                self._panel._set_status(hands_free_silence_text(silence_ms))
            else:
                self._panel._set_status(_("Recording audio… (%d ms silence)") % silence_ms)
