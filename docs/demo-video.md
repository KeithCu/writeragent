# Product demo video

Scripted so a take can be repeated: sample files + a UNO driver that "types"
prompts into the real sidebar while you screen-record.

## Run it

```bash
.venv/bin/python scripts/demo/make_demo_report.py     # -> build/demo/ (report, 2 sibling docs, Sales_Data.ods)
make demo-video SCENE=scene1                           # one scene; omit SCENE for all
```

`make demo-video` uses a **visible soffice with your own profile** (your keys, models, language) and
**kills any running LibreOffice first** (`lo-kill`), like `make test-mock-sidebar`. Scenes live in
`tests/demo/test_demo_video_uno.py`; they are skipped unless `WRITERAGENT_DEMO=1`, so they never run in normal suites.
Your `text_model` and grammar settings are restored afterwards.

## Sample data
`scripts/demo/make_demo_report.py` reuses the showcase workbook's palette and `get_sales_dataset()`, so the report's
figures ($119,142 across 35 orders; West 32%) equal `Sales_Data.ods` (copy of `tests/fixtures/python_showcase_demo.ods`).
The Executive Summary is clean except one subject–verb slip that harper-ls misses but an LLM checker flags.

## Scenes
All twelve are scripted in `tests/demo/test_demo_video_uno.py`. `make demo-video SCENE=scene7` runs one; omit `SCENE` for all.

| Scene | What it shows | Notes |
|-------|---------------|-------|
| 1 hook | Rewrite the Executive Summary | |
| 2 grammar | LLM checker on a typed logic-error sentence | Harper is switched off for the run; the sidebar status line reports "1 issue" |
| 3 chat edit | Halve the Product Categories paragraph | |
| 4 voice | Talk to the document, reply read aloud | Waits up to 4 minutes for you to click Record and speak; needs a mic |
| 5 web research | Market context with sources in Outlook | |
| 6 document research | Finds the 90-day notice in `West_Supplier_Contract.odt` | Works with cross-file search off (grep fallback) |
| 7 LaTeX | CAGR formula as an editable formula object | |
| 8 peer chat | Writer agent asks the open workbook's agent | Waits for the `[Peer result from:` follow-up turn ([peer messaging](chat/peer-messaging.md)) |
| 9 `=PY()` | Types a NumPy formula into F48, agent explains it | The formula is typed by the driver, not written by the model (see below) |
| 10 languages | Japanese, then Arabic prompt and reply | Arabic prompt must stay on one line (see below) |
| 11 Draw flag | "Draw the American flag with shapes." | `FLAG_MODEL`; slow and unfinished as of 2026-11-11 (see below) |
| 12 close | Holds on the report for the voice-over | |

## How the driver works
- **Endpoint and model.** Both models are OpenRouter ids, so setup saves `endpoint` = OpenRouter for the run and
  restores your endpoint, model, grammar and speech settings afterwards (also at interpreter exit, because the runner
  skips teardown when soffice dies). The model is chosen by setting the sidebar's model box: the sidebar saves that box
  as `text_model` on every send, so a `text_model` written to the file is overwritten.
- **Out of process.** The driver talks to soffice over URP, so it cannot reach `SendButtonListener`. It sets the Ask
  box text, clicks the real Send button, and waits for Stop to stay disabled for 2.5 seconds.
- **Clean takes.** Chat history is stored per document, so the driver clicks Clear the first time it uses each
  document's sidebar. All documents are opened in setup, before any chat deck exists.
- **Windows.** Each scene raises and maximizes its document. `IsMaximized` is only set when the window is not already
  maximized; setting it again un-maximizes the window on KDE Wayland.
- **Display sleep.** Run long takes under `kde-inhibit --screenSaver --power make demo-video` or the display blanks.

## Known problems found while scripting (2026-11-11, LibreOffice 26.8.2, KDE Wayland)
- **Arabic text that wraps in the Ask box kills soffice.** Setting a 51-character Arabic prompt one character at a
  time crashed soffice ("Unspecified Application Error") twice and froze it once, each time at the wrap column; the
  last log line was `process_queue start fn=SlashPopupController.hide` with no `done`. A 23-character prompt and a
  wrapped Japanese prompt are fine. Not yet checked with real keyboard input.
- **Draw flag does not finish.** Gemini 3.8 Flash drew the stripes and canton in about two minutes, then spent more
  than five minutes on stars that never appeared ("CustomShape 'star5' does not render") and a delegate error
  ("Pass index or name to address the shape").
- **`=PY()` written by gpt-oss-120b is unreliable.** It needed up to six `write_formula_range` attempts on the quoting
  and once ended in "Upstream error from Groq: Failed to parse tool call arguments as JSON". Scene 9 therefore types
  a known-good formula.
- **Peer answer varies.** The Calc agent returned Enterprise revenue of $81,497.50 (correct) in two takes and
  $9,980 in a third. Check the number before keeping a take.
- **Dark text background in the sidebar.** History restored from an earlier session, and sometimes the greeting
  after Clear, is drawn with a near-black character background on the light sidebar.

## Open research
Does a *local* model catch the logic-error sentence ("Revenue rose 12% while costs fell faster, so our margins shrank.")?
gpt-oss-120b via OpenRouter reports one issue for it. Try the target local model before filming scene 2.

## Recording
`scripts/demo/record_screen.py OUT.mkv` records the whole screen through the desktop portal (x264 in Matroska, native
resolution); stop it with Ctrl+C. Run it with `/usr/bin/python3` (it needs `dbus` and GStreamer's `gi`). The first run
shows KDE's share dialog: click the **Share "Laptop screen"** panel with the thumbnail, not "Share virtual screen"
(that records an empty extra screen). The choice is remembered in `build/demo/video/.portal_restore_token`.
`spectacle -R` could not be started reliably from a script.

`scripts/demo/record_takes.sh scene1 scene2 …` records one `build/demo/video/sceneN.mkv` per scene and appends the
result to `takes.txt`. Each take restarts LibreOffice and opens only the documents that scene needs, with the one being
filmed opened last: KDE Wayland will not let the driver raise a window while another application has focus.
