# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Flag row scoring and the python→shapes harness hop (no soffice)."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

from eval_worlds import WriterWorld  # noqa: E402
from flag_eval import score_flag_example  # noqa: E402
from llm_chat_eval import (  # noqa: E402
    _dispatch_delegate_tool_domains,
    delegated_domain_schemas,
)
from string_eval_tools import dispatch_string_tool  # noqa: E402


def _odt(path: Path, *, stars: int) -> Path:
    parts: list[str] = []
    for idx in range(6):
        parts.append(
            f'<draw:rect draw:name="Stripe{idx}" svg:width="7.4807in" '
            f'svg:height="0.4724in" svg:x="0.2in" svg:y="{idx}.0in"/>'
        )
    for idx in range(stars):
        parts.append(
            f'<draw:custom-shape draw:name="Star{idx}" svg:width="0.3157in" '
            f'svg:height="0.3157in" svg:x="1in" svg:y="1in">'
            '<draw:enhanced-geometry draw:type="star5"/>'
            "</draw:custom-shape>"
        )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'office:version="1.2">\n'
        f"<office:body><office:text>{''.join(parts)}</office:text></office:body>\n"
        "</office:document-content>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("content.xml", content)
    return path


def _good_trace() -> list[dict[str, str]]:
    return [
        {
            "name": "delegate_to_specialized_writer_toolset",
            "arguments": json.dumps({"domain": "python", "task": "American flag"}),
        },
        {
            "name": "delegate_tool_domains",
            "arguments": json.dumps({"domains": ["shapes"], "task": "flag"}),
        },
        {
            "name": "run_venv_python_script",
            "arguments": "import writeragent as wa\nwa.shape.upsert('create', width=19001)",
        },
    ]


def test_string_backend_fails_closed_without_geometry(tmp_path: Path) -> None:
    missing = tmp_path / "nope.odt"
    scored = score_flag_example(_good_trace(), backend="string", odt_path=str(missing))
    assert scored.passed is False
    assert scored.partial_score == 0.0
    assert any("string simulator" in item for item in scored.failures)
    assert not missing.exists()


def test_lo_trace_and_odt_soft_stars_still_pass(tmp_path: Path) -> None:
    odt = _odt(tmp_path / "flag.odt", stars=3)
    scored = score_flag_example(_good_trace(), backend="lo", odt_path=str(odt))
    assert scored.passed is True
    assert scored.failures == []
    assert scored.soft
    assert scored.partial_score == 0.875
    assert odt.is_file()


def test_lo_trace_missing_venv_fails(tmp_path: Path) -> None:
    odt = _odt(tmp_path / "flag.odt", stars=20)
    trace = _good_trace()[:2]
    scored = score_flag_example(trace, backend="lo", odt_path=str(odt))
    assert scored.passed is False
    assert any("run_venv" in item for item in scored.failures)


def test_string_world_refuses_venv_and_domain_delegate() -> None:
    world = WriterWorld("")
    venv = dispatch_string_tool(world, "run_venv_python_script", "{}")
    assert "unsupported_in_eval" in venv
    refused = _dispatch_delegate_tool_domains(
        kind="writer",
        raw_args=json.dumps({"domains": ["shapes"], "task": "flag"}),
        state=world,
        client=None,
        endpoint="",
        api_key="",
        model="",
        backend="string",
        max_tokens=8,
        verbose=False,
        student="llm",
        usage_acc={},
        trace=[],
        schema_patches=None,
        tools_spec=None,
        schema_density="full",
        allow=True,
    )
    assert "unsupported_in_eval" in refused


def test_shapes_hop_exposes_venv_and_hides_shape_upsert() -> None:
    from eval_catalog import schema_tool_name

    rows = delegated_domain_schemas("writer", ["shapes"])
    names = {schema_tool_name(row) for row in rows}
    assert "run_venv_python_script" in names
    assert "shape_summary" in names
    assert "specialized_workflow_finished" in names
    assert "shape_upsert" not in names
    assert "delegate_tool_domains" not in names
    assert "delegate_to_specialized_writer_toolset" not in names
