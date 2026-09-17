# Working on OSWright

Windows desktop automation as an MCP server. The differentiator is the
perception layer: it keeps a model of the screen between actions and re-reads
only what changed, instead of returning a screenshot after every step.

Read `CONTRIBUTING.md` first — it has the project's conventions. This file is
the operational detail an agent needs on top of them.

## Orientation

| document | what it is for |
|---|---|
| `README.md` | what the project claims, with numbers |
| `CONTRIBUTING.md` | conventions: measurement, safety, grading, style |
| `docs/ENGINEERING_LOG.md` | **why** things are the way they are, and 33 recorded mistakes |

When something in the code looks wrong, search `ENGINEERING_LOG.md` before
changing it. Most of the odd-looking decisions are odd deliberately and the
reason is written down.

## Commands

```powershell
python -m pytest tests/ -q                  # full suite, ~6 s
python -m pytest tests/ -m "not e2e" -q     # skip the ones that drive a desktop
ruff check oswright tests benchmarks        # lint, must be clean
python -m build; python -m twine check dist/*   # what the release does
```

Tests needing a desktop or an OCR backend skip themselves rather than fail, so
the suite stays meaningful on a headless runner. A skip is not a pass — check
what skipped before concluding a change is safe.

## The version lives in four places

This has broken a release. Changing it means changing all four:

| file | how it is read |
|---|---|
| `oswright/_version.py` | the source of truth, a plain literal |
| `server.json` | restated by hand, twice, for the MCP registry |
| `pyproject.toml` | setuptools parses the AST for a literal — no computed values |
| `.github/workflows/publish.yml` | a regex greps the file for `__version__ = "..."` |

Three of these read the literal **statically, without importing it**. Making
the version computed — an f-string, a `.join()`, a lookup — breaks the build
and the release guard silently.

`tests/test_core.py::TestVersionConsumers` asserts all four agree. If it fails,
it is right and you are about to ship a mismatch.

## Releasing

1. Bump `oswright/_version.py` **and** `server.json`.
2. Run the suite and `ruff`. Confirm `TestVersionConsumers` passes.
3. Tag and publish a GitHub release.
4. `publish.yml` → PyPI. `registry.yml` → the MCP registry.

Both workflows fire on `release` and race, so `registry.yml` polls PyPI for
five minutes before publishing. `publish.yml` uploads with `skip-existing`,
because a manual dispatch followed by a release event would otherwise fail on a
duplicate upload — a red run that means nothing, which is worse than no signal.

If a release run fails, **read it**. The one time that was skipped, a real
failure hid behind a habit formed by a harmless one (`ENGINEERING_LOG.md`
§2.20).

Verify a release against the **published artifact**, not the working tree:
install from PyPI into a clean venv and speak MCP over stdio. A version the
server reports incorrectly over the wire is invisible from a dev checkout, and
that is exactly how one shipped.

## Benchmarks open real windows

`benchmarks/` drives the actual desktop. It moves the mouse, types, and takes
focus. Do not start a sweep without asking the person at the machine first.

- Subjects must be stateless or use throwaway profiles the benchmark created.
- Every subject needs a `safe_because` explaining why it cannot destroy work.
- Notepad is excluded outright: it restored a document with unsaved changes
  belonging to the machine's owner.
- Grade against the application's own state — UI Automation or the window
  title — never against OCR. Checking OCR with OCR only proves it agrees with
  itself.
- Reproduce a finding before explaining it. A sweep once reported 24/36 and the
  next reported 36/36 with no code change.

## Things that have cost time

- **The OCR cache is keyed on the image, not on the settings.** Measuring
  anything that changes preprocessing — resolution, downscale width — needs a
  fresh `OCREngine` per measurement, or every configuration returns identical
  results and looks like a null finding.
- **Linux CI installs with `--no-deps`** to avoid pulling ~2.5 GB of torch, so
  no OCR backend exists there. Tests touching OCR need a skip guard, and the
  `mcp[cli]<2` bound has to be restated in the workflow because `--no-deps`
  ignores `pyproject.toml`.
- **`Optional[X]`, not `X | None`.** The MCP schema generator reads those
  annotations. Ruff's rewrite rule is disabled on purpose.
- **FastMCP takes no `version` argument.** The low-level server does, and
  FastMCP passes `None`, at which point the SDK reports *its own* version as
  ours. Set `mcp._mcp_server.version` — a private attribute, and the only seam.
- **OCR downscaling is per-backend.** Windows OCR is indifferent to a 1280 cap;
  EasyOCR scores 0/5 at 1280 and 5/5 at native. One shared cap was silently
  correct on Windows and catastrophic elsewhere.

## What does not belong in this repo

It is public. Launch posts, announcement drafts, and promotional framing have
been committed here twice and removed both times. Keep them somewhere private.

The engineering log is the exception and is not promotional — it records the
losses as carefully as the wins, which is the point of it.
