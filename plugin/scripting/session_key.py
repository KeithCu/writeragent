# WriterAgent - LibreOffice Extension
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""SessionKey class and helpers for session key prefixes and document keys."""

from __future__ import annotations

from dataclasses import dataclass, replace
import enum
import uuid


class SessionKind(enum.Enum):
    CALC = "calc"
    RPS = "rps"
    NOTEBOOK = "notebook"
    PPT_MASTER = "ppt_master"
    DOC = "doc"
    ISOLATED = "isolated"
    EMBEDDINGS = "embeddings"
    OTHER = "other"


INIT_SUFFIX = ":init"
UNSAVED_PREFIX = "unsaved:"
DOCUMENT_KINDS = (SessionKind.CALC, SessionKind.RPS, SessionKind.NOTEBOOK, SessionKind.PPT_MASTER)
PERSISTABLE_KINDS = (SessionKind.CALC, SessionKind.RPS, SessionKind.NOTEBOOK)
_KIND_BY_PREFIX = {k.value: k for k in SessionKind if k is not SessionKind.OTHER}


@dataclass(frozen=True, slots=True)
class SessionKey:
    kind: SessionKind
    rest: str
    init: bool = False

    def wire(self) -> str:
        if self.kind is SessionKind.OTHER:
            base = self.rest
        else:
            base = f"{self.kind.value}:{self.rest}"
        if self.init:
            return base + INIT_SUFFIX
        return base

    @classmethod
    def parse(cls, text: str) -> SessionKey:
        init = False
        if text.endswith(INIT_SUFFIX):
            init = True
            text = text[: -len(INIT_SUFFIX)]
        # ``calc:init`` (no document key) becomes OTHER+init, not CALC(rest="init").
        # HTTP rejects it via is_reserved_caller_id; host-only wire strings may still appear.
        prefix, sep, rest = text.partition(":")
        if sep and prefix in _KIND_BY_PREFIX:
            return cls(_KIND_BY_PREFIX[prefix], rest, init)
        return cls(SessionKind.OTHER, text, init)

    @classmethod
    def of(cls, kind: SessionKind, document_key: str) -> SessionKey:
        if kind is SessionKind.OTHER:
            raise ValueError("OTHER is parse-only")
        return cls(kind, document_key)

    def with_init_suffix(self) -> SessionKey:
        """Return the init-script companion id for this session (sets ``init=True``).

        Idempotent when ``init`` is already set (``calc:x:init`` stays ``calc:x:init``;
        older code that concatenated ``:init`` could yield ``:init:init``). Compute
        rejects caller ids ending in ``:init``; sandbox reset uses the flag, not stacked suffixes.
        """
        return replace(self, init=True)

    def without_init(self) -> SessionKey:
        return replace(self, init=False)

    def document_lookup_key(self) -> str | None:
        if self.kind not in DOCUMENT_KINDS:
            return None
        return self.rest or None


def is_reserved_caller_id(session_id: str) -> bool:
    """True when *session_id* must not be used as an HTTP compute caller id.

    Reserved: bare kind names (``calc``, ``rps``, …), any name ending in ``:init``,
    and any name starting with ``isolated:``. Bare kind names collide with
    kind-prefixed workbook routing inside LibreOffice (``kind:document_key``).
    """
    if session_id.endswith(INIT_SUFFIX):
        return True
    if session_id.startswith(f"{SessionKind.ISOLATED.value}:"):
        return True
    return session_id in _KIND_BY_PREFIX


RESERVED_CALLER_ID_HELP = (
    "Reserved session_id: a bare kind name (calc, rps, notebook, ppt_master, doc, isolated, embeddings), "
    "any name ending in ':init', or any name starting with 'isolated:' "
    "(bare kind names collide with kind-prefixed session routing)."
)


def reserved_caller_id_error_message(session_id: str) -> str:
    return f"Invalid session_id {session_id!r}: {RESERVED_CALLER_ID_HELP}"


def new_unsaved_document_key() -> str:
    return f"{UNSAVED_PREFIX}{uuid.uuid4()}"


def is_unsaved_document_key(value: str) -> bool:
    return value.startswith(UNSAVED_PREFIX)
