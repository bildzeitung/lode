# Data request: `lode work` hang + reconcile re-enqueue loop

**Direction of this doc:** agent → human. Unlike specs 01–06 (your feedback to me),
this file is a task **for you to run**, with the exact commands and the output I need
back. Drop the results wherever is convenient (paste into chat, or append them to the
"Results" section at the bottom of this file and commit).

## Why

Confirms and root-causes two defects filed under epic **lode-olmi** (spec 06, item 9):

- **lode-olmi.11** — reconcile re-enqueues and re-pulls the *same* gap version every
  pass; the gap never clears. Visible under `lode work --wait`.
- **lode-olmi.12** — plain `lode work` (one-shot) hangs silently instead of doing one
  pass and exiting.

Static analysis got as far as: the loop is a never-clearing reconcile gap (either the
`enrich_gap` `prompt_ver` check or the `embed_gap` dead-letter path), and the one-shot
hang is almost certainly a *blocking* call inside the single pass (first-use
fastembed/ONNX model load, or the enrich Batches-API call), **not** the reconcile loop
— because one-shot `lode work` is structurally single-pass. To pick the right fix I need
to see which job type loops and where one-shot blocks. That is what this gathers.

## What to run

Run these from the repo root with the venv active (`. ./venv/bin/activate`), against
the DB that reproduces the problem.

1. **The `--wait` transcript** — let it run through several poll cycles (10–20s is
   plenty), then Ctrl-C. Capture *all* stdout/stderr:

   ```bash
   lode --debug work --wait 2>&1 | tee /tmp/lode-work-wait.log
   ```

   `--debug` matters: it turns on the reconcile step's per-step `reconcile[<step>]: N
   gap version(s) enqueued` INFO line, which names the looping step (`embed_gap` /
   `enrich_gap` / `refresh_stale`).

2. **The stuck job row(s)** — after (1), dump the jobs table so I can see what state the
   looping job settles in:

   ```bash
   lode jobs
   ```

   and, for the precise signal, straight from SQLite (find the DB path with
   `lode config` or your `$LODE_HOME`; the table is `jobs`):

   ```bash
   sqlite3 "$LODE_HOME/lode.db" \
     "SELECT id, type, target_version, status, attempts, prompt_ver, next_attempt_at, substr(last_error,1,200) AS last_error FROM jobs ORDER BY id;"
   ```

3. **Where one-shot hangs** — start a plain one-shot, and while it is hung, grab a
   Python stack so I can see the exact blocking call:

   ```bash
   lode --debug work 2>&1 | tee /tmp/lode-work-oneshot.log &
   #   … wait ~10s until it's clearly hung, note the PID printed …
   py-spy dump --pid <PID>      # if py-spy is installed (pip install py-spy)
   #   — OR, if no py-spy: Ctrl-\ (SIGQUIT) in the foreground run prints a traceback
   ```

   The `--debug` log alone is useful even without a stack dump — the last line before it
   goes quiet tells me which phase it entered (reconcile vs. batch pre-step vs. embed).

## Desired output

Paste back, or append under Results below:

1. `/tmp/lode-work-wait.log` — enough cycles to show the repeating
   `reconciled N gap version(s)` / `reconcile[<step>]` lines. **The step name in those
   lines is the single most important datum.**
2. The `jobs` table dump (either `lode jobs` or the SQLite `SELECT`), focused on the
   row(s) whose `target_version` is the one being re-enqueued.
3. From the one-shot hang: the `py-spy dump` / SIGQUIT traceback if you got one, else the
   tail of `/tmp/lode-work-oneshot.log` (the last lines before it went silent).

With those three I can reproduce in a worktree and drive the fixes on lode-olmi.11
(never-clearing gap) and lode-olmi.12 (one-shot must not hang).

## Results

_(append output here, or paste into chat)_
