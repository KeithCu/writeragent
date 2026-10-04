import re

def apply_fix_1():
    print("Fix 1: The grammar cache key is truncated...")
    file_path = "plugin/writer/locale/grammar_worker.py"
    with open(file_path, "r") as f:
        content = f.read()

    diff_search = """    truncated: list[tuple[GrammarWorkItem, str]] = [
        (item, text[:max_chars] if len(text) > max_chars else text) for item, text in valid_items
    ]
    chunks: list[list[tuple[GrammarWorkItem, str]]] = []
    if len(truncated) > 1 and batch_size > 1:
        for i in range(0, len(truncated), batch_size):
            chunks.append(truncated[i : i + batch_size])
    else:
        for item, text in truncated:
            chunks.append([(item, text)])"""

    diff_replace = """    chunks: list[list[tuple[GrammarWorkItem, str]]] = []
    if len(valid_items) > 1 and batch_size > 1:
        for i in range(0, len(valid_items), batch_size):
            chunks.append(valid_items[i : i + batch_size])
    else:
        for item, text in valid_items:
            chunks.append([(item, text)])"""

    if diff_search in content:
        content = content.replace(diff_search, diff_replace)

    diff_search2 = """def call_grammar_llm(
    chunk: list[tuple[Any, str]],
    bcp47: str,
    ec: Any,
) -> tuple[list[Any], int]:
    \"\"\"Run grammar LLM for one sentence or a batch; return parsed results and elapsed ms.\"\"\""""

    diff_replace2 = """def call_grammar_llm(
    chunk: list[tuple[Any, str]],
    bcp47: str,
    ec: Any,
) -> tuple[list[Any], int]:
    \"\"\"Run grammar LLM for one sentence or a batch; return parsed results and elapsed ms.\"\"\"
    from . import grammar_proofread_locale
    max_chars = grammar_proofread_locale.grammar_max_chars(ec.ctx)
    chunk = [(item, text[:max_chars] if len(text) > max_chars else text) for item, text in chunk]"""

    if diff_search2 in content:
        content = content.replace(diff_search2, diff_replace2)

    with open(file_path, "w") as f:
        f.write(content)

def apply_fix_2():
    print("Fix 2: _ensure_persistence_bound binds the active document...")
    file_path = "plugin/writer/locale/ai_grammar_proofreader.py"
    with open(file_path, "r") as f:
        content = f.read()

    diff_search = """    model = get_document_model_for_id(ctx, doc_id)
    if model is None:
        model = get_active_document(ctx)
    get_persistence(ctx, doc_id, model=model)"""

    diff_replace = """    model = get_document_model_for_id(ctx, doc_id)
    if model is None:
        return
    get_persistence(ctx, doc_id, model=model)"""

    if diff_search in content:
        content = content.replace(diff_search, diff_replace)

    with open(file_path, "w") as f:
        f.write(content)

def apply_fix_3():
    print("Fix 3: apply_language_change retags the first document-wide occurrence...")
    file_path = "plugin/writer/locale/grammar_persistence.py"
    with open(file_path, "r") as f:
        content = f.read()

    # Step 1: Update signature
    diff_search1 = "def apply_language_change(ctx: Any, doc_id: str, sentence_text: str, detected_bcp47: str) -> None:"
    diff_replace1 = "def apply_language_change(ctx: Any, doc_id: str, sentence_text: str, detected_bcp47: str, start_pos: int = 0) -> None:"
    if diff_search1 in content:
        content = content.replace(diff_search1, diff_replace1)

    # Step 2: Implement exact span search
    diff_search2 = """        found_range = None
        if view_cursor:
            found_range = model.findNext(view_cursor.getStart(), search_desc)

        if not found_range:
            # Document-wide search from the start — view-cursor-relative findNext can miss
            # the sentence Writer just proofread when the caret is elsewhere.
            try:
                text_obj = model.getText()
                doc_start = text_obj.getStart()
                found_range = model.findNext(doc_start, search_desc)
            except Exception:
                found_range = model.findFirst(search_desc)

        if not found_range:
            found_range = model.findFirst(search_desc)"""

    diff_replace2 = """        found_range = None
        try:
            if start_pos > 0:
                text_obj = model.getText()
                doc_cursor = text_obj.createTextCursorByRange(text_obj.getStart())
                doc_cursor.goRight(start_pos, False)
                found_range = model.findNext(doc_cursor.getStart(), search_desc)
        except Exception:
            pass

        if not found_range and view_cursor:
            found_range = model.findNext(view_cursor.getStart(), search_desc)

        if not found_range:
            # Document-wide search from the start — view-cursor-relative findNext can miss
            # the sentence Writer just proofread when the caret is elsewhere.
            try:
                text_obj = model.getText()
                doc_start = text_obj.getStart()
                found_range = model.findNext(doc_start, search_desc)
            except Exception:
                found_range = model.findFirst(search_desc)

        if not found_range:
            found_range = model.findFirst(search_desc)"""

    if diff_search2 in content:
        content = content.replace(diff_search2, diff_replace2)

    with open(file_path, "w") as f:
        f.write(content)

    # We must also ensure n_start is propagated to apply_language_change
    # So we edit grammar_work_queue.py
    file_path = "plugin/writer/locale/grammar_work_queue.py"
    with open(file_path, "r") as f:
        content = f.read()

    diff_search = """    enqueue_seq: int
    original_bcp47: str = ""
    provider: str = ""
"""
    diff_replace = """    enqueue_seq: int
    original_bcp47: str = ""
    provider: str = ""
    n_start: int = 0
"""
    if diff_search in content:
        content = content.replace(diff_search, diff_replace)
    with open(file_path, "w") as f:
        f.write(content)

    # Edit ai_grammar_proofreader.py to pass n_start
    file_path = "plugin/writer/locale/ai_grammar_proofreader.py"
    with open(file_path, "r") as f:
        content = f.read()

    diff_search = """                    inflight_key=inflight_key,
                    enqueue_seq=seq,
                    provider=provider,
                )"""
    diff_replace = """                    inflight_key=inflight_key,
                    enqueue_seq=seq,
                    provider=provider,
                    n_start=sent_start,
                )"""
    if diff_search in content:
        content = content.replace(diff_search, diff_replace)
    with open(file_path, "w") as f:
        f.write(content)

    # Edit grammar_worker.py to pass n_start to apply_language_change
    file_path = "plugin/writer/locale/grammar_worker.py"
    with open(file_path, "r") as f:
        content = f.read()

    diff_search = """        if completion.apply_locale_after_success:
            for item, text in chunk:
                grammar_persistence.apply_language_change(ec.ctx, item.doc_id, text, bcp47)"""
    diff_replace = """        if completion.apply_locale_after_success:
            for item, text in chunk:
                grammar_persistence.apply_language_change(ec.ctx, item.doc_id, text, bcp47, start_pos=getattr(item, 'n_start', 0))"""
    if diff_search in content:
        content = content.replace(diff_search, diff_replace)
    with open(file_path, "w") as f:
        f.write(content)

def apply_test_patches():
    file_path = "tests/writer/locale/test_grammar_worker.py"
    with open(file_path, "r") as f:
        content = f.read()

    diff_search = """def test_worker_build_chunks_truncates_batch_and_single() -> None:
    long_a = "A" * 100
    long_b = "B" * 80
    batch_items = [
        (_item(long_a, seq=1, inflight_key="a"), long_a),
        (_item(long_b, seq=2, inflight_key="b"), long_b),
    ]
    chunks, _instr = _worker_build_chunks(
        batch_items, MagicMock(), batch_size=8, max_chars=10, detect_lang_enabled=False
    )
    assert len(chunks) == 1 and len(chunks[0]) == 2
    assert chunks[0][0][1] == "A" * 10
    assert chunks[0][1][1] == "B" * 10

    single_chunks, _instr = _worker_build_chunks(
        [(_item(long_a), long_a)], MagicMock(), batch_size=8, max_chars=10, detect_lang_enabled=False
    )
    assert len(single_chunks) == 1
    assert single_chunks[0][0][1] == "A" * 10"""

    diff_replace = """def test_worker_build_chunks_truncates_batch_and_single() -> None:
    long_a = "A" * 100
    long_b = "B" * 80
    batch_items = [
        (_item(long_a, seq=1, inflight_key="a"), long_a),
        (_item(long_b, seq=2, inflight_key="b"), long_b),
    ]
    chunks, _instr = _worker_build_chunks(
        batch_items, MagicMock(), batch_size=8, max_chars=10, detect_lang_enabled=False
    )
    assert len(chunks) == 1 and len(chunks[0]) == 2
    assert chunks[0][0][1] == "A" * 100
    assert chunks[0][1][1] == "B" * 80

    single_chunks, _instr = _worker_build_chunks(
        [(_item(long_a), long_a)], MagicMock(), batch_size=8, max_chars=10, detect_lang_enabled=False
    )
    assert len(single_chunks) == 1
    assert single_chunks[0][0][1] == "A" * 100"""
    if diff_search in content:
        content = content.replace(diff_search, diff_replace)


    diff_search2 = """        mock_apply.assert_called_once_with(ctx, "doc_1", "One mismatch.", "fr-FR")"""
    diff_replace2 = """        mock_apply.assert_called_once_with(ctx, "doc_1", "One mismatch.", "fr-FR", start_pos=0)"""
    content = content.replace(diff_search2, diff_replace2)

    diff_search3 = """            mock_apply.assert_called_once_with(ctx, "doc123", "\\u65e5\\u672c\\u8a9e\\u3067\\u66f8\\u3044\\u3066\\u3044\\u307e\\u3059\\u3002", "ja-JP")"""
    diff_replace3 = """            mock_apply.assert_called_once_with(ctx, "doc123", "\\u65e5\\u672c\\u8a9e\\u3067\\u66f8\\u3044\\u3066\\u3044\\u307e\\u3059\\u3002", "ja-JP", start_pos=0)"""
    content = content.replace(diff_search3, diff_replace3)

    with open(file_path, "w") as f:
        f.write(content)


    file_path = "tests/writer/locale/test_ai_grammar_proofreader.py"
    try:
        with open(file_path, "r") as f:
            content = f.read()

        diff_search = """                    inflight_key="key1",
                    enqueue_seq=mock.ANY,
                    provider="harper",
                )"""
        diff_replace = """                    inflight_key="key1",
                    enqueue_seq=mock.ANY,
                    provider="harper",
                    n_start=mock.ANY,
                )"""
        if diff_search in content:
            content = content.replace(diff_search, diff_replace)

        with open(file_path, "w") as f:
            f.write(content)
    except FileNotFoundError:
        pass


apply_fix_1()
apply_fix_2()
apply_fix_3()
apply_test_patches()
