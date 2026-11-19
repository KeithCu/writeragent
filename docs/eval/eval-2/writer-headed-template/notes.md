# Notes — Writer on a real template (PARKED)

**PARKED.** Do **not** claim this sibling is headed-ready. Stub notes
only.

Purpose: a Writer task that starts from a **real template** (letterhead /
first-page header / named styles). The gold that would catch **template
murder** once unlocked: agent reports `ok` while wiping the letterhead
or leaving Times 12 under a “Standard” apply.

Blocked on headed template fidelity — letterhead / `setString` logo path
from [#634](https://github.com/KeithCu/writeragent/pull/634)
(`page_set_header_footer_text` used to `setString()` the whole header
region; first-page letterhead lives in `HeaderTextFirst`, not shared
`HeaderText`). Product follow-ups are still landing; do not run this as
a harness trial until a headed poke keeps the logo and named styles.

Eval-2 is not a multi-model benchmark yet.

## How it differs from existing siblings

Tenant / Cadaver / GMP open a **blank** Writer memo (or a Draw form).
None start from a letterhead template with first-page header + named
styles. This slot is that missing surface.

## Pre-open cheat (later, after unpark)

Stage the template `.odt` as the **open** document (not a blank). Gold
PDF letterhead is not the write target until a Writer-friendly sibling
exists. Do not File→Open a flat PDF as the edit surface.

## Not ready

No in-repo gold package, no fixtures, no headed `--task`, no oracle CLI.
See [`SOURCE.md`](SOURCE.md).

## Open

- `[eng L]` Unpark the Writer-on-a-real-template sibling: add a gold package, fixtures, a headed `--task` and an oracle that catches a wiped letterhead or lost named styles, once header fidelity holds. (§ Not ready)
