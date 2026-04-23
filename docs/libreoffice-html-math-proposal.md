# Proposal: HTML Math -> LibreOffice Math

## Short answer

Yes, this is feasible, but not by relying on LibreOffice's normal HTML import alone.

The existing WriterAgent HTML path goes through LibreOffice's HTML filter (`insertDocumentFromURL(..., FilterName="HTML (StarWriter)")`). That is fine for paragraphs, headings, lists, and tables, but it is not a reliable way to turn HTML-embedded math into editable LibreOffice Math objects.

The better design is:

1. parse the HTML ourselves,
2. extract math expressions,
3. convert them into LibreOffice Math command strings (StarMath),
4. insert real formula objects into Writer,
5. send the non-math HTML through the normal import path.

That gives us editable equations instead of plain text or dropped markup.

## Why I think this is the right direction

### 1. LibreOffice Math is editable through command strings

LibreOffice Math exposes a `Formula` property through the UNO `com.sun.star.formula.FormulaProperties` service. The API docs describe this as the "command string of the formula", which is the StarMath markup that LibreOffice Math edits internally.

That means the best programmatic target is not "HTML with some math inside it", but an actual Writer formula object whose `Formula` property we set.

### 2. LibreOffice can import MathML, but only through dedicated math paths

LibreOffice's own help documents say:

- `Tools > Import Formula` can import MathML files.
- `Tools > Import MathML from Clipboard` transforms MathML into StarMath and inserts it.
- MathML and StarMath are "not fully compatible", so import results sometimes need revision.

This is useful, but it is a math-specific conversion path, not the same thing as generic HTML import.

### 3. Generic HTML/XHTML conversion is not a dependable round-trip for math

The web references I found point in the same direction: LibreOffice can export or import MathML in some contexts, but HTML/XHTML containing embedded math is not reliably turned back into editable formula objects during normal document conversion. In practice, that means generic HTML import should not be our math strategy.

## Important practical observation

Most web math is not "just HTML". It usually appears in one of these forms:

- literal MathML: `<math ...>...</math>`
- KaTeX output: usually HTML plus embedded MathML (`htmlAndMathml` is KaTeX's default output mode)
- MathJax output: MathJax can serialize its internal representation to MathML
- raw TeX delimiters in source text: `$...$`, `$$...$$`, `\(...\)`, `\[...\]`

So the real task is not "teach the HTML filter math". It is "detect math islands inside HTML, then convert them through a math-aware pipeline".

## Proposed architecture

### Recommendation

Add a dedicated HTML-math preprocessing layer ahead of the current Writer HTML import path.

The pipeline should look like this:

1. `HTML fragment`
2. `extract math nodes / spans / delimiters`
3. `normalize each equation into a math payload`
4. `convert payload into StarMath`
5. `insert real formula object`
6. `import remaining non-math HTML normally`

### Phase 1 target

I recommend starting with **MathML-first support**, because it aligns best with what LibreOffice already knows how to import.

Support these inputs first:

- literal `<math>` nodes
- KaTeX-generated MathML inside HTML
- MathJax-generated MathML when present in the DOM or provided by upstream HTML

TeX-delimited math can come later as a second layer.

## The conversion strategy I propose

### Preferred programmatic target: StarMath

The inserted Writer object should end up with a StarMath command string in its `Formula` property.

That gives us:

- editable formulas in Writer
- a native LibreOffice object
- no dependence on browser rendering once the content is in the document

### Best low-dependency path: let LibreOffice convert MathML for us

For MathML input, the most promising approach is to use LibreOffice itself as the converter instead of writing a full MathML -> StarMath translator from scratch.

Conceptually:

1. write one MathML expression to a temporary `.mml` file,
2. load/import it through LibreOffice's math import path,
3. read back the resulting `Formula` command string,
4. insert a Writer formula object and set that formula string directly.

Why this is attractive:

- it stays close to LibreOffice's own supported import path,
- it avoids inventing a huge custom converter on day one,
- it lets WriterAgent reuse LibreOffice's existing MathML understanding.

The main caveat is the same one LibreOffice documents themselves mention: some MathML imports will need cleanup, especially for more complex structures.

### Fallback if the LibreOffice import path is too awkward in UNO

If the direct UNO workflow for "load MathML, then read back StarMath" turns out to be brittle, the backup plan should be:

1. implement a **small internal MathML -> StarMath converter** for the common subset,
2. cover the 80% cases well,
3. explicitly reject or warn on unsupported constructs.

The initial subset should include:

- identifiers and numbers
- operators
- superscripts and subscripts
- fractions
- square roots and nth roots
- fenced expressions / parentheses
- rows
- matrices
- simple integrals, sums, products, limits

That subset would already handle a large share of model-generated math and common educational/scientific content.

## Concrete insertion plan inside WriterAgent

### 1. Keep the current HTML path for non-math content

The current code in `plugin/modules/writer/format_support.py` should remain the default import mechanism for regular HTML.

Do not replace it wholesale.

### 2. Add a math-aware import mode before HTML insertion

For HTML content that contains math:

- parse the HTML fragment,
- split it into a sequence of text/html chunks and math chunks,
- insert them in document order.

For text/html chunks:

- keep using the current HTML import helpers.

For math chunks:

- create a formula object,
- set its formula command,
- insert it inline or as a block depending on source context.

### 3. Inline vs display math

We should preserve whether the source formula was inline or display-style.

Rough rule:

- inline math: insert a formula object at the cursor position within the paragraph
- display math: insert paragraph breaks around the formula object so it stands on its own line

This matters a lot for usability, because a document full of display equations jammed inline will feel broken even if the conversion succeeds technically.

## Input detection order

I would use this precedence:

1. explicit MathML (`<math>`)
2. KaTeX or MathJax embedded MathML
3. preserved original TeX if it is available in attributes/annotations from the upstream renderer
4. raw TeX delimiters in plain text

The key idea is to prefer the most structured representation already present in the HTML.

## What I would not do first

I would **not** start with:

- trying to make the generic HTML import filter understand formula objects
- pixel/image conversion as the primary math path
- a giant "support all TeX" parser in phase 1
- support for arbitrary CSS-drawn math that has no underlying MathML or TeX source

Those are either too fragile or too expensive for the first version.

## Proposed phased implementation

### Phase 1: MathML-aware HTML import

Scope:

- detect `<math>` in imported HTML
- detect MathML emitted by KaTeX/MathJax when present
- convert each expression through LibreOffice's own MathML import path if possible
- insert editable formula objects into Writer

Success criteria:

- a pasted/generated HTML fragment with MathML becomes editable LibreOffice Math objects
- simple inline and display equations round-trip acceptably

### Phase 2: TeX-aware fallback

Scope:

- detect `$...$`, `$$...$$`, `\(...\)`, `\[...\]`
- if the upstream HTML includes original TeX, use it
- convert TeX to a normalized intermediate form and then to MathML or StarMath

This phase is important because a lot of LLM and web content starts as TeX even when the rendered page exposes MathML.

### Phase 3: Robustness and quality

Scope:

- spacing tweaks for inline formulas
- better matrix/alignment handling
- warnings for unsupported expressions
- optional fallback to plain text or image only when conversion truly fails

## Why this fits WriterAgent specifically

This project already has a clear separation between:

- HTML/text import mechanics in `plugin/modules/writer/format_support.py`
- higher-level Writer tool behavior elsewhere

That makes math a good candidate for a specialized preprocessor rather than a rewrite of the whole import stack.

It also fits the broader specialized-toolset direction already documented elsewhere in the repo: math is a domain with its own representation, its own failure modes, and its own insertion semantics.

## Risks and open questions

### 1. LibreOffice's MathML import is imperfect

This is the biggest product risk. LibreOffice explicitly warns that MathML and StarMath are not fully compatible.

Implication:

- the proposal is sound,
- but some equations will still need correction,
- and we should design with graceful fallback instead of assuming perfect conversion.

### 2. UNO automation details need a spike

The one thing I would verify in a short prototype before building the full feature is:

- how cleanly we can create a hidden formula document or formula object,
- feed it MathML,
- and read back the resulting `Formula` property.

If that works, the rest of the proposal becomes much simpler.

### 3. Not all web math exposes machine-readable source equally well

Some pages will give us clean MathML.
Some will give us TeX annotations.
Some will only give us styled HTML.

So we should define support boundaries up front:

- support MathML-backed math first,
- support TeX-backed math second,
- do not promise arbitrary visual-math scraping.

## My recommendation

I would implement this feature as a new **math-aware HTML import path**, with this exact strategy:

1. keep the current HTML import path for ordinary content,
2. detect and extract math from HTML before import,
3. use LibreOffice Math objects as the insertion target,
4. use LibreOffice's own MathML import/conversion machinery where possible,
5. fall back to a limited internal converter only where necessary.

If you want the shortest version of the proposal:

> Do not try to make HTML import "understand equations".
> Instead, extract math from the HTML, convert it to StarMath, and insert real LibreOffice formula objects.

That is the most native, editable, and LibreOffice-aligned approach.

## Suggested next implementation spike

The first prototype I would build is very small:

1. accept a single MathML string,
2. convert it into a Writer formula object,
3. verify that the resulting equation is editable in Writer,
4. verify inline vs display insertion behavior,
5. only then wire it into the HTML import path.

If that spike succeeds, the rest is mostly parser and integration work.

## Sources

- LibreOffice Help, "Import Formula": <https://help.libreoffice.org/latest/en-US/text/smath/01/06020000.html>
- LibreOffice Math Guide 25.2, "Exporting and importing": <https://books.libreoffice.org/en/MG252/MG25205-ExportingImporting.html>
- LibreOffice Math Features help: <https://help.libreoffice.org/latest/en-US/text/smath/main0503.html>
- LibreOffice SDK API, `FormulaProperties`: <https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1formula_1_1FormulaProperties.html>
- KaTeX options (`htmlAndMathml` default output): <https://katex.org/docs/options>
- MathJax MathML support / serialization: <https://docs.mathjax.org/en/latest/output/mathml.html>
- Stack Overflow, MathML import into LibreOffice: <https://stackoverflow.com/questions/10300067/how-to-load-and-mathml-formula-into-libreoffice>
- Stack Overflow, MathML lost in HTML/XHTML conversion workflows: <https://stackoverflow.com/questions/73396787/losing-mathml-when-converting-from-html-to-docx-using-libreoffice>
