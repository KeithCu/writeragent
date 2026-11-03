from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("writer")
def test_probe_para_style_and_indent(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setString("Hello paragraph world\n")

    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)

    # Read initial
    val0 = cursor.getPropertyValue("ParaLeftMargin")
    print(f"DEBUG: initial ParaLeftMargin={val0}")

    # Set direct indent
    cursor.setPropertyValue("ParaLeftMargin", 1500)
    val1 = cursor.getPropertyValue("ParaLeftMargin")
    print(f"DEBUG: after setting 1500: ParaLeftMargin={val1}")

    cur_style = cursor.getPropertyValue("ParaStyleName")
    print(f"DEBUG: cur_style={cur_style}")

    # Re-apply the SAME style name
    cursor.setPropertyValue("ParaStyleName", cur_style)
    val2 = cursor.getPropertyValue("ParaLeftMargin")
    print(f"DEBUG: after setting same ParaStyleName: ParaLeftMargin={val2}")

    # Set direct indent again
    cursor.setPropertyValue("ParaLeftMargin", 2500)
    print(f"DEBUG: after setting 2500: ParaLeftMargin={cursor.getPropertyValue('ParaLeftMargin')}")

    # Apply DIFFERENT style name e.g. "Heading 1"
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    print(f"DEBUG: after setting Heading 1: ParaLeftMargin={cursor.getPropertyValue('ParaLeftMargin')}")


@native_test
@with_native_doc("writer")
def test_probe_char_direct_on_same_style(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setString("Second paragraph with bold\n")
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)

    print(f"DEBUG char init: Weight={cursor.getPropertyValue('CharWeight')}, Height={cursor.getPropertyValue('CharHeight')}")

    # Set CharWeight direct override on cursor
    cursor.setPropertyValue("CharWeight", 150.0)  # Bold
    cursor.setPropertyValue("CharHeight", 18.0)  # 18pt
    print(f"DEBUG char after direct: Weight={cursor.getPropertyValue('CharWeight')}, Height={cursor.getPropertyValue('CharHeight')}")

    cur_style = cursor.getPropertyValue("ParaStyleName")
    # Re-apply same style
    cursor.setPropertyValue("ParaStyleName", cur_style)
    print(f"DEBUG char after same style: Weight={cursor.getPropertyValue('CharWeight')}, Height={cursor.getPropertyValue('CharHeight')}")

    # Now test on a SUB-RANGE (portion)
    cursor.gotoStart(False)
    cursor.goRight(5, True)  # first 5 chars
    cursor.setPropertyValue("CharWeight", 150.0)  # Bold on portion
    print(f"DEBUG portion after direct: Weight={cursor.getPropertyValue('CharWeight')}")

    # Now select whole paragraph and set same style
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)
    cursor.setPropertyValue("ParaStyleName", cur_style)

    # Now check the portion
    cursor.gotoStart(False)
    cursor.goRight(5, True)
    print(f"DEBUG portion after para same style: Weight={cursor.getPropertyValue('CharWeight')}")


@native_test
@with_native_doc("writer")
def test_probe_paragraph_enumeration(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setString("Paragraph with normal and bold text\n")
    cursor.gotoStart(False)
    cursor.goRight(10, False)
    cursor.goRight(6, True)
    cursor.setPropertyValue("CharWeight", 150.0)  # "normal" bolded

    # Get the paragraph object
    doc_enum = text.createEnumeration()
    para = doc_enum.nextElement()

    para_enum = para.createEnumeration()
    portions = []
    while para_enum.hasMoreElements():
        p = para_enum.nextElement()
        portions.append((p.getString(), getattr(p, "TextPortionType", None)))
    print(f"DEBUG: para_enum elements: {portions}")

    # Now test get_string_without_tracked_deletions on para
    from plugin.doc.text_helpers import get_string_without_tracked_deletions

    s = get_string_without_tracked_deletions(para)
    print(f"DEBUG: get_string_without_tracked_deletions(para) repr: {repr(s)}")


@native_test
@with_native_doc("writer")
def test_probe_header_text_leftover_when_off(ctx, doc):
    """Does LO keep HeaderText when HeaderIsOn=False? (2.3 region-off scan)."""
    styles = doc.getStyleFamilies().getByName("PageStyles")
    st = styles.getByName("Standard")
    st.setPropertyValue("HeaderIsOn", True)
    text_obj = st.getPropertyValue("HeaderText")
    text_obj.setString("Leftover header text")
    st.setPropertyValue("HeaderIsOn", False)
    try:
        leftover = st.getPropertyValue("HeaderText")
        if leftover is None:
            print("DEBUG: HeaderText while off = None")
        else:
            print(f"DEBUG: HeaderText while off = {repr(leftover.getString())}")
    except Exception as e:
        print(f"DEBUG: getPropertyValue(HeaderText) while off raised: {type(e).__name__}: {e}")


@native_test
@with_native_doc("writer")
def test_probe_header_leftover_survives_off_on(ctx, doc):
    """Does LO retain HeaderText through an off->on cycle? (2.3 premise check)."""
    styles = doc.getStyleFamilies().getByName("PageStyles")
    st = styles.getByName("Standard")
    st.setPropertyValue("HeaderIsOn", True)
    text_obj = st.getPropertyValue("HeaderText")
    text_obj.setString("Leftover header text")
    print(f"DEBUG: HeaderText while on (after setString) = {repr(text_obj.getString())}")
    st.setPropertyValue("HeaderIsOn", False)
    st.setPropertyValue("HeaderIsOn", True)
    text_obj2 = st.getPropertyValue("HeaderText")
    print(f"DEBUG: HeaderText after off->on = {repr(text_obj2.getString() if text_obj2 else None)}")


@native_test
@with_native_doc("writer")
def test_probe_apply_same_style_para_wide_font(ctx, doc):
    """Current apply_paragraph_style_preserving_direct_char on a paragraph-wide font
    (same style, clear_direct='none'): does it re-introduce the old font?"""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setString("Whole paragraph font test\n")
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)
    cursor.setPropertyValue("CharWeight", 150.0)  # paragraph-wide direct bold

    from plugin.writer.styles import apply_paragraph_style_preserving_direct_char

    report = apply_paragraph_style_preserving_direct_char(doc, cursor, "Standard", clear_direct="none")
    print(f"DEBUG: report={report}")
    chk = text.createTextCursor()
    chk.gotoStart(False)
    chk.gotoEndOfParagraph(True)
    print(f"DEBUG: after same-style none apply CharWeight={chk.getPropertyValue('CharWeight')}")


@native_test
@with_native_doc("writer")
def test_probe_portion_sees_paragraph_char(ctx, doc):
    """Does a paragraph-wide direct Char* show up on each portion's getPropertyValue?
    Determines whether _capture_direct_char_overrides sees a paragraph-level font override."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setString("Whole paragraph font test\n")
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)
    cursor.setPropertyValue("CharWeight", 150.0)  # paragraph-wide direct
    para = text.createEnumeration().nextElement()
    pe = para.createEnumeration()
    while pe.hasMoreElements():
        p = pe.nextElement()
        try:
            w = p.getPropertyValue("CharWeight")
        except Exception as e:
            w = f"ERR {type(e).__name__}"
        print(f"DEBUG: portion {p.getString()!r} CharWeight={w}")
