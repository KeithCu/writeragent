# WriterAgent tests for scripts/eval_2_python_shapes_flag_oracle.py
from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_debug_log import DEBUG_LOG_FILENAME  # noqa: E402
from eval_2_headed import PYTHON_SHAPES_ODT_NAME  # noqa: E402
from eval_2_headed import main as headed_main  # noqa: E402
from eval_2_python_shapes_flag_oracle import (  # noqa: E402
    CHECK_COUNT,
    MIN_MAX_WIDTH_HMM,
    MIN_SHAPES,
    MIN_STARS,
    MIN_STRIPE_RECTS,
    FlagGeometry,
    length_to_hmm,
    main as oracle_main,
    parse_path_evidence,
    read_writer_flag,
    resolve_flag_artifact,
    score_artifact,
    score_flag,
)

_REPO = Path(__file__).resolve().parents[2]
_PROMPT = _REPO / "docs" / "eval" / "eval-2" / "python-shapes-flag" / "prompt.writeragent.txt"
_ASK = "use the python domain to make an American flag using shapes"

# Executed calls only. A logged few-shot "content" line must not count.
_PASS_LOG = "\n".join((
    "Tool call: delegate_to_specialized_writer_toolset("
    '{"domain": "python", "task": "American flag with shapes"})',
    "streaming_loop: accumulated tool_calls tool_calls=["
    '{"index": 0, "type": "function", "function": '
    '{"name": "delegate_tool_domains", "arguments": '
    '"{\\"domains\\": [\\"shapes\\"], \\"task\\": \\"flag\\"}"}}]',
    "SmolToolAdapter executing async tool 'run_venv_python_script' on worker",
    "create_shape snapshot [after_page_add]: size=(19001x1200)",
))
_FEW_SHOT_ONLY = (
    '"content": "example Action {\\"name\\": \\"delegate_tool_domains\\", '
    '\\"arguments\\": {\\"domains\\": [\\"shapes\\"]}} '
    'run_venv_python_script wa.shape.upsert"'
)


def _odt(
    path: Path,
    *,
    stripes: int = 6,
    stars: int = 20,
    stripe_w: str = "7.4807in",
    stripe_h: str = "0.4724in",
    star: str = "0.3157in",
    group: bool = False,
    extra: str = "",
) -> Path:
    parts: list[str] = []
    for idx in range(stripes):
        parts.append(
            f'<draw:rect draw:name="Stripe{idx}" svg:width="{stripe_w}" '
            f'svg:height="{stripe_h}" svg:x="0.2in" svg:y="{idx}.0in"/>'
        )
    for idx in range(stars):
        parts.append(
            f'<draw:custom-shape draw:name="Star{idx}" svg:width="{star}" '
            f'svg:height="{star}" svg:x="1in" svg:y="1in">'
            '<draw:enhanced-geometry draw:type="star5"/>'
            "</draw:custom-shape>"
        )
    body = "".join(parts) + extra
    if group:
        body = f'<draw:g draw:name="Flag">{body}</draw:g>'
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'office:version="1.2">\n'
        f"<office:body><office:text>{body}</office:text></office:body>\n"
        "</office:document-content>\n"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">\n'
        ' <manifest:file-entry manifest:full-path="/" manifest:media-type='
        '"application/vnd.oasis.opendocument.text"/>\n'
        ' <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>\n'
        "</manifest:manifest>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/manifest.xml", manifest)
        zf.writestr("content.xml", content)
    return path


def _run(dir_path: Path, log_text: str, **odt_kw: Any) -> Path:
    _odt(dir_path / "final_flag.odt", **odt_kw)
    (dir_path / DEBUG_LOG_FILENAME).write_text(log_text, encoding="utf-8")
    return dir_path


def test_prompt_is_the_exact_ask() -> None:
    text = _PROMPT.read_text(encoding="utf-8").strip()
    assert text == _ASK
    assert "run_venv" not in text
    assert "shape_upsert" not in text
    assert "delegate_tool_domains" not in text


def test_inch_export_rounds_to_pass_width() -> None:
    # LibreOffice Writer storeToURL: 19001 HMM → svg:width="7.4807in".
    assert length_to_hmm("7.4807in") == 19001
    assert length_to_hmm("19.001cm") == 19001
    assert length_to_hmm("0.7480in") == 1900
    assert length_to_hmm("19001") == 19001


def test_few_shot_text_is_not_execution() -> None:
    evidence = parse_path_evidence(_FEW_SHOT_ONLY)
    assert evidence.python_domain is False
    assert evidence.delegate_shapes is False
    assert evidence.run_venv is False
    assert evidence.script_placement is False


def test_passing_flag(tmp_path: Path) -> None:
    run = _run(tmp_path / "stamp", _PASS_LOG)
    result = score_artifact(run)
    assert result.passed, result.failures
    assert result.checks == CHECK_COUNT
    assert result.failures == []
    assert result.stripe_rects >= MIN_STRIPE_RECTS
    assert result.star_shapes >= MIN_STARS
    assert result.shape_count >= MIN_SHAPES
    assert result.max_width_hmm >= MIN_MAX_WIDTH_HMM
    assert result.max_width_hmm == 19001
    assert result.python_domain is True
    assert result.delegate_shapes is True
    assert result.run_venv is True
    assert result.script_placement is True
    assert result.grouped is False


def test_group_is_bonus_not_required(tmp_path: Path) -> None:
    loose = _run(tmp_path / "loose", _PASS_LOG)
    grouped = _run(tmp_path / "grouped", _PASS_LOG, group=True)
    loose_result = score_artifact(loose)
    grouped_result = score_artifact(grouped)
    assert loose_result.passed, loose_result.failures
    assert grouped_result.passed, grouped_result.failures
    assert loose_result.grouped is False
    assert grouped_result.grouped is True
    assert any("#927" in item for item in grouped_result.bonuses)


def test_speck_width_fails(tmp_path: Path) -> None:
    run = _run(tmp_path / "speck", _PASS_LOG, stripe_w="0.7480in", stripe_h="0.04in")
    result = score_artifact(run)
    assert not result.passed
    assert any("speck" in item for item in result.failures)
    assert result.max_width_hmm < MIN_MAX_WIDTH_HMM
    assert len(result.failures) < result.checks


def test_too_few_stars_fails() -> None:
    evidence = parse_path_evidence(_PASS_LOG)
    result = score_flag(
        FlagGeometry(shape_count=11, stripe_rects=6, star_shapes=5, max_width_hmm=19001, grouped=False),
        evidence,
    )
    assert not result.passed
    assert any("star-like" in item for item in result.failures)


def test_llm_shape_upsert_without_venv_fails() -> None:
    log = "\n".join((
        'Tool call: delegate_to_specialized_draw_toolset({"domain": "shapes", "task": "flag"})',
        'Tool call: shape_upsert({"action": "create", "shape_type": "rectangle", "width": 19001})',
    ))
    evidence = parse_path_evidence(log)
    assert evidence.llm_shape_upsert is True
    assert evidence.run_venv is False
    assert evidence.python_domain is False
    result = score_flag(
        FlagGeometry(30, 13, 50, 19001, False),
        evidence,
    )
    assert not result.passed
    assert any("LLM shape_upsert" in item for item in result.failures)
    assert any("python specialized" in item for item in result.failures)
    assert any("run_venv" in item for item in result.failures)


def test_images_png_path_fails_even_with_venv() -> None:
    log = "\n".join((
        _PASS_LOG,
        'Tool call: delegate_to_specialized_writer_toolset({"domain": "images", "task": "png flag"})',
        'Tool call: image_generate({"prompt": "american flag"})',
    ))
    evidence = parse_path_evidence(log)
    assert evidence.images_path is True
    assert evidence.run_venv is True
    result = score_flag(
        FlagGeometry(30, 13, 50, 19001, False),
        evidence,
    )
    assert not result.passed
    assert any("images" in item for item in result.failures)


def test_missing_log_fails_path_checks(tmp_path: Path) -> None:
    doc = _odt(tmp_path / "final_flag.odt")
    result = score_artifact(doc)
    assert not result.passed
    assert any("writeragent_debug.log missing" in item for item in result.failures)
    assert result.stripe_rects >= MIN_STRIPE_RECTS


def test_square_rect_is_not_a_stripe(tmp_path: Path) -> None:
    path = _odt(tmp_path / "squares.odt", stripes=0, stars=0, extra=(
        '<draw:rect draw:name="Canton" svg:width="3in" svg:height="2.5in"/>'
    ))
    geom = read_writer_flag(path)
    assert geom.stripe_rects == 0
    assert geom.shape_count == 1


def test_polygon_star_counts(tmp_path: Path) -> None:
    points = " ".join(f"{idx},{idx % 2}" for idx in range(10))
    path = _odt(
        tmp_path / "poly.odt",
        stripes=0,
        stars=0,
        extra=f'<draw:polygon draw:name="StarPoly" svg:width="0.3in" svg:height="0.3in" draw:points="{points}"/>',
    )
    geom = read_writer_flag(path)
    assert geom.star_shapes == 1


def test_resolve_prefers_final_flag_and_sibling_log(tmp_path: Path) -> None:
    _odt(tmp_path / "Untitled 1.odt", stripes=1, stars=0)
    preferred = _odt(tmp_path / "final_flag.odt")
    log_path = tmp_path / DEBUG_LOG_FILENAME
    log_path.write_text(_PASS_LOG, encoding="utf-8")
    doc, found_log = resolve_flag_artifact(tmp_path)
    assert doc == preferred
    assert found_log == log_path
    named = tmp_path / PYTHON_SHAPES_ODT_NAME
    assert resolve_flag_artifact(log_path)[0] == preferred
    named.write_bytes(preferred.read_bytes())
    # final_flag stays preferred when both names exist.
    assert resolve_flag_artifact(tmp_path)[0] == preferred


def test_headed_score_routes_to_flag_oracle(tmp_path: Path) -> None:
    run = _run(tmp_path / "stamp", _PASS_LOG)
    assert headed_main(["--task", "python-shapes-flag", "--score", str(run)]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert headed_main(["--task", "python-shapes-flag", "--score", str(empty)]) == 1


def test_venv_args_wa_shape_upsert_is_bonus() -> None:
    log = (
        "streaming_loop: accumulated tool_calls tool_calls=["
        '{"index": 0, "type": "function", "function": '
        '{"name": "run_venv_python_script", "arguments": '
        '"import writeragent as wa\\nwa.shape.upsert(\'create\')"}}]'
    )
    evidence = parse_path_evidence(log)
    assert evidence.run_venv is True
    assert evidence.script_placement is True
    assert evidence.llm_shape_upsert is False


def test_oracle_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _run(tmp_path / "stamp", _PASS_LOG)
    assert oracle_main(["--json", str(run)]) == 0
    out = capsys.readouterr().out
    assert '"passed": true' in out
    assert '"checks": 9' in out
