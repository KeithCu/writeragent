# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin.writer.image_placeholders."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# --- data-less images kept (get_content leaves the picture data out) ------------------

_FRAME = ('<div data-lo-style="Standard"><div style="float:left" class="graphic-fr1" id="Assinatura">'
          '<img style="height:1cm" alt="" src=""/></div><div style="clear:both; line-height:0;">&nbsp;</div>'
          'Pelotas.</div>')


def _graphics_doc(*names):

    model = MagicMock()
    graphics = model.getGraphicObjects.return_value
    graphics.hasByName.side_effect = lambda name: name in names
    model.getEmbeddedObjects.return_value.hasByName.return_value = False
    return model


def test_swap_image_placeholders_turns_each_named_frame_into_a_marker():
    from plugin.writer.image_placeholders import capture_placeholder_images

    model = _graphics_doc("Assinatura", "Selo")
    content = _FRAME + '<p>Texto <span class="graphic-fr2" id="Selo"><img src=""/></span> depois.</p>'
    swapped, kept = capture_placeholder_images(model, content)
    assert [k.name for k in kept] == ["Assinatura", "Selo"]
    assert "<img" not in swapped and "clear:both" not in swapped  # no stray empty paragraph either
    assert swapped.count(kept[0].marker) == 1 and "Pelotas." in swapped


def test_swap_image_placeholders_keeps_a_picture_inside_a_heading():
    # The export puts a <p> between the wrapper and the <img> when the picture sits in a heading;
    # the edit was refused as "an image without its data" before.
    from plugin.writer.image_placeholders import capture_placeholder_images

    content = ('<h1 data-lo-style="Heading1"><a id="a__1423bb33"><span/></a><span class="graphic-fr1" id="Image2">'
               '<p><img alt="" src=""/></p></span>AO DOUTO JUIZO</h1>')
    swapped, kept = capture_placeholder_images(_graphics_doc("Image2"), content)
    assert [k.name for k in kept] == ["Image2"]
    assert "<img" not in swapped and "<p>" not in swapped and swapped.count(kept[0].marker) == 1
    assert "AO DOUTO JUIZO</h1>" in swapped


def test_swap_image_placeholders_refuses_a_picture_it_cannot_keep():
    """Losing a signature silently is worse than failing before anything changed."""

    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.image_placeholders import capture_placeholder_images

    with pytest.raises(ToolExecutionError, match="'Assinatura'"):
        capture_placeholder_images(_graphics_doc(), _FRAME)
    with pytest.raises(ToolExecutionError, match="without its data"):
        capture_placeholder_images(_graphics_doc(), '<p>Novo <img src=""/> texto</p>')


@pytest.mark.parametrize("markup", [
    "<p>Novo <img src=''/> texto</p>",
    '<p>Novo <img src = ""> texto</p>',
    '<span id="Selo"><img src=\'\'/></span>',
])
def test_swap_image_placeholders_refuses_non_canonical_empty_src(markup):
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.image_placeholders import capture_placeholder_images

    with pytest.raises(ToolExecutionError, match="without its data"):
        capture_placeholder_images(_graphics_doc("Selo"), markup)


def test_swap_image_placeholders_leaves_other_content_alone():
    from plugin.writer.image_placeholders import capture_placeholder_images

    model = MagicMock()
    content = '<p>Sem imagem</p><img src="data:image/png;base64,AAAA"/>'
    assert capture_placeholder_images(model, content) == (content, [])
    model.getGraphicObjects.assert_not_called()


def _kept(anchor="AT_PARAGRAPH"):
    from types import SimpleNamespace

    from plugin.writer.image_placeholders import _KeptImage

    return _KeptImage("waXimg0", "Assinatura", MagicMock(), MagicMock(), SimpleNamespace(value=anchor),
                      MagicMock(), {"TextWrap": 1})


def test_restore_puts_a_copy_where_the_marker_is_when_the_original_went():
    from plugin.writer import image_placeholders

    model = MagicMock()
    found = model.findFirst.return_value
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True):
        image_placeholders._restore_image_placeholders(model, [_kept()])
    picture = model.createInstance.return_value
    found.getText.return_value.insertTextContent.assert_called_once_with(found, picture, True)
    picture.setPropertyValue.assert_called_with("TextWrap", 1)


def test_restore_only_drops_the_marker_when_the_original_stayed():
    """A search replace of a paragraph's text keeps the paragraph and its picture: no duplicate."""

    from plugin.writer import image_placeholders

    for anchor, deleted in (("AT_PARAGRAPH", False), ("AT_PAGE", True)):
        model = MagicMock()
        found = model.findFirst.return_value
        with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=deleted):
            image_placeholders._restore_image_placeholders(model, [_kept(anchor)])
        found.setString.assert_called_once_with("")
        found.getText.return_value.insertTextContent.assert_not_called()


def test_restore_image_placeholders_missing_marker_for_deleted_original_raises():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import image_placeholders

    model = MagicMock()
    model.findFirst.return_value = None
    kept = [_kept("AT_PARAGRAPH")]
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True):
        with pytest.raises(ToolExecutionError, match="'Assinatura'"):
            image_placeholders._restore_image_placeholders(model, kept)


def test_restore_image_placeholders_missing_marker_for_kept_original_does_not_raise():
    from plugin.writer import image_placeholders

    model = MagicMock()
    model.findFirst.return_value = None
    kept = [_kept("AT_PARAGRAPH")]
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=False):
        image_placeholders._restore_image_placeholders(model, kept)
    model.createInstance.assert_not_called()


def test_restore_image_placeholders_missing_marker_resolves_before_mutation():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import image_placeholders

    model = MagicMock()
    found1 = MagicMock()
    model.findFirst.side_effect = [found1, None]
    kept = [_kept("AT_PARAGRAPH"), _kept("AT_PARAGRAPH")]
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True):
        with pytest.raises(ToolExecutionError, match="'Assinatura'"):
            image_placeholders._restore_image_placeholders(model, kept)
    model.createInstance.assert_not_called()
    found1.setString.assert_not_called()


def test_deleted_by_the_edit():
    from types import SimpleNamespace

    from plugin.writer import image_placeholders

    gone = MagicMock()
    gone.getAnchor.side_effect = RuntimeError("disposed")
    assert image_placeholders._deleted_by_the_edit(MagicMock(), gone, "Picture 1") is True

    model = MagicMock()
    model.getPropertyValue.return_value = False  # review off: alive means still there
    assert image_placeholders._deleted_by_the_edit(model, MagicMock(), "Picture 1") is False

    # Review on: an inline picture whose anchor sits inside a pending Delete went with it.
    model = MagicMock()
    model.getPropertyValue.return_value = True
    original = MagicMock()
    original.getPropertyValue.return_value = SimpleNamespace(value="AS_CHARACTER")
    text = MagicMock()
    delete = MagicMock()
    delete.getPropertyValue.side_effect = lambda name: (
        "Delete" if name == "RedlineType" else SimpleNamespace(getText=lambda: text))
    text.compareRegionStarts.return_value = 1
    text.compareRegionEnds.return_value = 1
    model.getRedlines.return_value.hasElements.return_value = True
    model.getRedlines.return_value.getCount.return_value = 1
    enum = model.getRedlines.return_value.createEnumeration.return_value
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.side_effect = [delete]
    assert image_placeholders._deleted_by_the_edit(model, original, "Picture 1") is True


def test_deleted_by_the_edit_name_guard():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer import image_placeholders

    # 1. Name missing from doc.getGraphicObjects() is treated as deleted without calling getAnchor()
    model = MagicMock()
    model.getGraphicObjects.return_value.hasByName.return_value = False
    original = MagicMock()
    original.getAnchor.side_effect = AssertionError("getAnchor must not be called when name is missing")
    assert image_placeholders._deleted_by_the_edit(model, original, "Picture 1") is True
    original.getAnchor.assert_not_called()
    assert model.getGraphicObjects.return_value.hasByName.call_args.args == ("Picture 1",)

    # 2. Name present in doc.getGraphicObjects() proceeds to check anchor
    model = MagicMock()
    model.getGraphicObjects.return_value.hasByName.return_value = True
    model.getPropertyValue.return_value = False  # review off
    original = MagicMock()
    assert image_placeholders._deleted_by_the_edit(model, original, "Picture 1") is False
    original.getAnchor.assert_called_once()

    # 3. Non-disposal lookup failures are treated as deleted
    model = MagicMock()
    model.getGraphicObjects.side_effect = RuntimeError("collection lookup failed")
    original = MagicMock()
    original.getAnchor.side_effect = AssertionError("getAnchor must not be called on lookup failure")
    assert image_placeholders._deleted_by_the_edit(model, original, "Picture 1") is True
    original.getAnchor.assert_not_called()

    # 4. Disposal lookup failure is re-raised
    model = MagicMock()
    model.getGraphicObjects.side_effect = DocumentDisposedError("doc disposed")
    with pytest.raises(DocumentDisposedError):
        image_placeholders._deleted_by_the_edit(model, MagicMock(), "Picture 1")


def test_restore_image_placeholders_passes_name():
    from plugin.writer import image_placeholders

    model = MagicMock()
    kept = [_kept()]
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True) as mock_deleted:
        image_placeholders._restore_image_placeholders(model, kept)
    mock_deleted.assert_called_once_with(model, kept[0].original, kept[0].name)

    # Missing marker case:
    model = MagicMock()
    model.findFirst.return_value = None
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=False) as mock_deleted:
        image_placeholders._restore_image_placeholders(model, kept)
    mock_deleted.assert_called_once_with(model, kept[0].original, kept[0].name)



def test_capture_region_images_unwraps_prefixes_and_refuses_unknown_empty():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import image_placeholders

    obj = MagicMock()
    obj.getPropertyValue.side_effect = lambda p: MagicMock(value="AT_PARAGRAPH") if p == "AnchorType" else p
    graphics = MagicMock()
    graphics.hasByName.side_effect = lambda n: n == "Logo"
    graphics.getByName.return_value = obj
    model = MagicMock()
    model.getGraphicObjects.return_value = graphics
    text_obj = MagicMock()

    html = (
        "\n<!--Next 'div' is emulating the top height of a draw:frame.-->\n"
        '<div style="height:0.3cm;">&nbsp;</div>'
        "\n<!--Next 'div' is a draw:frame. -->\n"
        '<div id="Logo"><img src="" alt="L"/></div>'
        '<div style="clear:both;">\xa0</div>'
        '<img src="data:image/png;base64,AA==" alt="bare"/>'
    )
    swapped, images = image_placeholders.capture_region_images(model, text_obj, html)
    assert len(images) == 1
    assert images[0].name == "Logo"
    assert images[0].no_data is True
    assert "emulating the top height" not in swapped
    assert "clear:both" not in swapped
    assert f'<img name="{images[0].marker}" border="0"' in swapped
    assert "data:image/png;base64," in swapped
    assert '<img src="data:image/png;base64,AA==" alt="bare"/>' in swapped

    prefix_placeholder = (
        "\n<!--Next 'div' is emulating the top height of a draw:frame.-->\n"
        '<div style="height:0.3cm;">&nbsp;</div>'
        "\n<!--Next 'span' is a draw:frame. -->\n"
        '<span id="Logo"><img src="" alt="L"/></span>'
        '<div style="clear:both;">&nbsp;</div>'
    )
    assert image_placeholders._IMAGE_PLACEHOLDER_RE.sub("M", prefix_placeholder) == "M"

    with pytest.raises(ToolExecutionError, match="Missing"):
        image_placeholders.capture_region_images(model, text_obj, '<div id="Missing"><img src=""/></div>')


def test_restore_region_images_raises_on_missing_marker():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import image_placeholders

    graphics = MagicMock()
    graphics.hasByName.return_value = False
    model = MagicMock()
    model.getGraphicObjects.return_value = graphics
    item = image_placeholders._RegionImage("m1", "Logo", None, False, False)
    with pytest.raises(ToolExecutionError, match="was lost"):
        image_placeholders.restore_region_images(model, [item])


def test_capture_placeholder_images_refuses_link_inside_wrapper():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import image_placeholders

    obj = MagicMock()
    graphics = MagicMock()
    graphics.hasByName.return_value = True
    graphics.getByName.return_value = obj
    model = MagicMock()
    model.getGraphicObjects.return_value = graphics

    with pytest.raises(ToolExecutionError, match="without its data"):
        image_placeholders.capture_placeholder_images(
            model, '<div id="Logo"><a href="x"><img src=""/></a></div>'
        )


def test_restore_region_images_at_page_nodata_and_in_region_dispose():
    from plugin.writer import image_placeholders

    # AT_PAGE with no_data=True restores kept.graphic before setName.
    imported_page = MagicMock()
    graphics = MagicMock()
    graphics.hasByName.return_value = True
    graphics.getByName.return_value = imported_page
    model = MagicMock()
    model.getGraphicObjects.return_value = graphics

    kept_page = image_placeholders._KeptImage(
        "m_page", "PageLogo", MagicMock(), "ORIG_GFX", MagicMock(value="AT_PAGE"), MagicMock(), {}
    )
    image_placeholders.restore_region_images(
        model, [image_placeholders._RegionImage("m_page", "PageLogo", kept_page, True, False)]
    )
    assert imported_page.Graphic == "ORIG_GFX"
    imported_page.setName.assert_called_once_with("PageLogo")

    # in_region=True when _deleted_by_the_edit is False disposes imported copy.
    imported_in = MagicMock()
    graphics.getByName.return_value = imported_in
    kept_in = image_placeholders._KeptImage(
        "m_in", "InLogo", MagicMock(), "G", MagicMock(value="AT_PARAGRAPH"), MagicMock(), {}
    )
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=False):
        image_placeholders.restore_region_images(
            model, [image_placeholders._RegionImage("m_in", "InLogo", kept_in, False, True)]
        )
    imported_in.dispose.assert_called_once()


def test_restore_image_placeholders_updates_hyperlink_from_marker():
    from plugin.writer import image_placeholders

    def _run(kept_image, marker_props):
        model = MagicMock()
        found = MagicMock()
        found.getPropertyValue.side_effect = lambda k: marker_props.get(k, "")
        with (
            patch.object(image_placeholders, "_find_marker", return_value=found),
            patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True),
            patch.object(image_placeholders, "_insert_copy") as ins,
        ):
            image_placeholders._restore_image_placeholders(model, [kept_image])
        return found, ins.call_args.kwargs["props"]

    # Originally linked and URL changed: name and target kept.
    kept_changed = image_placeholders._KeptImage(
        "m1", "Pic1", MagicMock(), "G1", MagicMock(value="AT_PARAGRAPH"), MagicMock(),
        {"HyperLinkURL": "https://old.example", "HyperLinkName": "nm", "HyperLinkTarget": "_blank"},
    )
    _, props = _run(kept_changed, {"HyperLinkURL": "https://new.example"})
    assert props["HyperLinkURL"] == "https://new.example"
    assert props["HyperLinkName"] == "nm"
    assert props["HyperLinkTarget"] == "_blank"

    # Originally linked and marker URL empty: all three cleared.
    _, props = _run(kept_changed, {"HyperLinkURL": ""})
    assert props["HyperLinkURL"] == ""
    assert props["HyperLinkName"] == ""
    assert props["HyperLinkTarget"] == ""

    # linked=True and originally unlinked: all three taken from marker, marker link cleared.
    kept_wrapped = image_placeholders._KeptImage(
        "m2", "Pic2", MagicMock(), "G2", MagicMock(value="AT_PARAGRAPH"), MagicMock(),
        {}, linked=True,
    )
    found, props = _run(
        kept_wrapped,
        {"HyperLinkURL": "https://new.example", "HyperLinkName": "n2", "HyperLinkTarget": "_blank"},
    )
    assert props["HyperLinkURL"] == "https://new.example"
    assert props["HyperLinkName"] == "n2"
    assert props["HyperLinkTarget"] == "_blank"
    found.setPropertyValue.assert_called_once_with("HyperLinkURL", "")

    # Not linked and originally unlinked: link props untouched.
    kept_unlinked = image_placeholders._KeptImage(
        "m3", "Pic3", MagicMock(), "G3", MagicMock(value="AT_PARAGRAPH"), MagicMock(),
        {},
    )
    found, props = _run(kept_unlinked, {"HyperLinkURL": "https://text.example"})
    assert "HyperLinkURL" not in props
    found.setPropertyValue.assert_not_called()


@pytest.mark.parametrize(("markup", "want_linked", "check_clean"), [
    (
        '<a href="https://a/1">\n<!--Next \'div\' is a draw:frame. -->\n'
        '<div id="Pic1"><img src=""/></div><div style="clear:both;">&nbsp;</div></a>',
        [True], False,
    ),
    (
        '\n<!--Next \'div\' is a draw:frame. -->\n<a href="https://a/2">'
        '<div id="Pic1"><img src=""/></div></a><div style="clear:both;">&nbsp;</div>',
        [True], True,
    ),
    (
        '\n<!--Next \'span\' is a draw:frame. -->\n<a href="https://a/3" target="_blank">'
        '<span id="Pic1"><img src=""/></span></a>',
        [True], True,
    ),
    ("<a href='https://a/4'><div id=\"Pic1\"><img src=\"\"/></div></a>", [True], False),
    (
        '<a href="https://outer"><a href="https://inner"><div id="Pic1"><img src=""/></div></a></a>',
        [True], False,
    ),
    (
        '<a href="https://a/5"><span id="Pic1"><img src=""/></span><span id="Pic2"><img src=""/></span></a>',
        [True, True], False,
    ),
    ('<a href="https://t/1">before <span id="Pic1"><img src=""/></span> after</a>', [False], False),
    ('<a href="https://t/2"><span id="Pic1"><img src=""/></span> after</a>', [False], False),
    ('<a name="bm"><div id="Pic1"><img src=""/></div></a>', [False], False),
    ('<a data-href="https://t/3"><div id="Pic1"><img src=""/></div></a>', [False], False),
])
def test_capture_placeholder_images_detects_picture_links(markup, want_linked, check_clean):
    from plugin.writer.image_placeholders import capture_placeholder_images

    swapped, kept = capture_placeholder_images(_graphics_doc("Pic1", "Pic2"), markup)
    assert [k.linked for k in kept] == want_linked
    if check_clean:
        assert "draw:frame" not in swapped
        assert "clear:both" not in swapped


def test_capture_placeholder_images_keeps_horizontal_space_before_closing_a():
    from plugin.writer.image_placeholders import capture_placeholder_images

    swapped_space, kept_space = capture_placeholder_images(
        _graphics_doc("Pic1"),
        '<a href="https://a/6"><span id="Pic1"><img src=""/></span> </a>caption',
    )
    assert [k.linked for k in kept_space] == [True]
    assert swapped_space.endswith("</a> caption")

    swapped_nl, kept_nl = capture_placeholder_images(
        _graphics_doc("Pic1"),
        '<a href="https://a/7"><span id="Pic1"><img src=""/></span>\n</a>caption',
    )
    assert [k.linked for k in kept_nl] == [True]
    assert swapped_nl.endswith("</a>caption")

    swapped_crlf, kept_crlf = capture_placeholder_images(
        _graphics_doc("Pic1"),
        '<a href="https://a/8"><span id="Pic1"><img src=""/></span>\r\n</a>caption',
    )
    assert [k.linked for k in kept_crlf] == [True]
    assert swapped_crlf.endswith("</a>caption")


def _prop_obj(props: dict[str, str]):
    obj = MagicMock()
    obj.getPropertyValue.side_effect = lambda name: props.get(name, "")
    return obj


def test_restore_image_placeholders_updates_surviving_picture_links():
    from plugin.writer.image_placeholders import _KeptImage, _restore_image_placeholders

    def _run(orig_props: dict[str, str], found_props: dict[str, str], *, linked: bool):
        orig = _prop_obj(orig_props)
        found = _prop_obj(found_props)
        model = MagicMock()
        model.findFirst.return_value = found
        kept = _KeptImage("wa1img0", "Pic1", orig, MagicMock(), MagicMock(), MagicMock(), dict(orig_props), linked=linked)
        with patch("plugin.writer.image_placeholders._deleted_by_the_edit", return_value=False):
            _restore_image_placeholders(model, [kept])
        return dict(orig.setPropertyValue.call_args_list[i].args for i in range(orig.setPropertyValue.call_count))

    # Linked survivor: updates URL and keeps target.
    assert _run(
        {"HyperLinkURL": "https://old", "HyperLinkName": "", "HyperLinkTarget": "_blank"},
        {"HyperLinkURL": "https://new", "HyperLinkName": "", "HyperLinkTarget": ""},
        linked=True,
    ) == {"HyperLinkURL": "https://new"}

    # Removed <a>: clears URL, name, and target.
    assert _run(
        {"HyperLinkURL": "https://old", "HyperLinkName": "nm", "HyperLinkTarget": "_blank"},
        {"HyperLinkURL": "", "HyperLinkName": "", "HyperLinkTarget": ""},
        linked=False,
    ) == {"HyperLinkURL": "", "HyperLinkName": "", "HyperLinkTarget": ""}

    # Unlinked picture inside a text link: untouched.
    assert _run(
        {"HyperLinkURL": "", "HyperLinkName": "", "HyperLinkTarget": ""},
        {"HyperLinkURL": "https://text", "HyperLinkName": "", "HyperLinkTarget": ""},
        linked=False,
    ) == {}


def test_restore_region_images_keeps_target_on_survivor_and_reinsert():
    from plugin.writer.image_placeholders import _KeptImage, _RegionImage, restore_region_images

    orig_props = {"HyperLinkURL": "https://old", "HyperLinkName": "", "HyperLinkTarget": "_blank"}
    orig = _prop_obj(orig_props)
    imported = _prop_obj({"HyperLinkURL": "https://new", "HyperLinkName": "", "HyperLinkTarget": "", "Title": "T"})
    model = MagicMock()
    graphics = model.getGraphicObjects.return_value
    graphics.hasByName.return_value = True
    graphics.getByName.return_value = imported

    kept = _KeptImage("wa1img0", "Pic1", orig, MagicMock(), MagicMock(), MagicMock(), dict(orig_props))
    region_img = _RegionImage("wa1img0", "Pic1", kept, True, True)

    with patch("plugin.writer.image_placeholders._deleted_by_the_edit", return_value=False):
        restore_region_images(model, [region_img])
    assert dict(c.args for c in orig.setPropertyValue.call_args_list) == {"HyperLinkURL": "https://new"}

    with patch("plugin.writer.image_placeholders._deleted_by_the_edit", return_value=True), \
         patch("plugin.writer.image_placeholders._insert_copy") as ins:
        restore_region_images(model, [region_img])
    assert ins.call_args.kwargs["props"]["HyperLinkURL"] == "https://new"
    assert ins.call_args.kwargs["props"]["HyperLinkTarget"] == "_blank"


def test_capture_placeholder_images_refuses_embedded_object_with_helpful_message():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.image_placeholders import capture_placeholder_images

    model = _graphics_doc()
    model.getEmbeddedObjects.return_value.hasByName.side_effect = lambda name: name == "Object1"
    with pytest.raises(ToolExecutionError, match="embedded object") as exc_info:
        capture_placeholder_images(model, '<span id="Object1"><img src=""/></span>')
    assert "'Object1'" in str(exc_info.value)
    assert "target='search'" in str(exc_info.value)
