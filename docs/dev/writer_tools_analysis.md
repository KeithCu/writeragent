# Writer Tools Consolidation Analysis

## Current State: 51 Tool Classes across 19 Files

| File | Tools | Lines | Bytes |
|------|-------|-------|-------|
| [annotations.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/annotations.py) | AddAiSummary, GetAiSummaries, RemoveAiSummary | 102 | 3.6K |
| [bookmarks.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/bookmarks.py) | ListBookmarks, CleanupBookmarks | 51 | 1.8K |
| [comments.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/comments.py) | ListComments, AddComment, DeleteComment, ResolveComment, ScanTasks, GetWorkflowStatus, SetWorkflowStatus, CheckStopConditions | 617 | 19.7K |
| [content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py) | GetDocumentContent, ApplyDocumentContent, FindText, ReadParagraphs, InsertAtParagraph, SetParagraphText, SetParagraphStyle, DeleteParagraph, DuplicateParagraph, CloneHeadingBlock, InsertParagraphsBatch | 1032 | 36.4K |
| [format_support.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/format_support.py) | *(helper module, no tools)* | 580 | 20.2K |
| [frames.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/frames.py) | ListTextFrames, GetTextFrameInfo, SetTextFrameProperties | 276 | 9.1K |
| [fulltext.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/fulltext.py) | SearchFulltext, GetIndexStats | 160 | 5.5K |
| [images.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images.py) | GenerateImage, EditImage | 123 | 3.6K |
| [images_doc.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images_doc.py) | ListImages, GetImageInfo, SetImageProperties, DownloadImage, InsertImage, DeleteImage, ReplaceImage | 729 | 24.1K |
| [navigation.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/navigation.py) | NavigateHeading, GetSurroundings | 90 | 3.1K |
| [ops.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/ops.py) | *(helper module, no tools)* | 125 | 3.8K |
| [outline.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/outline.py) | GetDocumentOutline, GetHeadingContent | 181 | 5.6K |
| [search.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py) | SearchInDocument, ReplaceInDocument | 241 | 8.1K |
| [stats.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/stats.py) | GetDocumentStats | 83 | 2.3K |
| [structural.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/structural.py) | ListSections, GotoPage, GetPageObjects, RefreshIndexes, ReadSection, ResolveBookmark, UpdateFields | 372 | 13.4K |
| [styles.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/styles.py) | ListStyles, GetStyleInfo | 151 | 4.3K |
| [tables.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tables.py) | ListTables, ReadTable, WriteTableCell, CreateTable | 279 | 8.8K |
| [tracking.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tracking.py) | SetTrackChanges, GetTrackedChanges, AcceptAllChanges, RejectAllChanges | 152 | 4.6K |
| [tree.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tree.py) | GetDocumentTree, GetHeadingChildren | 94 | 3.4K |

**Total: ~5,438 lines, ~181KB**

---

## Proposed Consolidations

### 1. [outline.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/outline.py) + [tree.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tree.py) → **[outline.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/outline.py)** (high overlap)

Both deal with heading/outline navigation. The overlap is significant:

| outline.py | tree.py | Overlap |
|---|---|---|
| `get_document_outline` | `get_document_tree` | Both build the heading tree — `get_document_tree` is the richer version with bookmarks and content strategies |
| `get_heading_content` | `get_heading_children` | Both drill into heading content — `get_heading_children` is richer with locator support |

**Proposal:** Merge into [outline.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/outline.py). Keep `get_document_tree` and `get_heading_children` as the canonical tools. `get_document_outline` can become a thin wrapper or be absorbed into `get_document_tree` with a [format](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/format_support.py#30-43) parameter. `get_heading_content` can be absorbed into `get_heading_children`.

**Savings:** ~2 tool classes eliminated, 1 file removed (~94 lines)

---

### 2. [search.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py) overlaps with [content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py)'s [FindText](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py#262-308)

- `content.py::FindText` — finds text, returns `{start, end, text}` positions
- `search.py::SearchInDocument` — finds text, returns paragraph context
- Both use pattern matching on document text

**Proposal:** Merge [FindText](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py#262-308) into [SearchInDocument](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py#10-140) by adding a `return_offsets` parameter. The caller chooses whether they want character offsets or paragraph context. Remove [FindText](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py#262-308) from [content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py).

**Savings:** ~1 tool class eliminated, ~45 lines

---

### 3. [images.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images.py) + [images_doc.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images_doc.py) → **[images.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images.py)** (split is artificial)

Currently split into:
- [images.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images.py) — AI-powered image generation/editing (2 tools)
- [images_doc.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images_doc.py) — Document image management (7 tools)

**Proposal:** Merge into a single [images.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images.py). The AI generation tools are just 2 small classes that work directly with images. There's no good reason for the split.

**Savings:** 1 file removed, ~20 lines of imports/boilerplate

---

### 4. [bookmarks.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/bookmarks.py) → fold into [structural.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/structural.py)

[bookmarks.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/bookmarks.py) has 2 small tools (51 lines):
- [ListBookmarks](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/bookmarks.py#6-34) — lists bookmarks
- [CleanupBookmarks](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/bookmarks.py#36-51) — removes `_mcp_*` bookmarks

[structural.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/structural.py) already has [ResolveBookmark](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/structural.py#254-336). All three are bookmark operations.

**Proposal:** Move [ListBookmarks](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/bookmarks.py#6-34) and [CleanupBookmarks](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/bookmarks.py#36-51) into [structural.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/structural.py) (renaming it or keeping the name). Delete [bookmarks.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/bookmarks.py).

**Savings:** 1 file removed, ~10 lines of boilerplate

---

### 5. [annotations.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/annotations.py) → fold into [comments.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/comments.py)

AI annotations are semantically comments/annotations. [annotations.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/annotations.py) tools (AddAiSummary, GetAiSummaries, RemoveAiSummary) share the same `intent = "review"` as comment tools and operate on the same comment infrastructure.

**Proposal:** Move all 3 annotation tools into [comments.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/comments.py). Delete [annotations.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/annotations.py).

**Savings:** 1 file removed, ~10 lines of boilerplate

---

### 6. [stats.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/stats.py) → fold into [content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py)

[stats.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/stats.py) has a single tool, [GetDocumentStats](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/stats.py#10-74) (83 lines). It reads character/word/paragraph/page/heading counts — all document content metadata.

**Proposal:** Move [GetDocumentStats](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/stats.py#10-74) into [content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py). Delete [stats.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/stats.py).

**Savings:** 1 file removed, ~10 lines of boilerplate

---

### 7. [AcceptAllChanges](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tracking.py#101-126) + [RejectAllChanges](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tracking.py#128-152) → single tool with `action` param

Both tools in [tracking.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tracking.py) are identical except for the UNO dispatch command. They could be a single `ManageTrackedChanges` tool with an `action` parameter (`accept_all`/`reject_all`).

**Proposal:** Merge into one tool class. Keeps the file but reduces one class.

**Savings:** ~1 tool class eliminated, ~25 lines

---

### 8. [fulltext.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/fulltext.py) → fold into [search.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py)

[fulltext.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/fulltext.py) provides [SearchFulltext](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/fulltext.py#6-106) and [GetIndexStats](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/fulltext.py#146-160), both search-related. Combined with [search.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py) (which already has [SearchInDocument](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py#10-140) and [ReplaceInDocument](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py#157-241)), this makes a single coherent "search" module.

**Proposal:** Merge [fulltext.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/fulltext.py) into [search.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py). Delete [fulltext.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/fulltext.py).

**Savings:** 1 file removed, ~10 lines of boilerplate

---

## Summary of Results

| Consolidation | Files Removed | Tools Reduced | Lines Saved (est.) |
|---|---|---|---|
| outline + tree → outline | 1 | 2 | ~100 |
| FindText → SearchInDocument | 0 | 1 | ~45 |
| images + images_doc → images | 1 | 0 | ~20 |
| bookmarks → structural | 1 | 0 | ~10 |
| annotations → comments | 1 | 0 | ~10 |
| stats → content | 1 | 0 | ~10 |
| Accept/Reject → single tool | 0 | 1 | ~25 |
| fulltext → search | 1 | 0 | ~10 |
| **Totals** | **6 files** | **4 tools** | **~230 lines** |

**After consolidation: 47 tools across 13 files** (down from 51 tools across 19 files)

> [!NOTE]
> The big content files ([content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py) at 1032 lines, [comments.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/comments.py) at 617 lines, [images_doc.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images_doc.py) at 729 lines) are already large. The proposed merges keep them from getting unwieldy — the largest merge adds ~160 lines to [search.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py) for fulltext, and ~100 lines to [comments.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/comments.py) for annotations.

> [!IMPORTANT]
> The [format_support.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/format_support.py) and [ops.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/ops.py) helper modules (705 lines combined) are not tool files and should remain as-is. They provide shared utilities used across multiple tool files.

## Not Recommended

These were considered but rejected:

- **Merging [frames.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/frames.py) into [content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py)** — [content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py) is already 1032 lines; frames are a distinct enough concept
- **Merging [tables.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tables.py) into [content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py)** — same size concern; tables have unique cell-addressing semantics  
- **Merging [navigation.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/navigation.py) into [outline.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/outline.py)** — navigation uses ProximityService while outline uses TreeService/DocumentService; different abstractions
- **Merging [tracking.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tracking.py) into [comments.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/comments.py)** — tracked changes vs comments are different UNO subsystems despite both being "review" intent
