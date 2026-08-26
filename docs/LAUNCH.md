# Launch material

Drafts for posting this. Not part of the library, kept here so the claims stay
in one place and stay consistent with what `benchmarks/` actually produces.

Ground rules I would keep to:

- **Lead with the finding, not the tool.** The 0.012% measurement is the
  interesting thing; the software is the consequence.
- **Put the losing result in the post itself.** Windows-MCP was more reliable
  than oswright, 20/20 against 19/20. Saying so first is both honest and the
  most credible thing available -- nobody publishes their losses.
- **Never oversell wall-clock.** It barely moved. The win is tokens and
  per-observation latency, and claiming otherwise invites the one question
  there is no good answer to.
- **Answer every comment with a number or an admission**, not with adjectives.

---

## Show HN

**Title** (80 char limit; the number does the work)

```
Show HN: My desktop agent re-read the whole screen. 99.988% of it hadn't changed
```

Alternatives, if that reads as too cute:

```
Show HN: OSWright - desktop automation that re-reads only what changed
Show HN: The median GUI observation changes 0.012% of the screen
```

**URL**

```
https://github.com/Ask-812/oswright
```

**First comment** (post immediately; HN expects the author to explain)

> Author here.
>
> I built a desktop automation MCP server the obvious way first: screenshot,
> OCR the whole thing, hand the model an image, act, repeat. Then I measured
> how much of the screen actually changes between an agent's observations.
>
> The median is 0.012% of pixels. Half of all observations move less than a
> hundredth of a percent of what is on screen, and I was paying ~2,800 image
> tokens each time to find that out.
>
> So it now keeps a model of the screen and re-reads only the regions that
> moved. Windows already tracks dirty rectangles for the compositor, and
> Desktop Duplication exposes them, so "has anything changed?" costs 0.14 ms
> without transferring a pixel -- against ~33 ms to capture a frame and then
> discover it was identical. I ended up hand-writing the COM bindings because
> no Python package exposed the dirty-rect metadata.
>
> Same task, end to end: 25,122 tokens to 3,404.
>
> Three things I would rather you heard from me than found yourselves:
>
> 1. **Wall-clock barely moved.** Perception was never the bottleneck for these
>    tasks -- they are dominated by application start-up and waiting for the UI
>    to settle. The win is tokens and per-observation latency. Anyone claiming
>    an order-of-magnitude speedup from this would be overselling it.
>
> 2. **I lost the head-to-head on reliability.** Against Windows-MCP on the same
>    tasks, graded by the applications themselves: they passed 20/20, I passed
>    19/20. I used 16.9x fewer tokens. I built the harness and chose the tasks,
>    so reporting it the other way round would have been the easiest lie
>    available. Their accessibility traversal also reads Chrome page content
>    that mine misses.
>
> 3. **I optimised a proxy metric for eight versions before checking it.** All
>    that work targeted perception *cost*. Whether the agent still completed the
>    task was unmeasured until near the end, and when I finally built the
>    harness it immediately broke three things -- including one where the
>    benchmark's own console output contained the words it was searching for, so
>    the agent clicked them.
>
> Everything is reproducible from `benchmarks/`. The reasoning, including 30
> things I got wrong, is in `docs/ENGINEERING_LOG.md`, which is honestly the
> part I would read first.
>
> Windows only for now. Happy to answer anything.

---

## r/mcp and r/LocalLLaMA

Same content, less formal. Reddit rewards the specific finding over the
project, and punishes anything that reads as marketing.

**Title**

```
I measured how much of the screen actually changes between agent actions. Median: 0.012%
```

**Body**

> I was building a desktop automation MCP server and doing the standard thing --
> screenshot, OCR, hand the model an image, repeat -- when it occurred to me to
> check how much of the screen was actually different each time.
>
> Median across real interactions: **0.012% of pixels**. And the mean is 350x
> the median, so it is wildly skewed: almost everything is a caret blinking or
> a clock ticking, with occasional whole-screen changes.
>
> Some things I found while acting on that:
>
> - **Windows tells you what changed, for free.** The compositor already tracks
>   dirty rectangles. Desktop Duplication exposes them: 0.14 ms to establish
>   "nothing changed", versus ~33 ms to capture a frame and compare it.
> - **Capturing only the dirty regions is slower than capturing everything.**
>   `mss` has a ~16.6 ms floor per grab regardless of area, so region capture
>   was 2x worse. Capture stays whole-frame; only the analysis is regional.
> - **OCR cannot see isolated digits.** Pointed at Calculator, Windows OCR
>   returns 30 text elements -- DEG, MC, log, Trigonometry -- and not one digit.
>   Text recognisers are trained on words and lines, and a lone glyph on a
>   button has no line context.
> - **The label a human reads is not the label a machine exposes.** That button
>   is *named* `Seven`. An agent reasoning from a screenshot asks for the wrong
>   string and no perception work fixes it.
> - **OCR is not a stable identity.** `bravo_notes.txt` came back as
>   `bravo notes.b(t`. In a terminal, `Placement_Prep_current` reads as
>   `Placement _ Prep _ current`.
>
> Result: 25,122 tokens to 3,404 on the same task, with accuracy checked
> against each application's own state rather than against OCR.
>
> Also worth saying: on a head-to-head against Windows-MCP, they were more
> reliable than me (20/20 vs 19/20) while I used 16.9x fewer tokens. And
> wall-clock barely moved, because perception was never the bottleneck.
>
> Code and benchmarks: https://github.com/Ask-812/oswright

---

## Anticipated questions

**"Isn't this just dirty-rectangle caching from VNC in the 1990s?"**
Yes, and that is the point. It is well-understood technology that no published
GUI agent applies. The novelty is the application, not the mechanism.

**"Why is wall-clock flat if perception is 23x cheaper?"**
Because perception was never the bottleneck for these tasks. They are dominated
by application start-up and the deliberate settle between actions. The win is
tokens and per-observation latency. This is the sharpest question available and
the answer is simply to concede it.

**"You chose the tasks."**
True, and it is why the ablations matter more than the head-to-head: turning off
each perception path in turn produces failures that are properties of the
surfaces, not of the tasks. Accessibility-only scores 0/3 on a Win32 list view;
pixels-only scores 6/9 on Calculator.

**"Windows only?"**
Yes. The compositor integration is Desktop Duplication, which is Windows. The
cascade design is portable; the fast path is not, yet.

**"Why not just use the accessibility tree?"**
Because it is blind on Electron. Point it at VS Code and it returns 18 elements
for the entire IDE -- one node named `Chrome Legacy Window` -- while OCR reads
94 from the same frame, including every filename.

**"How do I know the numbers are real?"**
Run them. Every claim comes from a script in `benchmarks/`, including the
head-to-head, which installs the competitor and drives both.
