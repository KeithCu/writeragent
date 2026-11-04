# WriterAgent - LibreOffice Extension
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""SessionKey class and helpers for session key prefixes and document keys."""

from __future__ import annotations

from dataclasses import dataclass
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

    def wire(self) -> str:
        if self.kind is SessionKind.OTHER:
            return self.rest
        return f"{self.kind.value}:{self.rest}"

    @classmethod
    def parse(cls, text: str) -> SessionKey:
        prefix, sep, rest = text.partition(":")
        if sep and prefix in _KIND_BY_PREFIX:
            return cls(_KIND_BY_PREFIX[prefix], rest)
        return cls(SessionKind.OTHER, text)

    @classmethod
    def of(cls, kind: SessionKind, document_key: str) -> SessionKey:
        if kind is SessionKind.OTHER:
            raise ValueError("OTHER is parse-only")
        return cls(kind, document_key)

    @classmethod
    def from_host(cls, value: object) -> SessionKey | None:
        if not isinstance(value, str) or not value:
            return None
        return cls.parse(value)

    def with_init_suffix(self) -> SessionKey:
        return SessionKey.parse(self.wire() + INIT_SUFFIX)

    def endswith_init(self) -> bool:
        return self.wire().endswith(INIT_SUFFIX)

    def document_lookup_key(self) -> str | None:
        if self.kind not in DOCUMENT_KINDS:
            return None
        key = self.rest
        if self.kind is SessionKind.CALC and key.endswith(INIT_SUFFIX):
            key = key[: -len(INIT_SUFFIX)]
        return key or None


def is_reserved_caller_id(session_id: str) -> bool:
    sid = session_id.strip()
    if sid.endswith(INIT_SUFFIX):
        return True
    if sid.startswith(f"{SessionKind.ISOLATED.value}:"):
        return True
    return sid in _KIND_BY_PREFIX


def new_unsaved_document_key() -> str:
    return f"{UNSAVED_PREFIX}{uuid.uuid4()}"


def is_unsaved_document_key(value: str) -> bool:
    return value.startswith(UNSAVED_PREFIX)
