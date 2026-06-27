# Wiki Schema

## Domain
WriterAgent architecture and roadmap

## Conventions
- File names: lowercase, hyphens, no spaces (e.g., `writeragent-architecture.md`)
- Every wiki page starts with YAML frontmatter (see below)
- Use `[[wikilinks]]` to link between pages (minimum 2 outbound links per page)
- When updating a page, always bump the `updated` date
- Every new page must be added to `index.md` under the correct section
- Every action must be appended to `log.md`

## Frontmatter
```yaml
---
title: Page Title
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: entity | concept | comparison | query | summary
tags: [from taxonomy below]
sources: [../docs/source-name.md]   # in-repo docs (canonical; do not copy)
# sources: [raw/articles/external.md]  # external ingests only
summary: One line for index.md
---
```

## Layer 1 sources (WriterAgent repo)
- **In-repo documentation** lives in `docs/` (git). Reference it as `../docs/<file>.md` from wiki pages. **Do not** duplicate files under `wiki/raw/articles/`.
- **`raw/`** is for external captures only (URLs, PDFs, pasted imports) per Karpathy — immutable once written.

## Tag Taxonomy
- Architecture: architecture, design, module, component, interface
- Roadmap: milestone, timeline, release, feature, priority
- Performance: benchmark, latency, throughput, scaling, optimization
- Integration: api, plugin, extension, interop, compatibility
- Deployment: docker, kubernetes, cloud, on-prem, ci/cd
- Testing: unit-test, integration-test, e2e, coverage, quality
- Documentation: spec, guide, tutorial, reference, faq
- Future: speculation, research, exploration, idea, concept

## Page Thresholds
- **Create a page** when an entity/concept appears in 2+ sources OR is central to one source
- **Add to existing page** when a source mentions something already covered
- **DON'T create a page** for passing mentions, minor details, or things outside the domain
- **Split a page** when it exceeds ~200 lines — break into sub-topics with cross-links
- **Archive a page** when its content is fully superseded — move to `_archive/`, remove from index

## Entity Pages
One page per notable entity. Include:
- Overview / what it is
- Key facts and dates
- Relationships to other entities ([[wikilinks]])
- Source references

## Concept Pages
One page per concept or topic. Include:
- Definition / explanation
- Current state of knowledge
- Open questions or debates
- Related concepts ([[wikilinks]])

## Comparison Pages
Side-by-side analyses. Include:
- What is being compared and why
- Dimensions of comparison (table format preferred)
- Verdict or synthesis
- Sources

## Update Policy
When new information conflicts with existing content:
1. Check the dates — newer sources generally supersede older ones
2. If genuinely contradictory, note both positions with dates and sources
3. Mark the contradiction in frontmatter: `contradictions: [page-name]`
4. Flag for user review in the lint report