from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from plugin.chatbot.record_gesture import (
    EMPTY_TAKES_EXIT,
    RECORD_HOLD_MS,
    STICKY_TTS_POLL_MS,
    exit_sticky,
    gesture_hold_elapsed,
    hands_free_silence_text,
)
from plugin.chatbot.send_state import SendEvent, SendEventKind
from plugin.framework.blocking_wait import run_blocking_in_thread
from plugin.framework.config import get_api_config
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.send_cancellation import is_cancelled, spawn_scope
from plugin.chatbot.tool_loop_actions import assign_send_outcome, send_outcome_is_stopped, send_outcome_terminal_display

log = logging.getLogger(__name__)


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
            # Playback-only Stop does not dispatch STOP_CLICKED. Still leave
            # hands-free, or the TTS poll would arm the mic again.
            host._voice.exit_hands_free_record()
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

        def _fire() -> None:
            if ev is self._event:
                callback()

        def _worker() -> None:
            if not ev.wait(delay_s):
                try:
                    executor.post(_fire)
                except Exception:
                    log.debug("%s post failed", name, exc_info=True)

        from plugin.framework.worker_pool import run_in_background

        run_in_background(_worker, name=name, dedicated=True)


def _join_voice_text(a: str, b: str) -> str:
    """Combine voice transcript with typed text separated by a newline."""
    if a and b:
        return (a + "\n" + b).strip()
    return a or b


class PanelVoiceController:
    """Voice controller for STT, TTS, and recording gestures."""

    _panel: Any
    _sticky_restart_pending: bool
    _empty_take_count: int

    def __init__(self, panel: Any) -> None:
        self._panel = panel
        self._sticky_restart_pending = False
        # Empty transcripts in a row while sticky (EMPTY_TAKES_EXIT ends the loop).
        self._empty_take_count = 0
        self._hold_waiter: _CancellableWaiter = _CancellableWaiter()
        self._sticky_waiter: _CancellableWaiter = _CancellableWaiter()

    def teardown(self) -> None:
        self._hold_waiter.cancel()
        self._sticky_waiter.cancel()

    def note_send_completed(self, was_busy: bool) -> None:
        # Flag only. The drain flushes after speak_text_async so a voice reply
        # is already speaking (or known idle) before we touch the mic.
        if was_busy and self._panel._record_gesture.sticky:
            self._sticky_restart_pending = True

    def note_empty_take(self) -> bool:
        if self._panel._record_gesture.sticky:
            self._empty_take_count += 1
            # End hands-free after EMPTY_TAKES_EXIT empty
            # takes in a row. An empty take finishes as
            # Stopped, not Error, so sticky re-arms Record
            # and noise that trips silence auto-stop loops
            # empty takes forever.
            if self._empty_take_count >= EMPTY_TAKES_EXIT:
                log.info("Hands-free off after %d empty takes", self._empty_take_count)
                self.exit_hands_free_record()
                return True
        return False

    def _kill_inflight_stt(self) -> None:
        """Kill the local Whisper child when the sidebar is disposed.

        Stop does not call this: STT runs to completion and ``_do_send`` puts the
        transcript in the Ask box instead of sending it.
        """
        kill = self._panel._stt_kill
        if not kill:
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
        if self._panel._record_gesture.sticky:
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
        if step.start_timer:
            self._hold_waiter.wait_and_post(
                RECORD_HOLD_MS,
                self._panel.queue_executor,
                self._on_record_hold_elapsed,
                "record-hold",
            )
        if step.dispatch_record:
            _stop_tts()
            self._panel.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))

    def _panel_gone(self) -> bool:
        return bool(getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None)

    def _on_record_hold_elapsed(self) -> None:
        if self._panel_gone():
            return
        self._apply_record_gesture(gesture_hold_elapsed(self._panel._record_gesture))

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
        pending = self._sticky_restart_pending
        self._sticky_restart_pending = False
        if not pending or not self._panel._record_gesture.sticky:
            return
        self._begin_sticky_rerecord()

    def _begin_sticky_rerecord(self) -> None:
        if self._panel_gone():
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
        if speaking:
            log.debug("Hands-free record waiting for TTS")
            self._sticky_waiter.wait_and_post(
                STICKY_TTS_POLL_MS,
                self._panel.queue_executor,
                self._poll_sticky_rerecord,
                "sticky-rerecord",
            )
        else:
            log.info("Hands-free record restarting")
            self._panel.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))

    def _poll_sticky_rerecord(self) -> None:
        if self._panel_gone():
            return
        if not self._panel._record_gesture.sticky:
            return
        self._begin_sticky_rerecord()

    def _on_audio_auto_stop(self) -> None:
        """Silence detector ended capture; same FSM path as clicking Stop Rec (stop + send)."""
        # A post can already be queued when disposing clears the recorder hooks.
        if self._panel_gone():
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
        # A post can already be queued when disposing clears the recorder hooks.
        if self._panel_gone():
            return
        recorder = getattr(self._panel, "audio_recorder", None)
        if recorder is not None:
            recorder.apply_stdout_error(msg)

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

        if self._panel_gone():
            return
        if self._panel.sidebar_state.send.is_recording:
            if self._panel._record_gesture.sticky:
                self._panel._set_status(hands_free_silence_text(silence_ms))
            else:
                self._panel._set_status(_("Recording audio… (%d ms silence)") % silence_ms)

    def _transcribe_audio(self, wav_path: str, stt_model: str) -> str:
        """Transcribe audio synchronously using event pumping on the main thread.

        The send-scoped checker is taken via ``spawn_scope(self._panel)`` on this
        thread before blocking work, and the
        checker is read once more after the transcript returns. Without
        that checker, ``run_blocking_in_thread`` pumps the UI, so Stop can
        cancel the send scope and a nested send can replace
        the panel's cancellation field, while Whisper's ``subprocess.run`` still
        waits out 900s and the transcript is posted anyway. Stop does
        not kill local Whisper or close the endpoint socket: transcription
        finishes and ``_do_send`` puts the words back in the Ask box instead
        of sending them (see the StopSendEffect invariant in ``panel``).
        Only sidebar teardown kills the child, through ``_stt_kill``. A
        re-entrant call returns without deleting the first WAV or clearing
        the first client's stop latch. The STT client is not registered with
        the send scope for the same reason.
        """
        from plugin.audio.stt_service import (
            status_for_transcription,
            terminate_stt_process,
            transcribe,
            uses_local_stt,
        )
        from plugin.framework.queue_executor import main

        # getattr: a duck-typed host (the smol _do_send double) has no field
        # until this call sets it. Missing means nothing is in flight.
        if getattr(self._panel, "_stt_inflight", False):
            log.info("Ignoring re-entrant STT; the in-flight transcription keeps the first Stop")
            return ""

        self._panel._stt_inflight = True
        try:
            cancel_scope = spawn_scope(self._panel)
            if is_cancelled(cancel_scope):
                assign_send_outcome(self._panel, "stopped")
                return ""

            if uses_local_stt():
                client = None
            else:
                # Always create a fresh client to avoid reusing previous STT endpoint/key
                api_config = get_api_config()
                client = LlmClient(
                    api_config,
                    self._panel.ctx,
                    cancel=cancel_scope,
                    register_with_send=False,
                )
                if not is_cancelled(cancel_scope):
                    client.clear_stop()

            transcribing = status_for_transcription()
            self._panel._set_status(transcribing)
            self._panel._append_response("\n[" + transcribing + "]\n")

            def on_status(message: str) -> None:
                # What was wrong: local Whisper model download status messages were lost.
                # How it happened: on_status callback was dropped during cleanup.
                # Why this change: post status updates to the UI thread so weight
                # download progress is visible in the sidebar.
                main.post(self._panel._set_status, message)

            def _on_spawn(managed: Any) -> None:
                def _kill() -> None:
                    terminate_stt_process(managed)

                # Sidebar teardown (_kill_inflight_stt) calls this. Stop does
                # not: the transcript is kept for the Ask box.
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
                # Runs to the end: Stop keeps the transcript.
                transcript_text = run_blocking_in_thread(self._panel.ctx, _call)
            except Exception as e:
                from plugin.framework.i18n import _

                log.exception("Transcription error in _transcribe_audio")
                self._panel._append_response("\n" + _("[Transcription error: {0}]").format(str(e)) + "\n")
                raise
            if is_cancelled(cancel_scope):
                log.info("Speech-to-text finished after Stop; preserving transcript for query box")
                assign_send_outcome(self._panel, "stopped")
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
        # Transcription Fallback check
        # Local Whisper is the user's STT choice: transcribe in the venv and
        # send text, including when the chat model could take input_audio.
        # Endpoint STT still waits until the chat model cannot take audio.
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

                    if send_outcome_is_stopped(self._panel):
                        # Do not auto-send late transcript into the new typed turn.
                        # Put it back in Ask so it is not silently lost.
                        new_text = _join_voice_text(query_text, transcript)
                        if typed_after and typed_after != query_text:
                            new_text = _join_voice_text(typed_after, transcript)

                        self._panel._restore_query_text(new_text)
                        self._panel.sync_has_text_from_query()
                        return query_text, True

                    user_changed_ask = bool(typed_after and typed_after != query_text)
                    if user_changed_ask:
                        query_text = typed_after

                    if transcript:
                        self._empty_take_count = 0
                        if user_changed_ask:
                            restored = _join_voice_text(typed_after, transcript)
                            self._panel._restore_query_text(restored)
                        else:
                            query_text = _join_voice_text(query_text, transcript)
                except Exception as e:
                    from plugin.framework.errors import NetworkError

                    if isinstance(e, NetworkError):
                        log.exception("NetworkError during STT fallback")
                    else:
                        log.exception("Error during STT fallback")
                    assign_send_outcome(self._panel, "error")
                    self._panel._restore_query_text(query_text)
                    return query_text, True

                # WAV is deleted in _transcribe_audio finally. Empty STT must not
                # fall through into a chat POST with a blank user message.
                if not query_text.strip():
                    self._panel._append_response("\n" + _("[No speech detected.]") + "\n")
                    assign_send_outcome(self._panel, "stopped")
                    if self.note_empty_take():
                        self._panel._append_response(_("[Hands-free off: no speech heard.]") + "\n")
                    return query_text, True
            else:
                err_msg = _(
                    "[Model {0} does not support native audio. Please select an STT Model in Settings.]"
                ).format(current_model)
                self._panel._append_response("\n%s\n" % err_msg)
                assign_send_outcome(self._panel, "error")
                self._panel._set_status(_("Error"))
                self._panel._restore_query_text(query_text)
                return query_text, True
        else:
            log.debug("_do_send: model %s supports native audio, proceeding", current_model)
            if send_outcome_is_stopped(self._panel):
                self._panel._restore_query_text(query_text)
                return query_text, True

        return query_text, False

    def speak_reply(self, *, speak: bool) -> None:
        """Optional TTS after send completion; toggles Stop while speech runs."""
        if not speak or self._panel._panel_teardown:
            return
        from plugin.framework.i18n import _

        terminal = send_outcome_terminal_display(self._panel)
        if terminal:
            self._panel._set_status(_(terminal))
        try:
            from plugin.framework.config import get_config
            from plugin.chatbot.tool_loop_actions import current_turn, session_for_turn

            # Speak from session_for_turn(self) (valid until drop_turn) when TTS is enabled and
            # terminal status is not Stopped. An alive-turn check fails here: the tool-loop finally
            # already ran abort_turn(self).
            if get_config("audio.tts_enabled") and terminal != "Stopped":
                spoken = session_for_turn(self._panel)
                turn = current_turn(self._panel)
                if (
                    turn is not None
                    and spoken is turn.session
                    and spoken is not None
                    and spoken.messages
                ):
                    start_count = turn.send_start_msg_count
                    last_msg = spoken.messages[-1]
                    # Speak only when an assistant message exists and this send appended a new message.
                    # A whitespace-only query or a cancelled send otherwise replays the previous turn: the
                    # session's last message is still that reply.
                    has_new_assistant_msg = (
                        last_msg.get("role") == "assistant"
                        and bool(last_msg.get("content"))
                        and len(spoken.messages) > start_count
                    )
                    if has_new_assistant_msg:
                        from plugin.chatbot.tool_loop_actions import _STOP_LINE
                        from plugin.chatbot.panel import _set_control_enabled
                        from plugin.chatbot.record_gesture import hands_free_status_text

                        content_to_speak = last_msg["content"].replace(_STOP_LINE, "")
                        if content_to_speak.strip():
                            from plugin.audio.tts_service import speak_text_async, is_speaking

                            # Restore the send-complete status after download/fallback lines.
                            prior_status = terminal or "Ready"

                            def _disable_stop() -> None:
                                if not self._panel._send_busy:
                                    # A take may still be recording (sticky restart or a manual take).
                                    # Ready would hide that status or "Hands-free off", so only restore it after.
                                    if self._panel.sidebar_state.send.is_recording:
                                        if self._panel._record_gesture.sticky:
                                            self._panel._set_status(hands_free_status_text())
                                    else:
                                        _set_control_enabled(self._panel.stop_control, False, "disable stop after speech")
                                        self._panel._set_status(_(prior_status))

                            def _on_tts_status(message: str) -> None:
                                # Speech runs on a worker; the status control is a UNO widget.
                                try:
                                    self._panel.queue_executor.post(self._panel._set_status, message)
                                except Exception:
                                    log.debug("TTS status post failed", exc_info=True)

                            speak_text_async(
                                content_to_speak,
                                on_complete=lambda: self._panel.queue_executor.post(_disable_stop),
                                on_status=_on_tts_status,
                                # Sentence breaks use BreakIterator on this UI
                                # thread. The audio worker only receives the list.
                                ctx=self._panel.ctx,
                            )
                            if is_speaking():
                                _set_control_enabled(self._panel.stop_control, True, "enable stop for speech")
        except Exception as e:
            log.debug("TTS playback trigger: %s", e)


def strip_audio_from_last_user_message(host: Any, *, speak: bool) -> None:
    """Remove input_audio parts from the last user row once per turn (deferred drain close)."""
    if not speak:
        return
    from plugin.framework.i18n import _
    from plugin.chatbot.tool_loop_actions import session_for_turn

    sess = session_for_turn(host) or host.session
    if sess and getattr(sess, "messages", None):
        # Ensure in-memory audio is not resent on the next turn.
        # Iterate backwards to find the last user message.
        for msg in reversed(sess.messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, list) and any(c.get("type") == "input_audio" for c in content):
                    kept_parts = [c for c in content if c.get("type") != "input_audio"]
                    if kept_parts:
                        msg["content"] = kept_parts
                    else:
                        msg["content"] = _("[Voice message]")
                break
