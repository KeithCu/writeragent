import re

with open('plugin/doc/document_helpers.py', 'r') as f:
    content = f.read()

# Insert _writer_tree_service, _unresolved_locator, and _dispatch_writer_locator above resolve_locator

insert_str = """
# Resolver-only TreeService used when plugin.main has not registered writer_tree
# (unit tests, and any call before WriterModule.initialize). One instance so
# repeated misses do not subscribe a new cache listener each time.
_FALLBACK_WRITER_TREE: Any = None


def _unresolved_locator(locator: str) -> ToolExecutionError:
    \"\"\"Locator string that is not ``type:value``.\"\"\"
    return ToolExecutionError(
        "Cannot resolve locator '%s'. Use type:value such as paragraph:N, "
        "heading:1.2, chapter_number:3.1, bookmark:NAME, heading_text:Title, "
        "section:NAME, or page:N." % locator
    )


def _writer_tree_service() -> Any:
    \"\"\"Return the process TreeService, or one resolver-only fallback.

    The registered service shares the heading-tree cache and bookmark map
    with navigation. The fallback exists so a locator still resolves when
    this function runs outside bootstrap (pytest, or before the writer
    module loads). Its document service is a plain DocumentService.
    \"\"\"
    import sys

    global _FALLBACK_WRITER_TREE

    main_mod = sys.modules.get("plugin.main")
    services = getattr(main_mod, "_services", None) if main_mod is not None else None
    if services is not None:
        getter = getattr(services, "get", None)
        if callable(getter):
            tree = getter("writer_tree")
            if tree is not None and hasattr(tree, "resolve_writer_locator"):
                return tree

    if _FALLBACK_WRITER_TREE is not None:
        return _FALLBACK_WRITER_TREE

    from types import SimpleNamespace

    from plugin.writer.tree import TreeService

    class _Events:
        def subscribe(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _Bookmarks:
        def get_mcp_bookmark_map(self, _doc: Any) -> dict[Any, Any]:
            return {}

    _FALLBACK_WRITER_TREE = TreeService(
        SimpleNamespace(
            document=DocumentService(),
            writer_bookmarks=_Bookmarks(),
            events=_Events(),
        )
    )
    return _FALLBACK_WRITER_TREE


def _dispatch_writer_locator(model: Any, loc_type: str, loc_value: str) -> dict[str, Any]:
    \"\"\"Resolve a locator ``TreeService.resolve_writer_locator`` owns.

    What was wrong: ``heading_text:``, ``section:``, ``page:``, and a bookmark
    name that was missing or already deleted fell out of ``resolve_locator``
    as paragraph 0. A bookmark that still had a name but whose anchor could
    not be placed did the same, because the live ``bookmark:`` branch called
    ``find_paragraph_for_range`` and returned its fallback 0. Navigation,
    ``get_page_objects``, and ``clone_heading_block`` then changed the first
    paragraph and returned success. ``TreeService.resolve_writer_locator``
    already rejected a missing name, but the live branch never called it.
    Why: every ``bookmark:`` goes through that resolver, which rejects an
    anchor that does not land on a paragraph. ``ValueError`` (``page:abc``
    fails ``int()`` before the resolver's own error) becomes
    ``ToolExecutionError`` because the tool registry re-raises ``ValueError``
    as a programmer error. A result with no paragraph index is an error, not
    paragraph 0.
    \"\"\"
    tree = _writer_tree_service()
    try:
        resolved = tree.resolve_writer_locator(model, loc_type, loc_value)
    except ToolExecutionError:
        raise
    except ValueError as exc:
        raise ToolExecutionError("Cannot resolve %s:%s — %s" % (loc_type, loc_value, exc)) from exc
    if not isinstance(resolved, dict) or not isinstance(resolved.get("para_index"), int):
        raise ToolExecutionError("Cannot resolve %s:%s" % (loc_type, loc_value))
    return resolved


"""

old_func = """def resolve_locator(model: Any, locator: str) -> dict[str, int]:
    \"\"\"Resolve a locator string to a paragraph index or other document position.

    Broader than bookmarks: ``paragraph:``, ``heading:``, ``chapter_number:``,
    and ``bookmark:``. Left here because ``plugin.writer.specialized.bookmarks``
    only owns bookmark tools. ``heading:`` is sibling-ordinal path;
    ``chapter_number:`` is the Chapter Numbering paint label.
    \"\"\"
    loc_type, sep, loc_value = locator.partition(":")
    if not sep:
        return {"para_index": 0}

    if loc_type == "paragraph":
        return {"para_index": int(loc_value)}

    if loc_type == "heading":
        parts = []
        try:
            parts = [int(p) for p in loc_value.split(".")]
        except Exception:
            logging.getLogger(__name__).exception("resolve_locator heading parse error")
            return {"para_index": 0}

        tree = _text_helpers.build_heading_tree(model)
        node: _text_helpers.HeadingTreeNode = tree
        for part in parts:
            children = node["children"]
            if 1 <= part <= len(children):
                node = children[part - 1]
            else:
                break
        return {"para_index": node["para_index"]}

    if loc_type == "chapter_number":
        tree = _text_helpers.build_heading_tree(model)
        found = _text_helpers.find_heading_by_chapter_number(tree, loc_value)
        if found is None:
            raise ToolExecutionError(
                "No heading with chapter_number:%s. Chapter Numbering may be off "
                "(writer_tree omits chapter_number when the outline label is empty), "
                "or no heading has that label. heading: is the sibling-ordinal path, "
                "not the chapter label." % loc_value
            )
        return {"para_index": found["para_index"]}

    if loc_type == "bookmark":
        if hasattr(model, "getBookmarks"):
            bms = model.getBookmarks()
            if bms.hasByName(loc_value):
                anchor = bms.getByName(loc_value).getAnchor()
                para_ranges = _get_paragraph_ranges(model)
                return {"para_index": _find_paragraph_for_range(anchor, para_ranges, model.getText())}

    return {"para_index": 0}"""

new_func = """def resolve_locator(model: Any, locator: str) -> dict[str, Any]:
    \"\"\"Resolve a locator string to a paragraph index.

    ``paragraph:``, ``heading:`` (sibling-ordinal), and ``chapter_number:``
    (Chapter Numbering paint label) are resolved here. Every ``bookmark:``
    goes to ``TreeService.resolve_writer_locator``, including a name that
    still exists. ``heading_text:``, ``section:``, ``page:``, and any other
    writer locator go there too. A missing bookmark, or a bookmark whose
    anchor does not land on a paragraph, raises ``ToolExecutionError``.
    \"\"\"
    loc_type, sep, loc_value = locator.partition(":")
    if not sep or not loc_type:
        raise _unresolved_locator(locator)

    if loc_type == "paragraph":
        try:
            return {"para_index": int(loc_value)}
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("Cannot resolve paragraph:%s" % loc_value) from exc

    if loc_type == "heading":
        try:
            parts = [int(p) for p in loc_value.split(".")]
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError(
                "Cannot resolve heading:%s — use a sibling-ordinal path such as heading:1.2."
                % loc_value
            ) from exc

        tree = _text_helpers.build_heading_tree(model)
        node: _text_helpers.HeadingTreeNode = tree
        for part in parts:
            children = node["children"]
            if 1 <= part <= len(children):
                node = children[part - 1]
            elif len(children) > 0:
                node = children[-1]
            else:
                break
        return {"para_index": node["para_index"]}

    if loc_type == "chapter_number":
        tree = _text_helpers.build_heading_tree(model)
        found = _text_helpers.find_heading_by_chapter_number(tree, loc_value)
        if found is None:
            raise ToolExecutionError(
                "No heading with chapter_number:%s. Chapter Numbering may be off "
                "(writer_tree omits chapter_number when the outline label is empty), "
                "or no heading has that label. heading: is the sibling-ordinal path, "
                "not the chapter label." % loc_value
            )
        return {"para_index": found["para_index"]}

    # bookmark: is not resolved here. The old branch returned
    # find_paragraph_for_range's fallback 0 when the anchor could not be
    # placed, so a stale name still navigated to the first paragraph.
    # TreeService.resolve_writer_locator rejects that anchor.
    return _dispatch_writer_locator(model, loc_type, loc_value)"""

content = content.replace(old_func, insert_str + new_func)

with open('plugin/doc/document_helpers.py', 'w') as f:
    f.write(content)
