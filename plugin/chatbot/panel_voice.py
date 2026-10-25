from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from plugin.chatbot.record_gesture import (
    EMPTY_TAKES_EXIT,
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
from plugin.framework.blocking_wait import run_blocking_in_thread
from plugin.framework.config import get_api_config
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.send_cancellation import capture_send_stop

log = logging.getLogger("plugin.chatbot.panel")


def _stop_tts() -> None:
    """Stop active speech synthesis, logging errors at debug level."""
    try:
        from plugin.audio.tts_service import stop_speech

        stop_speech()
    except Exception:
        log.debug("stop_speech failed", exc_info=True)


def stop_playback_if_speaking(host: Any) -> bool:
    """If speech is active, stop playback, exit hands-free, and disable Stop."""
    from plugin.audio.tts_service import is_speaking

    if is_speaking():
        _stop_tts()
        if not getattr(host, "_send_busy", False):
            if hasattr(host, "_voice"):
                host._voice.exit_hands_free_record()
            elif hasattr(host, "exit_hands_free_record"):
                host.exit_hands_free_record()
            if host.stop_control and host.stop_control.getModel():
                from plugin.framework.errors import suppress_disposed

                with suppress_disposed("disable stop on speech stopped", logger=log):
                    host.stop_control.getModel().Enabled = False
            return True
    return False


class _CancellableWaiter:
    def __init__(self) -> None:
        self._event: threading.Event | None = None

    def cancel(self) -> None:
        if self._event is not None:
            self._event.set()
            self._event = None

    def wait_and_post(
        self,
        delay_ms: int,
        executor: Any,
        callback: Callable[[], None],
        name: str,
    ) -> None:
        self.cancel()
        ev = threading.Event()
        self._event = ev
        delay_s = delay_ms / 1000.0

        def _worker() -> None:
            if not ev.wait(delay_s):
                try:
                    executor.post(callback)
                except Exception:
                    log.debug("%s post failed", name, exc_info=True)

        from plugin.framework.worker_pool import run_in_background

        run_in_background(_worker, name=name, dedicated=True)


class PanelVoiceController:
    """Voice controller for STT, TTS, and recording gestures."""

    _panel: Any
    _sticky_restart_pending: bool
    _empty_take_count: int

    def __init__(self, panel: Any) -> None:
        self._panel = panel
        self._sticky_restart_pending = False
        self._empty_take_count = 0
        self._hold_waiter = _CancellableWaiter()
        self._sticky_waiter = _CancellableWaiter()

    def teardown(self) -> None:
        self._hold_waiter.cancel()
        self._sticky_waiter.cancel()

    def note_send_completed(self, was_busy: bool) -> None:
        gesture = getattr(self._panel, "_record_gesture", None)
        if was_busy and gesture is not None and getattr(gesture, "sticky", False):
            self._sticky_restart_pending = True

    def note_empty_take(self) -> bool:
        gesture = getattr(self._panel, "_record_gesture", None)
        if gesture is not None and getattr(gesture, "sticky", False):
            self._empty_take_count += 1
            if self._empty_take_count >= EMPTY_TAKES_EXIT:
                log.info("Hands-free off after %d empty takes", self._empty_take_count)
                self.exit_hands_free_record()
                return True
        return False

    def _kill_inflight_stt(self) -> None:
        """Kill the Whisper child for the transcription that is running now.

        The scope hook covers Stop on the send that started STT. This covers
        Stop after a second send replaced ``_send_cancellation``: that click
        cancels the new scope, which does not own the first child.
        """
        kill = self._panel._stt_kill
        if not callable(kill):
            return
        try:
            kill()
        except Exception:
            log.debug("STT kill failed", exc_info=True)

    def exit_hands_free_record(self) -> None:
        """Drop sticky so the next reply does not arm Record again."""
        self._hold_waiter.cancel()
        self._sticky_waiter.cancel()
        was_sticky = self._panel._record_gesture.sticky
        self._panel._record_gesture = exit_sticky(self._panel._record_gesture)
        self._sticky_restart_pending = False
        self._panel._sticky_restart_gen += 1
        self._panel._record_hold_gen += 1
        self._empty_take_count = 0
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
            self.exit_hands_free_record()
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
            self._hold_waiter.cancel()
            self._panel._record_hold_gen += 1
        if step.start_timer:
            self._arm_record_hold_timer()
        if step.dispatch_record:
            _stop_tts()
            self._panel.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))

    def _arm_record_hold_timer(self) -> None:
        self._panel._record_hold_gen += 1
        self._spawn_record_hold_wait(self._panel._record_hold_gen)

    def _spawn_record_hold_wait(self, gen: int) -> None:
        """Sleep off the UI thread, then hop back through QueueExecutor."""
        self._hold_waiter.wait_and_post(
            RECORD_HOLD_MS,
            self._panel.queue_executor,
            lambda: self._on_record_hold_elapsed(gen),
            "record-hold",
        )

    def _on_record_hold_elapsed(self, gen: int) -> None:
        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return
        if gen != self._panel._record_hold_gen:
            return
        self._apply_record_gesture(gesture_hold_elapsed(self._panel._record_gesture))

    def _release_open_microphone(self) -> None:
        """Stop capture and leave the Stop Rec label."""
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
        pending = self._sticky_restart_pending
        self._sticky_restart_pending = False
        if not pending or not self._panel._record_gesture.sticky:
            return
        self._begin_sticky_rerecord()

    def _begin_sticky_rerecord(self) -> None:
        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return
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
            self._spawn_sticky_tts_wait(self._panel._sticky_restart_gen)

    def _spawn_sticky_tts_wait(self, gen: int) -> None:
        """Poll ``is_speaking`` later. Do not sleep on the UNO thread."""
        self._sticky_waiter.wait_and_post(
            STICKY_TTS_POLL_MS,
            self._panel.queue_executor,
            lambda: self._poll_sticky_rerecord(gen),
            "sticky-rerecord",
        )

    def _poll_sticky_rerecord(self, gen: int) -> None:
        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return
        if gen != self._panel._sticky_restart_gen or not self._panel._record_gesture.sticky:
            return
        self._begin_sticky_rerecord()

    def _on_audio_auto_stop(self) -> None:
        """Silence detector ended capture; same FSM path as clicking Stop Rec (stop + send)."""
        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return
        if not self._panel.sidebar_state.send.is_recording:
            log.info("audio auto-stop ignored (not recording)")
            return
        log.info("Audio silence pause detected — treating as Stop Rec")
        self._panel.dispatch(SendEvent(SendEventKind.STOP_REC_CLICKED))

    def _on_audio_recorder_error(self, msg: str) -> None:
        """Child error after ready, on the UI thread."""
        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return
        recorder = getattr(self._panel, "audio_recorder", None)
        if recorder is not None:
            recorder.apply_stdout_error(msg)

        if not self._panel.sidebar_state.send.is_recording:
            log.warning("audio recorder error while not recording: %s", msg)
            return

        self._panel._start_local_error_turn("\n[Audio error: %s]\n" % msg)
        self._panel.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))

    def _on_audio_silence_progress(self, silence_ms: int) -> None:
        from plugin.framework.i18n import _

        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return
        if self._panel.sidebar_state.send.is_recording:
            if self._panel._record_gesture.sticky:
                self._panel._set_status(hands_free_silence_text(silence_ms))
            else:
                self._panel._set_status(_("Recording audio… (%d ms silence)") % silence_ms)

    def _transcribe_audio(self, wav_path: str, stt_model: str) -> str:
        """Transcribe audio synchronously using event pumping on the main thread."""
        from plugin.audio.stt_service import (
            status_for_transcription,
            terminate_stt_process,
            transcribe,
            uses_local_stt,
        )
        from plugin.framework.queue_executor import post_to_main_thread

        if getattr(self._panel, "_stt_inflight", False):
            log.info("Ignoring re-entrant STT; the in-flight transcription keeps the first Stop")
            return ""

        self._panel._stt_inflight = True
        try:
            cancel_scope, stop_checker = capture_send_stop(self._panel)
            if stop_checker():
                self._panel._terminal_status = "Stopped"
                return ""

            if uses_local_stt():
                client = None
            else:
                api_config = get_api_config()
                client = LlmClient(
                    api_config,
                    self._panel.ctx,
                    cancellation_scope=cancel_scope,
                    register_with_send=False,
                )
                if cancel_scope is not None and not cancel_scope.is_cancelled():
                    clearer = getattr(client, "clear_stop", None)
                    if callable(clearer):
                        clearer()

            transcribing = status_for_transcription()
            self._panel._set_status(transcribing)
            self._panel._append_response("\n[" + transcribing + "]\n")

            def on_status(message: str) -> None:
                post_to_main_thread(self._panel._set_status, message)

            def _on_spawn(proc: Any) -> None:
                def _kill() -> None:
                    terminate_stt_process(proc)

                self._panel._stt_kill = _kill

            def _call() -> str:
                return transcribe(
                    wav_path,
                    client=client,
                    model=stt_model,
                    on_status=on_status,
                    on_spawn=_on_spawn,
                )

            try:
                transcript_text = run_blocking_in_thread(self._panel.ctx, _call)
            except Exception as e:
                from plugin.framework.i18n import _

                log.exception("Transcription error in _transcribe_audio")
                self._panel._append_response("\n" + _("[Transcription error: {0}]").format(str(e)) + "\n")
                raise
            if stop_checker():
                log.info("Speech-to-text finished after Stop; preserving transcript for query box")
                self._panel._terminal_status = "Stopped"
                return transcript_text
            return transcript_text
        finally:
            self._panel._stt_inflight = False
            self._panel._stt_kill = None
            from plugin.framework.errors import best_effort

            best_effort(lambda: os.remove(wav_path), "cleanup audio wav")
            self._panel.audio_wav_path = None

    def resolve_voice_query(self, query_text: str) -> tuple[str, bool]:
        """Resolve query from audio_wav_path via STT fallback if needed.

        Returns (query_text, should_exit_send).
        """
        if not getattr(self._panel, "audio_wav_path", None):
            return query_text, False

        from plugin.audio.stt_service import uses_local_stt
        from plugin.framework.client.model_fetcher import (
            get_stt_model,
            get_text_model,
            has_native_audio,
        )
        from plugin.framework.config import get_current_endpoint
        from plugin.framework.i18n import _

        current_model = get_text_model()
        current_endpoint = get_current_endpoint()

        local_stt = uses_local_stt()
        if local_stt or has_native_audio(current_model, current_endpoint) is False:
            stt_model = get_stt_model()
            if local_stt or stt_model:
                if local_stt:
                    log.info("_do_send: local Whisper STT (chat model %s)", current_model)
                else:
                    log.warning(
                        "_do_send: model %s has no native audio, using stt fallback %s",
                        current_model,
                        stt_model,
                    )
                try:
                    transcript = self._transcribe_audio(self._panel.audio_wav_path, stt_model)

                    typed_after = ""
                    if self._panel.query_control and self._panel.query_control.getModel():
                        from plugin.chatbot.dialogs import get_control_text

                        typed_after = (get_control_text(self._panel.query_control) or "").strip()

                    if self._panel._terminal_status == "Stopped":
                        new_text = (
                            (query_text + "\n" + transcript).strip()
                            if (query_text and transcript)
                            else (transcript or query_text)
                        )
                        if typed_after and typed_after != query_text:
                            new_text = (
                                (typed_after + "\n" + transcript).strip()
                                if (typed_after and transcript)
                                else (transcript or typed_after)
                            )

                        self._panel._restore_query_text(new_text)
                        self._panel._sync_has_text_from_query()
                        return query_text, True

                    user_changed_ask = bool(typed_after and typed_after != query_text)
                    if user_changed_ask:
                        query_text = typed_after

                    if transcript:
                        self._empty_take_count = 0
                        if user_changed_ask:
                            restored = (typed_after + "\n" + transcript).strip()
                            self._panel._restore_query_text(restored)
                        else:
                            query_text = (
                                (query_text + "\n" + transcript).strip()
                                if query_text
                                else transcript
                            )
                except Exception as e:
                    from plugin.framework.errors import NetworkError

                    if isinstance(e, NetworkError):
                        log.exception("NetworkError during STT fallback")
                    else:
                        log.exception("Error during STT fallback")
                    self._panel._terminal_status = "Error"
                    self._panel._restore_query_text(query_text)
                    return query_text, True

                if not query_text.strip():
                    self._panel._append_response("\n" + _("[No speech detected.]") + "\n")
                    self._panel._terminal_status = "Stopped"
                    if self.note_empty_take():
                        self._panel._append_response(_("[Hands-free off: no speech heard.]") + "\n")
                    return query_text, True
            else:
                err_msg = _(
                    "[Model {0} does not support native audio. Please select an STT Model in Settings.]"
                ).format(current_model)
                self._panel._append_response("\n%s\n" % err_msg)
                self._panel._terminal_status = "Error"
                self._panel._set_status(_("Error"))
                self._panel._restore_query_text(query_text)
                return query_text, True
        else:
            log.debug("_do_send: model %s supports native audio, proceeding", current_model)
            if self._panel._terminal_status == "Stopped":
                self._panel._restore_query_text(query_text)
                return query_text, True

        return query_text, False
