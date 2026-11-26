# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Split authoring for agent tracked changes: insertions and deletions get DIFFERENT authors
so LibreOffice's "show changes by author" coloring shows them in two distinct colors.

A redline's author is captured at creation time and is READ-ONLY afterward (setting
RedlineAuthor on an existing redline is a silent no-op), so the only way to author a replace's
Insert and Delete differently is to swap the office author BETWEEN the deletion and the
insertion. EditReviewSession makes the INSERT author the default for the whole edit via
``begin()``; each replace primitive wraps its ``setString("")`` deletion in ``deletion_author()``,
which flips the office author to the DELETE author for that moment and back. All of this runs
synchronously on the main thread, so the brief intermediate author state is never observed.

The actual colors are LibreOffice's automatic per-author choice (we cannot pick them) -- the two
authors just guarantee two distinct colors. Inert (no swaps) unless ``begin()`` armed this thread.

Before swapping, ``begin()`` saves the user's real name to ``writeragent_office_author.json``
in the user config directory. At startup, ``restore_author_after_crash()`` writes that saved
name back if the profile still holds one of the two agent author names.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from typing import Any, Generator

from plugin.framework.config import atomic_write_text, user_config_dir
from plugin.framework.i18n import get_lo_locale

log = logging.getLogger(__name__)

# Distinct authors -> distinct by-author colors. Both read as the agent in the Manage dialog.
INSERT_AUTHOR = "WriterAgent"
DELETE_AUTHOR = "WriterAgent (deletions)"
_AGENT_PROFILES: frozenset[tuple[str, str]] = frozenset({(INSERT_AUTHOR, ""), (DELETE_AUTHOR, "")})
_SAVED_AUTHOR_FILE = "writeragent_office_author.json"

_state = threading.local()


def _saved_author_path() -> str | None:
    config_dir = user_config_dir()
    if not config_dir:
        return None
    return os.path.join(config_dir, _SAVED_AUTHOR_FILE)


def _saved_prior() -> tuple[str, str] | None:
    path = _saved_author_path()
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("review_authors: could not read saved author from %s", path, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    given = data.get("givenname")
    sn = data.get("sn")
    if isinstance(given, str) and isinstance(sn, str):
        return (given, sn)
    return None


def _save_prior(prior: tuple[str, str]) -> None:
    path = _saved_author_path()
    if path is None:
        return
    try:
        atomic_write_text(
            path,
            json.dumps({"givenname": prior[0], "sn": prior[1]}),
            prefix=".office-author-",
        )
    except OSError:
        log.warning("review_authors: could not save prior author to %s", path, exc_info=True)


def _disarm() -> None:
    _state.ctx = None
    _state.last = None


def _author_access(ctx: Any) -> Any:
    # com.sun.star names resolve through PyUNO's import hook (pyuno/source/module/uno.py _uno_import); importing here keeps a missing type inside this try.
    from com.sun.star.beans import NamedValue

    smgr = ctx.getServiceManager()
    provider = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
    node = NamedValue()
    node.Name = "nodepath"
    node.Value = "/org.openoffice.UserProfile/Data"
    return provider.createInstanceWithArguments(
        "com.sun.star.configuration.ConfigurationUpdateAccess", (node,))


def _commit_author(access: Any, given: str, sn: str) -> None:
    access.setPropertyValue("givenname", given)
    access.setPropertyValue("sn", sn)
    access.commitChanges()


def set_office_author(ctx: Any, given: str) -> bool:
    try:
        access = _author_access(ctx)
        _commit_author(access, given, "")
        _state.last = given
        return True
    except Exception:
        log.debug("review_authors: could not set office author", exc_info=True)
        return False


def office_author(ctx: Any) -> str:
    """The name LibreOffice stamps on a redline the user records now.

    Matches ``SvtUserOptions::Impl::GetFullName`` (``unotools/source/config/useroptions.cxx``)
    and ``SwModule::GetRedlineAuthor`` (``sw/source/uibase/app/swmodul1.cxx``).
    """
    access = _author_access(ctx)
    lang = get_lo_locale(ctx)[:2]
    if lang in ("ja", "ko", "zh", "hu"):  # MsLangId::isFamilyNameFirst
        names: tuple[str, ...] = ("sn", "givenname")
    elif lang == "ru":
        names = ("givenname", "fathersname", "sn")
    else:
        names = ("givenname", "sn")
    parts = [str(access.getPropertyValue(n) or "").strip() for n in names]
    return " ".join(p for p in parts if p) or str(access.getPropertyValue("initials") or "").strip() or "Unknown Author"


def begin(ctx: Any) -> tuple[str, str] | None:
    """Capture the prior office author, set the INSERT author as the default, and arm
    ``deletion_author()`` on this thread. Returns the prior ``(given, sn)`` for ``end()``, or None.

    Split authoring is armed ONLY if the office author was actually switched. If that fails, the
    thread-local is left disarmed -- so a failed begin() can never leave ``deletion_author()``
    armed for a later, unrelated edit on this thread, even when the caller skips ``end()`` on a
    None return.
    """
    try:
        access = _author_access(ctx)
        prior = (str(access.getPropertyValue("givenname")), str(access.getPropertyValue("sn")))
        if prior not in _AGENT_PROFILES and prior != _saved_prior():
            _save_prior(prior)
        # We commit the swap because LibreOffice stamps redlines with the committed UserProfile name.
        # configmgr writes it to registrymodifications.xcu about 1 s later (configmgr/source/components.cxx
        # WriteThread::execute), so a crash mid-edit can leave it on disk. The real name is saved first
        # so restore_author_after_crash can put it back.
        _commit_author(access, INSERT_AUTHOR, "")
        _state.last = INSERT_AUTHOR
        _state.ctx = ctx
        return prior
    except Exception:
        log.debug("review_authors.begin failed", exc_info=True)
        _disarm()
        return None


def restore_author_after_crash(ctx: Any) -> None:
    """At startup, put back the user's name if a crash left the profile on an agent author."""
    try:
        access = _author_access(ctx)
        current = (str(access.getPropertyValue("givenname")), str(access.getPropertyValue("sn")))
        if current not in _AGENT_PROFILES:
            return
        saved = _saved_prior()
        if saved is None:
            log.warning("review_authors: profile author is %r and no saved name exists; leaving it", current)
            return
        _commit_author(access, *saved)
        log.info("review_authors: restored office author %r after prior crash", saved)
    except Exception:
        log.exception("review_authors: startup author recovery failed")


def end(ctx: Any, prior: tuple[str, str] | None) -> None:
    """Disarm split authoring and restore the prior office author."""
    last = getattr(_state, "last", None)
    _disarm()
    if prior is None:
        return
    try:
        access = _author_access(ctx)
        current = (str(access.getPropertyValue("givenname")), str(access.getPropertyValue("sn")))
        # Tip: a name the user changed in User Data during the edit is not ours to overwrite.
        if current == prior:
            return
        if current[0] != last:
            log.debug("review_authors.end: office author changed outside the edit; leaving %r", current)
            return
        _commit_author(access, prior[0], prior[1])
    except Exception:
        log.warning("review_authors.end: failed to restore office author %r", prior, exc_info=True)


@contextlib.contextmanager
def deletion_author() -> Generator[None, None, None]:
    """Author the tracked deletion inside this block as the DELETE author, then restore the
    INSERT author. A no-op unless ``begin()`` armed split authoring on this thread."""
    ctx = getattr(_state, "ctx", None)
    if ctx is None or not set_office_author(ctx, DELETE_AUTHOR):
        yield
        return
    try:
        yield
    finally:
        set_office_author(ctx, INSERT_AUTHOR)
