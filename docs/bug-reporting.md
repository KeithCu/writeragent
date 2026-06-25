# Bug reporting

WriterAgent can open a pre-filled [GitHub new issue](https://github.com/KeithCu/writeragent/issues/new) page in the system default browser. No GitHub CLI or `gh` install is required.

## Menu

**WriterAgent → Report bug...** opens the issue form with environment metadata in the body.

## Error dialogs

Some unexpected-failure message boxes include **Report bug...** and **Copy URL** (copies the same pre-filled GitHub URL). User-guidance errors (wrong document type, missing API key, etc.) keep a plain OK box.

Debug builds (UNO thread guard) also offer report on thread-violation popups.

## What is collected automatically

| Field | Notes |
|-------|--------|
| WriterAgent version | From `plugin/version.py` |
| LibreOffice version | UNO setup product info |
| OS / locale / Python | Platform and LO UI locale |
| **Endpoint** | Current chat API endpoint from settings |
| **Chat model** | Current text/chat model from settings |
| Debug log | Path plus guidance to skim `writeragent_debug.log` for relevant errors |

**Not included:** API keys, document text, chat history, or full `writeragent.json`.

## Browser open behavior

Implementation: [`plugin/framework/bug_report.py`](../plugin/framework/bug_report.py)

1. UNO `com.sun.star.system.SystemShellExecute` with `URIS_ONLY`
2. Fallback: Python `webbrowser.open()` (`xdg-open`, `open`, or Windows default handler)

If both fail, the URL is logged at warning level and the dialog stays open so **Copy URL** still works.

## Code entry points

- `open_bug_report_in_browser(ctx, title=..., extra_body=...)`
- `msgbox_with_report(..., reportable=True, report_title=..., report_extra=...)` in [`plugin/chatbot/dialogs.py`](../plugin/chatbot/dialogs.py)
