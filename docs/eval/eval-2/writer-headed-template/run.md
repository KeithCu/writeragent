# Run — headed template (PARKED placeholder)

**Do not run a headed trial for this stub.** PARKED on letterhead /
`setString` logo fidelity ([#634](https://github.com/KeithCu/writeragent/pull/634)).

Not wired into `scripts/eval_2_headed.py` `--task` / `--launch` /
`--score`. Do **not** pass an unregistered `--task` name.

## If unparked later

1. `make deploy`.
2. Do **not** call `--launch` until a helper task exists. Stage **only**
   the Writer-friendly template into a clean trial dir and open **that**
   file (not a blank, not the gold PDF).
3. Existing Writer-only helper trials use **50** rounds. Schema **max is
   200**. Everyday default stays **15**. Do not hand-edit
   `writeragent.json`.
4. Model: `openai/gpt-oss-120b:nitro`.
5. Paste `prompt.writeragent.txt` only after it is more than this TODO.

Save under `runs/` only after unpark. Ready / STREAM_DONE is ignored —
a murdered header fails even if the body looks done.

See [`rubric.eval2.md`](rubric.eval2.md).

## Open

- `[eng S few]` Register a headed helper task for the template sibling and write its real `prompt.writeragent.txt` so a trial can be run. (§ If unparked later)
