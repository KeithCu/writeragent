# Run — python domain + shapes American flag

Manual Writer chat trial (headed). The same Ask is eval-1 task
`python_shapes_flag` (`backend=lo`), scored from the harness trace and
an exported `.odt` — no headed GUI and no `writeragent_debug.log` on
that path. OpenRouter-only `--backend string` still cannot run this Ask.
Matrix runners that already call `scripts/eval_2_headed.py --task` can
launch this slot the same way as the other headed tasks.

## Setup

1. From the repo root, **deploy** the extension the headed session will
   load (`make deploy`). Then start the headed helper **with
   `--task python-shapes-flag --launch`**. It **writes**
   `"chatbot.max_tool_rounds": 50` into `writeragent.json` and restores
   the previous value (or removes the key) on Enter / Ctrl-C. Do not edit
   the JSON by hand.

   ```bash
   .venv/bin/python scripts/eval_2_headed.py --task python-shapes-flag --launch
   ```

   `--launch` writes a blank `American Flag.odt` into
   `$TMP/writeragent-eval2-python-shapes-flag` (override with
   `--trial-dir`) and opens that Writer document. Prompt, rubric, and
   notes stay outside that folder. Do **not** open this task directory.
   Shapes go on this document’s draw page (Writer, not Draw `.odg`).

   Headed start is **50** rounds (schema max is **200**). Do not raise
   the everyday default (15). Confirm in the debug log:
   `Tool-calling loop START (max 50 rounds)`.
2. Set the chat model you are scoring (headed gate is
   `google/gemini-3.8-flash`; a single-model trial can use
   `openai/gpt-oss-120b:nitro`).
3. Paste the full contents of `prompt.writeragent.txt` as the user
   message. The text is one line:

   ```
   use the python domain to make an American flag using shapes
   ```

## After the run

`--launch` copies `writeragent_debug.log` into the stamp dir on Enter /
Ctrl-C. Save the Writer document beside that log:

| File | Contents |
|------|----------|
| `prompt_used.txt` | Exact text sent (the one-line Ask) |
| `thinking_and_tools.md` | Model thinking plus tool calls |
| `final_flag.odt` | Writer doc. `--launch` copies the trial `American Flag.odt` here on Enter. Save in Writer first so LibreOffice's page thumbnail is inside the file |
| `writeragent_debug.log` | Full debug log from the stamp (process checks, and outer tool-round counts) |
| `notes.txt` | Observer notes: wrong path, speck geometry, images PNG |
| `preview.png` | Written by `--score` from `Thumbnails/thumbnail.png` inside `final_flag.odt` |

Then score the run directory (document + log). Ready / STREAM_DONE is
ignored — a title with no shapes fails. The same command writes
`preview.png` for the board:

```bash
.venv/bin/python scripts/eval_2_headed.py --task python-shapes-flag --score docs/eval/eval-2/python-shapes-flag/runs/<stamp>/
```

See [`rubric.eval2.md`](rubric.eval2.md). Soft star geometry stays the
partial (gpt-oss-20b almost-flag). Tool-round count is recorded from
the debug log and is not a check. The picture is the page thumbnail
LibreOffice already saved in the `.odt`.
