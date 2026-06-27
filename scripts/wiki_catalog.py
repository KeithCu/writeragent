"""Karpathy llm-wiki maintenance: rebuild index from wiki pages; use repo docs/ as sources (no raw copy)."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

MIRROR_MARKER = "source_sha256:"


@dataclass
class WikiPage:
    section: str
    slug: str
    title: str
    summary: str


@dataclass
class Report:
    wiki_pages: int = 0
    mirrors_removed: int = 0
    raw_removed: int = 0
    index_written: bool = False
    docs_files: int = 0


def docs_source_href(filename: str) -> str:
    """Frontmatter / link path from wiki/ to canonical repo doc (Layer 1, no copy)."""
    return f"../docs/{filename}"


def read_page_meta(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta


def is_mirror_page(path: Path) -> bool:
    if not path.is_file():
        return False
    head = path.read_text(encoding="utf-8")[:2500]
    if MIRROR_MARKER in head:
        return True
    return "raw/articles/" in head and "type: concept" in head


def list_wiki_pages(wiki_dir: Path) -> list[WikiPage]:
    pages: list[WikiPage] = []
    for section in ("entities", "concepts", "comparisons", "queries"):
        folder = wiki_dir / section
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if is_mirror_page(path):
                continue
            meta = read_page_meta(path)
            slug = path.stem
            title = meta.get("title", slug.replace("-", " "))
            summary = meta.get("summary", title)
            pages.append(WikiPage(section, slug, title, summary))
    return pages


def build_index_content(pages: list[WikiPage], today: str, *, docs_dir: Path | None) -> str:
    total = len(pages)
    lines = [
        "# Wiki Index",
        "",
        "> Content catalog. Every wiki page listed under its type with a one-line summary.",
        "> Read this first to find relevant pages for any query.",
        f"> Last updated: {today} | Total pages: {total}",
    ]
    if docs_dir is not None:
        lines.append(
            f"> Repo sources: read markdown directly under `{docs_dir.name}/` "
            f"(use `{docs_source_href('topic.md')}` in frontmatter — do not copy into `raw/articles/`)."
        )
    lines.append("")
    for section in ("entities", "concepts", "comparisons", "queries"):
        lines.append(f"## {section.capitalize()}")
        if section == "entities":
            lines.append("<!-- Alphabetical within section -->")
        section_pages = [p for p in pages if p.section == section]
        if not section_pages:
            lines.append("")
            continue
        lines.append("")
        for page in section_pages:
            lines.append(f"- [[{page.slug}]] – {page.summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def append_log(wiki_dir: Path, today: str, summary: str, details: list[str]) -> None:
    log_path = wiki_dir / "log.md"
    block = [f"## [{today}] ingest | {summary}", *details, ""]
    existing = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
    if not existing.endswith("\n"):
        existing += "\n"
    log_path.write_text(existing + "\n".join(block), encoding="utf-8")


def prune_mirrors(wiki_dir: Path, report: Report, *, dry_run: bool) -> None:
    for section in ("entities", "concepts", "comparisons", "queries"):
        folder = wiki_dir / section
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if not is_mirror_page(path):
                continue
            if dry_run:
                print(f"[dry-run] remove mirror {section}/{path.name}")
            else:
                path.unlink()
            report.mirrors_removed += 1
    raw_articles = wiki_dir / "raw" / "articles"
    if raw_articles.is_dir():
        for path in sorted(raw_articles.glob("*.md")):
            if dry_run:
                print(f"[dry-run] remove raw copy {path.name}")
            else:
                path.unlink()
            report.raw_removed += 1


def run_ingest(
    wiki_dir: Path,
    docs_dir: Path | None,
    *,
    prune: bool,
    dry_run: bool,
) -> Report:
    report = Report()
    today = date.today().isoformat()

    if docs_dir is not None and docs_dir.is_dir():
        report.docs_files = len(list(docs_dir.glob("*.md")))

    if prune:
        prune_mirrors(wiki_dir, report, dry_run=dry_run)

    pages = list_wiki_pages(wiki_dir)
    report.wiki_pages = len(pages)

    if dry_run:
        print(f"[dry-run] would write Karpathy index with {len(pages)} wiki pages")
        return report

    content = build_index_content(pages, today, docs_dir=docs_dir)
    (wiki_dir / "index.md").write_text(content, encoding="utf-8")
    report.index_written = True

    details = [
        f"- Index: {len(pages)} wiki pages (entities/concepts/comparisons/queries)",
        f"- Pruned mirrors: wiki={report.mirrors_removed}, raw/articles={report.raw_removed}",
    ]
    if docs_dir is not None:
        details.append(f"- Canonical sources: `{docs_dir}` ({report.docs_files} .md files, not copied)")
    append_log(wiki_dir, today, "Index rebuild (Karpathy); docs/ used in place", details)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", type=Path, required=True)
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Repo docs/ directory (canonical Layer 1; files are not copied)",
    )
    parser.add_argument("--prune-mirrors", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    wiki = args.wiki.expanduser().resolve()
    src = args.src.expanduser().resolve() if args.src else None
    if not wiki.is_dir():
        print(f"Wiki not found: {wiki}", file=sys.stderr)
        return 1
    if src is not None and not src.is_dir():
        print(f"docs/ not found: {src}", file=sys.stderr)
        return 1

    report = run_ingest(wiki, src, prune=args.prune_mirrors, dry_run=args.dry_run)
    print(
        f"Done. wiki_pages={report.wiki_pages} docs_files={report.docs_files} "
        f"pruned={report.mirrors_removed}/{report.raw_removed} index={report.index_written}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())