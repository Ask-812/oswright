# Contributing

Bug reports and pull requests are welcome. A few things worth knowing before
you start, because this project has some unusual conventions.

## Claims need measurements

Every performance number in the README and the engineering log is reproducible
from `benchmarks/`. If a change is meant to make something faster or cheaper,
the pull request should say by how much, measured, on a named machine -- and if
it makes something slower somewhere else, it should say that too.

Results that go against the project's own claims are welcome and get written
down. `docs/ENGINEERING_LOG.md` already records thirty of them.

## Repeats before findings

Benchmarks that open windows are noisy. One sweep here reported 24/36 and the
next reported 36/36 with no change to the code under test, and real time was
spent explaining a result that turned out to be environmental. Reproduce a
finding before explaining it, and raise `OSWRIGHT_BENCH_REPEATS` when a number
is load-bearing.

## Benchmarks must not be able to lose anyone's work

Subjects are stateless, or run against throwaway profiles and folders the
benchmark created. Notepad is excluded outright: launching it restored a
document with unsaved changes belonging to the machine's owner.

Any new subject needs a `safe_because` explaining why it cannot destroy
anything, and must clean up what it opened.

## Grading

A task is graded against the application's own state -- UI Automation, or the
window title -- never against OCR. Checking OCR with OCR only establishes that
it agrees with itself.

## Running things

```
pytest tests/                        # 237 tests; desktop ones skip without a display
pytest tests/ -m "not e2e"           # unit tests only
ruff check oswright tests benchmarks # lint
python benchmarks/bench_tasks.py     # task success, opens real windows
```

Tests that need a desktop skip themselves rather than failing, so CI stays
meaningful on a headless runner.

## Style

Comments explain *why*, not *what*, and are worth writing when a decision looks
wrong without context -- most of the odd-looking code here is odd for a reason
that is recorded next to it. `Optional[X]` is used throughout rather than
`X | None`, because the MCP schema generator reads those annotations.
