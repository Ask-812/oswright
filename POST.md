# Your desktop agent is paying for a screenshot it does not need

Every GUI agent I have looked at works the same way: take a screenshot, OCR the
whole thing, hand the model an image, act, repeat. It is the obvious design, it
is what I built first, and it is enormously wasteful.

Here is the measurement that changed my mind. On a live desktop, timing real
interactions rather than synthetic ones, **the median observation changes 0.012%
of the screen's pixels.** Half of all observations move less than a hundredth of
a percent of what is on screen.

A full rescan does roughly eight thousand times more work than the change
warrants, and it bills the model ~2,800 image tokens whether anything happened
or not.

## What I built instead

[OSWright](https://github.com/Ask-812/oswright) keeps a model of the screen
between observations and re-reads only what moved. Four things make that work,
and none of them is new technology -- the novelty is applying them here.

**Ask the compositor what changed.** Windows already tracks dirty rectangles for
Desktop Window Manager. Desktop Duplication exposes them, so "has anything
changed?" is answerable in **0.14 ms** without transferring a single pixel,
against ~33 ms to capture a frame and then discover it was identical. I ended up
hand-writing the COM bindings, because no Python package exposed the dirty-rect
metadata.

**Try the cheapest source that can answer.** Element lookups go through a
cascade: what the model already knows, then a rescan of just the changed
regions, then the accessibility tree, then the application's own text buffer,
then a full read. A repeat lookup costs ~0.05 ms.

**Remember screens, and verify them with pixels.** Returning to a screen you
have seen before should not cost a full read. It does not -- but verification
compares *pixels*, never re-read text, because OCR output is not a stable
identity. The same label came back as `Elevatc` and `Subarr&` on different
passes.

**Wait as long as the interface actually takes.** The fixed 300 ms sleep after
every action turned out to be six times the cost of the perception it was
protecting. The compositor knows when the screen stops changing.

Measured end to end over a real agent loop: latency per step **212 ms to 33 ms**,
tokens per observation **~2,764 to ~49**.

## The part I nearly got wrong

All of that measures *cost*. Cost is a proxy. The metric that matters is whether
the agent finishes the job, and a cheaper perception path that quietly degraded
accuracy would be worse than no optimisation at all.

I optimised the proxy for eight versions before checking. When I finally built a
task harness -- real tasks, driven through the real tool surface, graded against
each application's own state rather than against OCR -- it immediately broke
three things Calculator had been hiding, including one where the benchmark's own
console output contained the words it was searching for, so the agent clicked
them.

The result, once those were fixed: accuracy identical across every
configuration, token cost down 23x. But I would not have known, and the honest
version of this post would have been "here are some numbers I did not check".

## Neither pixels nor accessibility wins

The design bets that no single perception method is enough. That is arguable, so
I measured it by turning each half off:

| | Calculator | File Explorer | Chrome |
|---|---|---|---|
| both | **9/9** | **3/3** | **3/3** |
| accessibility only | 9/9 | **0/3** | **0/3** |
| pixels only | **6/9** | 3/3 | 3/3 |

Accessibility-only -- the posture most Windows GUI agents take -- is perfect on
XAML and blind on a Win32 list view and on web content. Point it at VS Code and
it sees **18 elements**, the entire IDE being one node named `Chrome Legacy
Window`, while OCR reads 94 from the same frame.

Pixels-only fails Calculator's buttons, because the button a human reads as `7`
is *named* `Seven`, and Windows OCR returns no digits from Calculator at all.
The label a person sees and the label a machine exposes are different strings.

## Against the incumbent

Same tasks, graded by the applications themselves, against
[Windows-MCP](https://github.com/CursorTouch/Windows-MCP):

| | passed | tokens |
|---|---|---|
| oswright | 19/20 | **832** |
| Windows-MCP, snapshot per action | **20/20** | 14,053 |
| Windows-MCP, snapshot once | **20/20** | 8,214 |

**They were more reliable. Mine was 16.9x cheaper.** I built the harness and
chose the tasks, so reporting that the other way round would be the easiest lie
available.

The cost difference is mechanical rather than a tuning win: Windows-MCP returns
the screen to the agent and takes coordinates back, so a description of the
screen is charged to the model's context on every action. OSWright takes the
text and returns the outcome.

## What it is not

One laptop. Short tasks. Windows only. As a *product* Windows-MCP is far ahead --
OAuth, analytics, a watchdog, an installer, real users -- and its accessibility
traversal reads Chrome page content that mine misses.

And wall-clock barely moved, because perception was never the bottleneck for
these tasks. The win is tokens and per-observation latency. Saying otherwise
would be overselling a real result.

## Try it

```
pip install oswright
```

Or point any MCP client at `uvx oswright`.

Everything above is reproducible from
[`benchmarks/`](https://github.com/Ask-812/oswright/tree/master/benchmarks). The
reasoning, including thirty things I got wrong, is in
[`docs/ENGINEERING_LOG.md`](https://github.com/Ask-812/oswright/blob/master/docs/ENGINEERING_LOG.md).
