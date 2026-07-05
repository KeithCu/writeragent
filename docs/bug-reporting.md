# Bug reporting

WriterAgent can open a pre-filled [GitHub new issue](https://github.com/KeithCu/writeragent/issues/new) page in the system default browser. No GitHub CLI or `gh` install is required.

## Menu

**WriterAgent → Report bug...** opens the issue form with environment metadata in the body.

## Error dialogs

Some unexpected-failure message boxes include **Report bug...** and **Copy URL** (copies the same pre-filled GitHub URL). User-guidance errors (wrong document type, missing API key, etc.) keep a plain OK box.

Debug builds (UNO thread guard) also offer report on thread-violation popups.

## Agent tool

The agent (chat or MCP) can call **`report_bug`** to capture a bad experience without leaving the conversation. It takes a `summary`, `details`, and a `category` (`bug`, `ux`, or `feature`), records the feedback to a local log (the durable record), and builds the same pre-filled GitHub issue URL as the menu. It **never auto-submits**: the tool returns `github_issue_url` for the agent to show the user, who reviews and files it. No document text, chat history, or API keys are sent.

Implementation: `ReportBug` in [`plugin/doc/document_research_tools.py`](../plugin/doc/document_research_tools.py).

## What is collected automatically

| Field | Notes |
|-------|--------|
| WriterAgent version | From `plugin/version.py` |
| LibreOffice version | UNO setup product info |
| OS / locale / Python | Platform and LO UI locale |
| **Endpoint** | Current chat API endpoint from settings |
| **Chat model** | Current text/chat model from settings |
| Debug log | Path plus guidance to skim `writeragent_debug.log` for relevant errors |
| **Agent feedback log** (`report_bug` tool) | Append-only `agent_feedback.jsonl` beside `writeragent.json` in the LibreOffice user profile (timestamp, category, summary, details only — no document text or API keys) |

**Not included:** API keys, document text, chat history, or full `writeragent.json`.

## Browser open behavior

Implementation: [`plugin/framework/bug_report.py`](../plugin/framework/bug_report.py)

1. UNO `com.sun.star.system.SystemShellExecute` with `URIS_ONLY`
2. Fallback: Python `webbrowser.open()` (`xdg-open`, `open`, or Windows default handler)

If both fail, the URL is logged at warning level and the dialog stays open so **Copy URL** still works.

## Code entry points

- `open_bug_report_in_browser(ctx, title=..., extra_body=...)`
- `msgbox_with_report(..., reportable=True, report_title=..., report_extra=...)` in [`plugin/chatbot/dialogs.py`](../plugin/chatbot/dialogs.py)
