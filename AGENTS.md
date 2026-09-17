# AGENTS.md

Agent instructions for this repository live in
**[`.github/copilot-instructions.md`](.github/copilot-instructions.md)**.

Read that first, then `CONTRIBUTING.md` for the project's conventions.

If a `HANDOVER.md` exists in the working copy, read it before anything else —
it is gitignored local notes carrying state between sessions, and it is not
part of the repository.

Two things that catch people out, in full over there:

- The version is restated in four files and three of them read it *statically*.
  `tests/test_core.py::TestVersionConsumers` is what keeps them honest.
- `benchmarks/` drives the real desktop. Ask before running a sweep.
