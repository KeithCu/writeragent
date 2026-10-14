# Applying LibreOffice Styles with LLMs

When integrating Large Language Models (LLMs) with LibreOffice using WriterAgent, you can seamlessly apply existing LibreOffice document styles by instructing the AI to output specifically formatted HTML. This avoids the limitations of basic Markdown and gives the LLM full access to the rich styling of your template.

## How It Works

WriterAgent applies LLM-generated formatting by saving the response to an HTML/Markdown file and importing it using LibreOffice's `HTML (StarWriter)` filter. Therefore, LibreOffice's native HTML class-to-style mapping rules apply automatically.

You can have the LLM map directly to your existing styles using the standard HTML `class` attribute.

### Paragraph Styles

Instruct the LLM to output a `<p>` tag with the class name matching the exact name of the LibreOffice paragraph style.

```html
<p class="My Custom Theme">This paragraph will take on the 'My Custom Theme' style.</p>
<p class="Warning Box">This is an alert box!</p>
```

### Character Styles

Instruct the LLM to use a `<span>` tag with the character style name:

```html
Check out this <span class="Code Snippet">inline code</span> right here.
```

### Built-in Styles

Standard HTML tags are also automatically mapped to their LibreOffice equivalents without needing specific class names:
* `<h1>`, `<h2>`, `<h3>` → **Heading 1**, **Heading 2**, **Heading 3**
* `<blockquote>` → **Quotations**
* `<ul>/<li>` → Standard List formatting

## The `data-lo-style` Convention (Agent Read/Write)

The `class="Style Name"` mapping above is the **legacy** path: it relies on LibreOffice's `HTML (StarWriter)` import and is still honored for hand-written or non-agent HTML.

For the agent, WriterAgent uses a tighter, symmetric convention so reads and writes speak the same language:

- **Read:** `get_document_content` exports via the `XHTML Writer File` filter and post-processes it. Each block carries its named paragraph style as a **compact `data-lo-style` token = the LibreOffice style name with spaces removed** (`Heading 1` → `Heading1`, `Text body` → `Textbody`, `Caption` → `Caption`, `Standard` → `Standard`). Use the tokens exactly as returned. Inline `style="..."` is reserved *exclusively* for direct character overrides (bold, italic, run colour, and similar). Synthetic autostyle paragraphs (e.g. after an edit, where the StarWriter import bakes extra direct char props into the paragraph) have their real base style name recovered from a paired flat-ODF (`.fodt`) export (the autostyle's `style:parent-style-name`, which the XHTML export flattens away); only a genuinely unresolvable autostyle is emitted without a token. Optional parameter **`include_images`** (default `false`): when false, inline `data:image` base64 is stripped; external img URLs are kept.
- **`data-lo-para` (read report):** the same FODT sidecar also lists each automatic style's **own** properties — margins, indent, alignment, line-height, paragraph-level font/size/weight/style, and paragraph colour (`fo:color` → `color:`). Those are emitted as `data-lo-para="margin-left:3.25cm; font-size:12pt; color:#ff0000"`. DO: use that attribute to tell a hand-indented or coloured quote from body text before you style it. It is **not** a write instruction — sending it back is ignored (the result lists `ignored_attributes`). Character-level colour still appears on spans as inline `style`.
- **Write:** `apply_document_content` reads `data-lo-style` back, resolves the compact token to the real LibreOffice `ParaStyleName` (`Heading1` → `Heading 1`), applies the named style first, then layers any inline `style="..."` on top as direct overrides. An unknown token falls back to `Standard`. **Named-style application happens when you rewrite the whole document (`target="full_document"`).** For targeted inserts/replaces (`end`/`beginning`/`selection`/`search`) the content is inserted but the named style is **not** applied — the first imported block merges into the cursor's existing paragraph, so applying its style would restyle the adjacent pre-existing text. To style text that already exists, use `apply_style`. Inline `style="..."` character overrides are honored on all targets.

```html
<!-- What get_document_content returns, and what the agent should write back: -->
<p data-lo-style="Heading1">Section title</p>
<p data-lo-style="Standard">A normal paragraph with a <span style="font-weight: bold">bold</span> word.</p>
```

Use the tokens exactly as returned by `get_document_content` (no spaces). This keeps documents using named styles instead of accreting raw inline formatting.

### v1 limitations and workarounds

Full design notes: [html-style-model-plan.md](html-style-model-plan.md#v1-limitations-shipped).

| Situation | What v1 does | What to do instead |
|-----------|--------------|-------------------|
| Whole-paragraph alignment, colour, or margins (not a named style) | Reported on **read** as read-only `data-lo-para` (FODT automatic style; includes `color:` when `fo:color` is set). Ignored on **write** — sending the attribute back does not restore `Para*`. Re-applying a style does **not** keep a quote indent (LibreOffice drops direct `Para*`) | DO: read `data-lo-para` to tell an indented or coloured quote from body text. `apply_style` defaults to house font/size winning (`clear_direct='style_props'`, including Asian/Complex slots); pass `clear_direct='none'` only to keep a hand-set font; use inline `style` on **spans** for character-level exceptions |
| Styling content you insert at `end` / `search` / `selection` | `data-lo-style` is **not** applied (would restyle text already in the document) | Use `target='full_document'` for styled rewrites, or `apply_style` on existing text |
| Table cell paragraph styles | Not exposed in agent HTML | Use `apply_style` on the cell text |
| Large documents | Every full read exports twice (XHTML + flat ODF) | Prefer `scope=range`, `get_document_tree`, and `search_in_document` before `scope=full` |
| Generated list labels (`1.`, `•`) | XHTML keeps `Numbering_20_Symbols` / `Bullet_20_Symbols`; nested levels display as `1.` not `1.2.3`. `search_in_document("1.")` does not hit those labels | Search item **text**. List-only (not chapter numbering): [list-numbering.md](list-numbering.md) |

**Post-v1:** a cached UNO paragraph-style index should remove the second full export on large docs and improve autostyle resolution. See the plan doc “Long-term” section.

## Strategies for Prompting

To reliably get the LLM to use your styles, you need to provide it with instructions and the names of the styles available in the current document. 

### 1. Dynamic Discovery via Tools

In newer versions of WriterAgent, the LLM has access to a `style_list()` tool. Calling `style_list()` with no family returns family names; pass `family="ParagraphStyles"`, `"CharacterStyles"`, or `"PageStyles"` to list styles in that family. You can instruct the agent in your prompt to first call this tool to learn what styles are available before writing its HTML formatting.

Note: `No Character Style` in the character styles list is only the `apply_style` clear token. Inspect and edit the character style object as `Default Style`.

**Pros:**
* Always accurate and up-to-date with whatever document the user is currently editing.
* Requires no manual configuration by the user when they switch between documents with different templates.

**Cons:**
* Requires an extra conversational roundtrip (tool call + tool response) before the LLM begins generating the content, which can introduce latency.
* Consumes additional tokens for the tool execution.

### 2. Providing Styles as Context

Another approach is to proactively inject the list of available document styles directly into the system prompt or context window sent to the LLM on every request.

**Pros:**
* **Saves a Roundtrip:** The LLM immediately has the styles it needs, removing the latency of a tool call.
* **Guarantees Awareness:** The LLM is less likely to hallucinate style names because the valid list is explicitly provided upfront.

**Cons:**
* **Context Token Usage:** Pushing the full list of styles (which can be quite long, depending on the template) into every prompt consumes context tokens. This might be inefficient for long documents or templates with dozens of custom styles.
* **Potential Clutter:** Built-in LibreOffice documents often contain many default styles that the user has no intention of using. Sending *all* of them might confuse the LLM or result in it choosing obscure defaults over user-created ones.

### The Hybrid Approach (Recommended)

A balanced solution is to use proactive context injection, but **filter the list of styles provided**. 

Instead of sending every available style, WriterAgent could:
1. Only inject styles that are currently *in use* within the document.
2. Only inject custom (user-defined) styles, excluding built-in LibreOffice defaults (`Standard`, `Heading 1`, etc.).
3. Allow the user to define a specific subset of "preferred styles" in their Settings that always get injected.

If the LLM needs a style outside of this injected "shortlist", it can rely on the dynamic `style_list()` tool as a fallback. 

## Example System Prompt Instruction

> *"When writing text, use the `<p class="Style Name">` and `<span class="Style Name">` HTML tags to apply formatting. Make sure the class names precisely match the custom styles provided in your context. Available preferred paragraph styles include: 'Warning Box', 'Aside', 'Code Block', and 'Appendix'."*

## Editing Styles Dynamically

WriterAgent also provides tools for the LLM to inspect and modify existing document styles directly.

### The `style_create` Tool

The LLM can create new styles from scratch, allowing for document-wide consistent formatting updates. This tool supports standard Paragraph and Character styles, as well as **Conditional Paragraph Styles**. `style_create` validates rules and properties before insertion; if any property or conditional rule is invalid, it returns `failed_properties` and does not insert the style into the document. Providing `conditional_rules` for a character style is an error.

**Tool Parameters:**
* `style_name`: The name of the new style.
* `family`: `ParagraphStyles` or `CharacterStyles`.
* `parent_style`: (Optional) The style to inherit from.
* `property_updates`: Initial font, margin, and color settings.
* `conditional_rules`: (Optional, ParagraphStyles only) Map contexts like `Table` or `Header` to other styles.

**Struct-valued properties** (`style_create`, `style_update`, and read by `style_get_info`): `ParaLineSpacing`, the border
properties (`Left/Right/Top/BottomBorder` and the `Char*` ones), `DropCapFormat` take an **object of
fields**, and `ParaTabStops` a **list** of them — e.g. `{"ParaLineSpacing": {"Mode": "prop",
"Height": 150}}` is 1.5 lines (`Mode` is `prop` / `minimum` / `leading` / `fix`; `Height` is a percent
for `prop`, 1/100 mm otherwise). `style_get_info` returns these struct properties as the same objects `style_update` accepts.
`CharPosture` is `0=None`, `1=Oblique`, `2=Italic`; `style_get_info` returns that integer.

### The `style_clone` Tool

Clones an existing style under a new name. `family` is `ParagraphStyles` (default), `CharacterStyles`, or `PageStyles`.

Paragraph and character clones share the source parent and copy only properties that style sets directly. Paragraph clones also copy `FollowStyle`. A page clone copies direct page properties (not `FollowStyle` — a new page style already points at itself), then the header and footer content, including fields, and floating graphics anchored in those regions. Header and footer flow is copied inside the same document (paragraphs, tables, direct character and paragraph properties, and fields, including a page-number field's SubType, Offset, and NumberingType). The clone does not open a second Writer and does not export XHTML. Floating graphics are still recreated on the draw page. The new style is not applied to a page. Apply that clone with `page_insert_break(page_style=<new name>, restart_page_number=1)` on the paragraph that should start the annex (or another integer from 1 to 32767). Omit `restart_page_number` to keep the current page sequence. The page sub-agent can call `style_list` and `style_clone` without switching to the styles domain.

**Tool Parameters:**
* `style`: Source style name.
* `new_style`: Name of the new style. Must not already exist.
* `family`: `ParagraphStyles`, `CharacterStyles`, or `PageStyles`.

### The `style_import` Tool

If the user has a preferred template file (.ott or .odt), the agent can import all its styles at once to ensure branding consistency.

**Tool Parameters:**
* `path`: Absolute filesystem path to an existing template file.
* `overwrite`: Whether to replace existing styles.
* `load_paragraph_styles`: (Default: True) Import text styles.
* `load_page_styles`: Import page layouts.

### Setting Colors and Properties

When updating styles, the LLM sets underlying **LibreOffice UNO API properties**. The `style_update` and `style_create` tools support hex colors (like `#FF0000` or `FF0000`) and the values `-1`, `"automatic"`, or `"transparent"` for automatic or no fill (which are kept distinct from 24-bit RGB and not passed through `parse_color_to_uno_int`). Border `Color` fields accept the same forms. `ParaBackColor` accepts a hex string or `-1`/`automatic`/`transparent` as well. `ParaAdjust` is `left` / `center` / `right` / `justify` — not `0/1/2/3` (those integers are hostile: `1` is right, `2` is justify). A misspelled style name gets a close-name hint. `before` / `after` echo the keys that were set. If `CharFontName` is not installed, the result warns that LibreOffice will substitute.

**Key Color Properties (CharacterStyles and ParagraphStyles):**
* `CharColor`: The main text color.
* `CharBackColor`: The background (highlight) color of the text.
* `CharUnderlineColor`: The color of the text underline.
* `ParaBackColor`: The paragraph background color.

**Example Tool Call:**
```json
{
  "style_name": "Heading 1",
  "family": "ParagraphStyles",
  "property_updates": {
    "CharColor": "#0055A4",
    "CharPosture": 2,
    "ParaBackColor": "transparent",
    "CharWeight": 150,
    "ParaTopMargin": 500,
    "ParaAdjust": "center"
  }
}
```

By leveraging `style_update`, the LLM can act as a fully autonomous designer, adjusting typography, spacing, and brand colors without the user having to manually edit LibreOffice style templates.
