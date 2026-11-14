# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations


def test_sidebar_flags_impress_has_ppt_master():
    from plugin.chatbot.chat_sidebar_mode import (
        CHAT_MODE_PPT_MASTER,
        get_mode_labels,
        mode_from_label,
        sidebar_mode_flags_for_doc_type,
    )

    impress_flags = sidebar_mode_flags_for_doc_type("impress")
    assert impress_flags.include_ppt_master is True
    assert impress_flags.include_brainstorming is False
    labels = get_mode_labels(impress_flags)
    # Librarian is always last; PPT-Master is the last doc-type-specific mode.
    assert mode_from_label(labels[-2], impress_flags) == CHAT_MODE_PPT_MASTER

    writer_flags = sidebar_mode_flags_for_doc_type("writer")
    assert writer_flags.include_ppt_master is False
