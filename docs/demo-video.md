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
| Scene | Status | Notes |
|-------|--------|-------|
| 1 hook, 2 grammar (LLM only), 3 chat edit | scripted | model `CHAT_MODEL` (gpt-oss-120b:nitro) |
| 4 voice, 5 web research, 6 document research, 7 LaTeX, 8 Writer↔Calc peer chat, 9 `=PY()`, 10 languages, 11 Draw flag (`FLAG_MODEL`, Gemini 3.8 Flash), 12 close | planned | 6 needs **Settings → Vector Search → Embeddings + FTS** and a built index; 8 needs Writer + Calc open in one soffice ([peer messaging](chat/peer-messaging.md)); 4 is done live with a real mic |

## Open research
Does a *local* model catch the logic-error sentence ("Revenue rose 12% while costs fell faster, so our margins shrank.")?
Unverified; try it with the target local model before filming scene 2. Fallback: the subject–verb slip already in the report.
