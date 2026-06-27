#!/usr/bin/env python3
# WriterAgent - inspect embeddings locale run parsing on disk.
"""Print locale-tagged text runs used for prose sentence breaking.

Uses the same extract path as folder indexing (ODF/OOXML span parsing,
per-paragraph langdetect for plain text). No venv embed worker required.

Examples:
  .venv/bin/python scripts/dump_embeddings_locale_runs.py
  .venv/bin/python scripts/dump_embeddings_locale_runs.py ~/Desktop/Writing
  .venv/bin/python scripts/dump_embeddings_locale_runs.py --file ~/Desktop/Writing/report.odt
  .venv/bin/python scripts/dump_embeddings_locale_runs.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from plugin.embeddings.embeddings_fs import (
    LocaleTextRun,
    extract_indexable_passage_runs,
    extract_indexable_passages,
    guess_indexable_paths,
    path_uses_prose_chunking,
)
from plugin.embeddings.embeddings_locale import resolve_document_locale_bcp47

DEFAULT_FOLDER = Path("~/Desktop/Writing")


def _run_slice(passage: str, run: LocaleTextRun) -> str:
    return passage[run.char_start : run.char_end]


def _summarize_runs(runs: list[LocaleTextRun] | list[dict[str, object]]) -> str:
    locales: set[str | None] = set()
    for run in runs:
        if isinstance(run, LocaleTextRun):
            locales.add(run.locale_bcp47)
        else:
            locales.add(run.get("locale_bcp47"))  # type: ignore[union-attr]
    locales.discard(None)
    if len(locales) <= 1:
        tag = next(iter(locales), None) or "(default)"
        return f"1 locale ({tag})"
    return f"{len(runs)} runs, {len(locales)} locales"


def _file_report(path: Path, *, max_paragraphs: int | None) -> dict[str, object]:
    norm = str(path.resolve())
    passages = extract_indexable_passages(norm)
    doc_default = resolve_document_locale_bcp47(norm, body_sample="\n".join(passages[:20]))
    passage_runs = extract_indexable_passage_runs(norm)

    paragraphs: list[dict[str, object]] = []
    multi_locale = 0
    for para_index, (passage, runs) in enumerate(passage_runs):
        locales = {run.locale_bcp47 for run in runs}
        if len(locales) > 1:
            multi_locale += 1
        paragraphs.append(
            {
                "para_index": para_index,
                "text": passage,
                "runs": [
                    {
                        "char_start": run.char_start,
                        "char_end": run.char_end,
                        "locale_bcp47": run.locale_bcp47,
                        "text": _run_slice(passage, run),
                    }
                    for run in runs
                ],
            }
        )

    if max_paragraphs is not None:
        paragraphs = paragraphs[:max_paragraphs]

    return {
        "path": norm,
        "name": path.name,
        "doc_default_locale_bcp47": doc_default,
        "paragraph_count": len(passage_runs),
        "multi_locale_paragraph_count": multi_locale,
        "paragraphs": paragraphs,
    }


def _print_text_report(report: dict[str, object]) -> None:
    print(f"=== {report['name']} ===")
    print(f"doc_default: {report['doc_default_locale_bcp47'] or '(none)'}")
    print(
        f"paragraphs: {report['paragraph_count']} "
        f"(mixed-locale: {report['multi_locale_paragraph_count']})"
    )
    for block in report["paragraphs"]:  # type: ignore[union-attr]
        para_index = block["para_index"]
        text = str(block["text"])
        preview = text if len(text) <= 120 else text[:117] + "..."
        print(f"\n[{para_index}] {_summarize_runs(block['runs'])}")  # type: ignore[arg-type]
        print(f"    {preview!r}")
        for run in block["runs"]:  # type: ignore[union-attr]
            locale = run["locale_bcp47"] or "(default)"
            snippet = str(run["text"])
            if len(snippet) > 80:
                snippet = snippet[:77] + "..."
            print(f"    [{run['char_start']}:{run['char_end']}] {locale}: {snippet!r}")


def _collect_paths(folder: Path, *, file_arg: Path | None) -> list[Path]:
    if file_arg is not None:
        path = file_arg.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Not a file: {path}")
        if not path_uses_prose_chunking(str(path)):
            raise ValueError(f"Not a prose index extension: {path.name}")
        return [path]

    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    return [
        Path(entry.path)
        for entry in guess_indexable_paths(str(folder))
        if path_uses_prose_chunking(entry.path)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump locale-tagged prose runs for embeddings sentence breaking",
    )
    parser.add_argument(
        "folder",
        nargs="?",
        type=Path,
        default=DEFAULT_FOLDER,
        help=f"Document folder to scan (default: {DEFAULT_FOLDER})",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Inspect one prose file instead of scanning a folder",
    )
    parser.add_argument(
        "--max-paragraphs",
        type=int,
        default=None,
        help="Limit paragraphs printed per file",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    folder = args.folder.expanduser().resolve()
    try:
        paths = _collect_paths(folder, file_arg=args.file)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not paths:
        target = args.file.expanduser() if args.file else folder
        print(f"No prose documents found under {target}", file=sys.stderr)
        return 1

    reports: list[dict[str, object]] = []
    errors: list[str] = []
    locale_counter: Counter[str] = Counter()
    multi_locale_files = 0

    for path in sorted(paths):
        try:
            report = _file_report(path, max_paragraphs=args.max_paragraphs)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        reports.append(report)
        if int(report["multi_locale_paragraph_count"]) > 0:
            multi_locale_files += 1
        for block in report["paragraphs"]:  # type: ignore[union-attr]
            for run in block["runs"]:  # type: ignore[union-attr]
                locale_counter[str(run["locale_bcp47"] or "(default)")] += 1

    if args.json:
        payload = {
            "folder": str(folder),
            "file_count": len(reports),
            "errors": errors,
            "locale_run_counts": dict(locale_counter),
            "files": reports,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for report in reports:
            _print_text_report(report)
            print()
        print(
            f"Scanned {len(reports)} prose file(s); "
            f"{multi_locale_files} with mixed-locale paragraph(s)."
        )
        if locale_counter:
            print("Run locales:", ", ".join(f"{k}={v}" for k, v in sorted(locale_counter.items())))
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)

    return 1 if errors and not reports else 0


if __name__ == "__main__":
    raise SystemExit(main())
