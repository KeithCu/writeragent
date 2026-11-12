# WriterAgent - moved from plugin.framework.prompts (framework must not import applications)
"""Peer messaging prompt blocks and outer/inner tool-result annotations."""
from __future__ import annotations

import logging
from typing import Any

# Module import, not a name import: tests patch plugin.doc.peer_message.list_v1_peers
# at the owner, and that one patch must also cover this prompt builder.
from plugin.doc import peer_message
from plugin.framework.doc_type import is_calc, is_draw
from plugin.framework.queue_executor import main

PEER_OUTER_IDLE_AFTER_SEND = "Stop tool use and Ready. Why: the peer reply arrives as a later user turn; more document_research, python, or query tools in this turn race the peer."

# Outer main chat only — no send_peer_work / send_peer_result on this loop.
# {delegate} is the Writer/Calc/Draw specialized gateway. Shown when a v1 peer is open.
PEER_OUTER_DELEGATE_HINT = (
    'Do {delegate}(domain="document_research") for sibling Writer/Calc/Draw work. '
    "Why: the inner agent chooses a silent read vs asking the peer sidebar; this loop must not invent the other app's tools.\n"
    "After that inner result means a peer message was sent/accepted, or the answer says waiting for a peer reply: "
    f"{PEER_OUTER_IDLE_AFTER_SEND} "
    "A short chat line that the peer was asked is OK.\n"
    "When this turn is a [Peer work from: …] envelope: do the local work with your tools "
    "(nested domains such as ranges, sheets, charts, … are fine for that local work); "
    'finishing those specializes is not delivery — you MUST still Do {delegate}(domain="document_research") '
    "to deliver via send_peer_result "
    "(one string: envelope uid or url, and the HTML or result — not a JSON array). "
    "Why: only that inner agent can deliver the peer result; finishing with only a local sidebar answer never reaches the asking peer.\n"
    "When this turn is a [Peer result from: …] envelope: it is information for the requester. Use it to answer the user; insert it into the document only if the user asked for that. Then stop. Do not delegate an ack specialize. "
    "Why: this is the answer to your earlier work request, not a new work request."
)

# Inner answer / tool-result text that means the outer should idle (not keep researching).
_PEER_WAIT_OUTCOME_MARKERS = ("waiting for a peer reply", "waiting for peer", "peer message was sent", "message sent to the peer", '"accepted": true', "'accepted': true", "accepted: true")


# Outer must keep going after a nested specialize on a Peer-work receiving turn.
# ranges/sheets/charts/… finishing (or document_research without send_peer_result)
# is local work only — not peer delivery.
PEER_OUTER_DELIVERY_STILL_REQUIRED = (
    'Peer delivery still required: you MUST Do domain="document_research" to deliver via send_peer_result. Why: finishing a nested specialize (ranges/sheets/charts/…) or research without send_peer_result is not peer delivery; Ready here leaves the asking peer waiting.'
)

def looks_like_peer_work_envelope(text: str | None) -> bool:
    """True when *text* is (or starts as) a [Peer work from:…] turn envelope."""
    if not text:
        return False
    return str(text).lstrip().startswith("[Peer work from:")


def _append_peer_note(payload: dict[str, Any], text: str) -> dict[str, Any]:
    """Copy payload and append *text* to message, and to result when it is non-empty."""
    out = dict(payload)
    message = str(out.get("message") or "")
    out["message"] = (message + " " + text).strip()
    result = str(out.get("result") or "")
    if result:
        out["result"] = result + "\n" + text
    return out


def annotate_outer_peer_wait(payload: dict[str, Any], *, peer_send_invoked: bool = False) -> dict[str, Any]:
    """Append idle-after-send on an ok document_research payload when a peer was asked.

    The outer model otherwise treats “Message sent to the peer…” as unfinished work
    and starts another document_research / query in the same turn.
    """
    if payload.get("status") != "ok":
        return payload
    blob = " ".join(str(payload.get(key) or "") for key in ("message", "result", "answer"))
    blob_lower = blob.lower()
    if not (peer_send_invoked or any(marker in blob_lower for marker in _PEER_WAIT_OUTCOME_MARKERS)):
        return payload
    if PEER_OUTER_IDLE_AFTER_SEND in blob:
        return payload
    return _append_peer_note(payload, PEER_OUTER_IDLE_AFTER_SEND)


def annotate_outer_peer_delivery_pending(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp still-required peer delivery on an ok specialize return during Peer work.

    Nested domains can finish successfully while the outer still owes
    ``document_research`` → ``send_peer_result``. Sets ``instruction`` and appends
    the same line on ``message`` / ``result`` so the outer cannot treat the hop as done.
    """
    if payload.get("status") != "ok":
        return payload
    blob = " ".join(str(payload.get(key) or "") for key in ("message", "result", "answer", "instruction"))
    if PEER_OUTER_DELIVERY_STILL_REQUIRED in blob:
        return payload
    out = _append_peer_note(payload, PEER_OUTER_DELIVERY_STILL_REQUIRED)
    existing = out.get("instruction")
    if isinstance(existing, str) and existing.strip():
        out["instruction"] = (existing.rstrip() + " " + PEER_OUTER_DELIVERY_STILL_REQUIRED).strip()
    else:
        out["instruction"] = PEER_OUTER_DELIVERY_STILL_REQUIRED
    return out


# document_research specialized only. Short DO+why; catalog is appended when peers exist.
# Hard fork ASK vs REPLY by whether the task contains a [Peer work from: …] envelope.
# Ask path (no envelope): matching Open peers → send_peer_work only, then finish.
# Never delegate_read_document on that open peer; never send_peer_result from the asker.
# Soft "change/compute/write vs file fact" let the inner reopen a live peer (wasted
# work, races the peer reply). Ask polarity: do not request a content dump so you
# can fill elsewhere — the live sidebar owns the write tools.
# Reply path (envelope present): send_peer_result is a side effect. Outer already
# stuffed the HTML/result into task, so the smol "answer from the task alone →
# finish" rule otherwise skips the send and the peer sidebar never sees the reply.
# Naming "send_peer_result" / "reply back" in an *ask* task does NOT flip polarity —
# only a [Peer work from: …] envelope in the task does.
PEER_INNER_CHOICE_RULES = (
    "ASK vs REPLY: The reply path is only when the task contains a [Peer work from: …] "
    "envelope. Mentioning send_peer_result, reply back, or delivering a result in an ask "
    "task does not make this the reply path — only that envelope does.\n"
    "ASK PATH (task is about an Open peer file; no [Peer work from: …] in the task): "
    "Do send_peer_work(document_url=<peer uid, URL, or unique name>, "
    "message=<task; say the answer must come back as a peer result>), "
    "then specialized_workflow_finished. "
    "NEVER delegate_read_document on that open peer. "
    "NEVER send_peer_result from the asker. "
    "Why: that sidebar is live and will send_peer_result back; silent reopen only peeks, "
    "duplicates work, and races the peer reply; send_peer_result here stamps [Peer result "
    "from: …] onto the peer instead of waiting for their reply.\n"
    "When sending to an Open peer about that peer's own document and the task needs a "
    "change, fill, or write, Do ask that peer in message to perform the edit/fill and "
    "include the values/facts to write. "
    "Why: that live sidebar owns the write tools for that file; asking only for a dump of "
    "blank/current content so you can fill it elsewhere skips the peer write path.\n"
    "Do delegate_read_document only when the file is not in Open peers (nearby on disk / "
    "no live sidebar). "
    "Why: there is no live sidebar to ask.\n"
    "REPLY PATH (task is to reply to a [Peer work from: …] envelope — outer stuffed "
    "uid/url + HTML/result): you MUST send_peer_result("
    "document_url=<uid or url from the envelope>, message=<one HTML/result string>) "
    "before specialized_workflow_finished — that tool stamps [Peer result from: …]. "
    "Why: putting the reply only in answer stays inside this loop — the peer sidebar "
    "never sees it.\n"
    "On the reply path only: HTML, table, or other result text in the task is the "
    "message argument to send_peer_result, not a final answer. "
    "Why: the outer already did the local work; your job is deliver via the tool.\n"
    "Do send_peer_result on a peer-reply task even when you can answer from the task alone. "
    "Why: that rule is for silent research finishes; peer delivery is a tool side effect.\n"
    "After ok/accepted (ask or reply) you MUST call specialized_workflow_finished immediately. "
    "Why: the peer runs after this loop exits; waiting deadlocks the reply."
)


def _agent_for_model(model: Any) -> str:
    # Unknown document types and None are treated as Writer.
    if is_calc(model):
        return "Calc"
    if is_draw(model):
        return "Draw"
    return "Writer"


def format_peer_outer_delegate_hint(model: Any) -> str:
    """Outer DO+why with the matching specialized gateway name."""
    delegate = f"delegate_to_specialized_{_agent_for_model(model).lower()}_toolset"
    return PEER_OUTER_DELEGATE_HINT.format(delegate=delegate)


def _peer_block_on_main_thread(build: Any, fail_log: str) -> str:
    """Run a peer-catalog prompt builder on the UI thread. Empty string on failure."""
    try:
        return main.run(build)
    except Exception:
        logging.getLogger(__name__).exception(fail_log)
        return ""


def get_peer_messaging_prompt_block(model: Any, ctx: Any) -> str:
    """Thin outer pointer when a v1 peer is open. No send_peer_work/result on this loop.

    ``list_v1_peers`` touches UNO (RuntimeUID / desktop catalog). Chat refresh can
    run off-main; marshal like ``get_peer_inner_choice_block`` so the hint is not
    silently dropped when assert_main_thread fails.
    """
    if ctx is None or model is None:
        return ""

    def _build() -> str:
        peers = peer_message.list_v1_peers(ctx, model)
        if not peers:
            return ""
        return format_peer_outer_delegate_hint(model)

    return _peer_block_on_main_thread(_build, "peer messaging prompt block failed")


def get_peer_inner_choice_block(uno_ctx: Any, doc: Any) -> str:
    """Short inner read-vs-peer rules plus catalog. Empty when no v1 peer is open.

    ``list_v1_peers`` reads RuntimeUID and the desktop catalog (UNO). Specialized
    execute runs on a tool-async worker, so this marshals when called off-main.
    """
    if uno_ctx is None:
        return ""

    def _build() -> str:
        peers = peer_message.list_v1_peers(uno_ctx, doc)
        if not peers:
            return ""
        return PEER_INNER_CHOICE_RULES + "\n" + peer_message.format_peer_catalog(peers)

    return _peer_block_on_main_thread(_build, "peer inner choice prompt block failed")


