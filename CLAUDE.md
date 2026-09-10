# CLAUDE.md

Guidance for working in this repo.

## What this is

`pdfsplitter` — cuts a large, bookmarked PDF into one file per top-level
chapter while keeping every internal hyperlink working across the resulting
files. Two modules at the repo root, exposed as console scripts in
`[project.scripts]`:

- `split_manual.py` → `pdfsplitter` (`outline` and `split` subcommands)
- `verify.py` → `pdfsplitter-verify` (independent link-correctness check)

`verify.py` imports the resolution helpers from `split_manual` so both agree on
what a link "points to".

## Tooling

- **mise** provisions Python and uv — `.mise.toml` pins Python 3.14 and
  `uv = "latest"`; run `mise install` after cloning. `.mise.toml` is the sole
  source of truth for the Python version — no `.python-version` file.
- Use **uv / uvx** for packaging, envs, and running everything. Never `pip`,
  `pipx`, or a hand-rolled venv.
- Run ad hoc: `uv run --with pikepdf --with reportlab python split_manual.py outline manual.pdf`
- Dev env: `uv sync` (installs the `dev` dependency group).
- Lint / format individually: `uvx ruff check .`, `uvx black .`, `uvx mdformat .`
- All hooks at once: `uvx pre-commit run --all-files`
- Config lives in `pyproject.toml` (`ruff`, `black`), `.mdformat.toml`, and
  `.pre-commit-config.yaml`. This file is excluded from `mdformat` — keep it
  that way.

## Design notes

- **Capture before copy.** Link targets must be resolved against the intact
  original _before any page is copied_ — `collect_and_strip_links` does this and
  then deletes `/Dest` and `/A` from the source annotations so page copying
  cannot drag foreign pages along. Do not reorder that.
- **Three destination encodings** must all be handled: a bare `/Dest` array, a
  `/Dest` that is a name looked up in `/Names/Dests` (and the legacy
  `/Root/Dests`), and an `/A << /S /GoTo /D … >>` action. `resolve_dest`
  recurses through all of them; `dest_of_annot` and the outline reader pick
  whichever is present.
- **Link rewriting in the output:** target in the same chapter → plain `/GoTo`;
  target in another chapter → `/GoToR` at `filename` + page-index-within-target;
  target in no chapter → dropped (counted). `/URI` links are never touched.
- `/PageLabels` renumbers each chapter so the viewer shows the original page
  numbers; `/B` (article beads) are stripped since they can span chapters.
- `00_Contents.pdf` is drawn with reportlab, then reopened with pikepdf to
  attach the `/GoToR` link annotations.
- Requires Python 3.14+ (`requires-python` in `pyproject.toml`).

## Correctness anchor

After any change to destination resolution or link rewriting, run
`pdfsplitter-verify <original> <outdir>` against a real split — it must print
`RESULT: PASS` and exit 0. It re-derives every link target independently, so a
bug that affects both scripts the same way would still show as a page-count or
ToC mismatch.
