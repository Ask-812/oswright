# Benchmarks

Every performance claim in the README and in `docs/ENGINEERING_LOG.md` comes
from one of these. They run against the live desktop, so results depend on
what is on screen — run them yourself rather than trusting the numbers below.

```bash
python benchmarks/bench_change.py     # how much the screen actually changes
python benchmarks/bench_methods.py    # cost of each perception method
python benchmarks/bench_pipeline.py   # v0.4.0 path vs v0.5.0 path, end to end
python benchmarks/bench_atlas.py      # cost of a return visit to a known screen
```

Reference machine: HP EliteBook 840 G8, Intel Iris Xe, 16 GB RAM, 1920×1080 at
125% scaling, Windows 11, Python 3.13. The *ratios* transfer; the absolute
milliseconds do not.

## `bench_change.py` — the premise

Establishes that re-reading the whole screen is wasted work.

```
changed pixels per frame        median  0.012%   mean 4.297%   max 93.150%
changed 64px tiles per frame    median  0.417%   mean 8.587%   max 100.000%
=> analysing only dirty tiles is ~240x less work
```

The mean is ~350× the median: almost every observation is of a nearly-static
screen, punctuated by rare full repaints. Tuning for the mean would optimise a
case that essentially never occurs.

## `bench_methods.py` — the cost table

Orders the resolution cascade.

```
method                                    median ms   result
screen capture (mss)                          48.48   1920x1080
OCR, full screen                             197.33   256 elements
OCR, quarter of screen                        71.57   92 elements
OCR, sixteenth of screen                      27.73   25 elements
capture 256x176 region                        17.45   2.2% of screen area
UIA tree walk (foreground window)            479.79   358 elements
UIA TextPattern read                         685.74   157 text ranges
UIA FindText (exact string)                  492.61   0 hits
compositor change poll (no pixels)             0.14   DXGI dirty rects
```

Two counter-intuitive results:

- **`capture 256x176 region` costs the same as any other region.** `mss` has a
  fixed ~16.6 ms per-grab cost, so capturing only the dirty parts of the screen
  is *slower* than one full grab. Capture stays whole-frame; only analysis is
  regional.
- **The accessibility tree can be slower than OCR.** 480 ms here, and on Chrome
  537 ms while returning no page text at all. It is not the free lunch it is
  usually described as.

## `bench_pipeline.py` — end to end

```
                                med ms   total ms  med tokens  total tokens
v0.4.0 full OCR + shot           212.2       3139       2,764        38,696
v0.5.0 incremental delta          32.8       1520          49         3,930

latency : 6.5x faster (median per step)
tokens  : 10x fewer

lookup of known text:
  v0.4.0 (capture + full OCR) :    317.9 ms
  v0.5.0 (cascade rung 0)     :    0.055 ms   5,801x cheaper
```

## `bench_atlas.py` — the cost of a return visit

```
cold: full screen read                   125.2 ms
warm: recognise + verify                   1.41 ms
recall hit rate: 5/5
warm start is 89x cheaper than reading the screen

rejections (all should be None):
  blank screen            : None
  inverted screen         : None
  same pixels, other app  : None
```

The rejections matter more than the speedup. An atlas that returns a stale
layout makes the agent click somewhere arbitrary, so verification must fail
closed — note that the inverted screen is *recognised* by the layout signature
(inverting does not move edges) and then *rejected* by the pixel check. That
split is the design: the signature filters, verification guarantees.
