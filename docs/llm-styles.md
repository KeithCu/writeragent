# Applying LibreOffice Styles with LLMs

When integrating Large Language Models (LLMs) with LibreOffice using LocalWriter, you can seamlessly apply existing LibreOffice document styles by instructing the AI to output specifically formatted HTML. This avoids the limitations of basic Markdown and gives the LLM full access to the rich styling of your template.

## How It Works

LocalWriter applies LLM-generated formatting by saving the response to an HTML/Markdown file and importing it using LibreOffice's `HTML (StarWriter)` filter. Therefore, LibreOffice's native HTML class-to-style mapping rules apply automatically.

You can have the LLM map directly to your existing styles using the standard HTML `class` attribute.

### Paragraph Styles

Instruct the LLM to output a `<p>` or `<div>` tag with the class name matching the exact name of the LibreOffice paragraph style.

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

## Strategies for Prompting

To reliably get the LLM to use your styles, you need to provide it with instructions and the names of the styles available in the current document. 

### 1. Dynamic Discovery via Tools

In newer versions of LocalWriter, the LLM has access to a `list_styles()` tool. You can instruct the agent in your prompt to first call this tool to learn what styles are available before writing its HTML formatting.

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

Instead of sending every available style, LocalWriter could:
1. Only inject styles that are currently *in use* within the document.
2. Only inject custom (user-defined) styles, excluding built-in LibreOffice defaults (`Standard`, `Heading 1`, etc.).
3. Allow the user to define a specific subset of "preferred styles" in their Settings that always get injected.

If the LLM needs a style outside of this injected "shortlist", it can rely on the dynamic `list_styles()` tool as a fallback. 

## Example System Prompt Instruction

If you choose to manage this via the LocalWriter **Settings → Additional Instructions**, you can add a rule like:

> *"When writing text, use the `<p class="Style Name">` and `<span class="Style Name">` HTML tags to apply formatting. Make sure the class names precisely match the custom styles provided in your context. Available preferred paragraph styles include: 'Warning Box', 'Aside', 'Code Block', and 'Appendix'."*
