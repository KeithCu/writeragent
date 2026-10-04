import zipfile
from pathlib import Path
from plugin.embeddings.venv import embeddings_ooxml_extract as ooxml

def test_extract_pptx_passages_fallback_order(tmp_path: Path):
    pptx = tmp_path / "deck3.pptx"
    with zipfile.ZipFile(pptx, "w") as zf:
        zf.writestr("ppt/slides/slide10.xml", '''<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide 10 text</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>''')

        zf.writestr("ppt/slides/slide2.xml", '''<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide 2 text</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>''')

    passages = ooxml.extract_pptx_passages(str(pptx))
    assert passages == [
        "[Slide: Slide1]\tSlide 2 text",
        "[Slide: Slide2]\tSlide 10 text",
    ]
