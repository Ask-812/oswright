"""
Tests for the incremental perception layer.

These use synthetic images and a stub OCR engine, so they run without a display
or an OCR backend and are safe in CI.
"""

import platform

import pytest
from PIL import Image, ImageDraw

from oswright.dirty import (
    TILE,
    DirtyTracker,
    Region,
    merge_regions,
    tile_signature,
)


def frame(size=(640, 480), color="white"):
    return Image.new("RGB", size, color)


def with_box(img, box, color="black"):
    out = img.copy()
    ImageDraw.Draw(out).rectangle(box, fill=color)
    return out


class TestRegion:
    def test_dimensions(self):
        r = Region(10, 20, 40, 60)
        assert (r.width, r.height, r.area) == (30, 40, 1200)

    def test_intersects(self):
        a = Region(0, 0, 10, 10)
        assert a.intersects(Region(5, 5, 15, 15))
        assert not a.intersects(Region(10, 0, 20, 10)), "touching edges do not overlap"
        assert not a.intersects(Region(100, 100, 110, 110))

    def test_union(self):
        assert Region(0, 0, 10, 10).union(Region(20, 5, 30, 40)) == Region(0, 0, 30, 40)

    def test_clamp(self):
        assert Region(-50, -50, 5000, 5000).clamp(800, 600) == Region(0, 0, 800, 600)

    def test_padded_is_wider_than_tall(self):
        """Text runs horizontally, so cutting a line vertically splits words."""
        r = Region(100, 100, 200, 120).padded()
        assert r.width - 100 > r.height - 20


class TestTileSignature:
    def test_identical_frames_match(self):
        a = frame()
        assert (tile_signature(np_of(a)) == tile_signature(np_of(a))).all()

    def test_change_is_detected(self):
        a = frame()
        b = with_box(a, [100, 100, 140, 130])
        assert not (tile_signature(np_of(a)) == tile_signature(np_of(b))).all()

    def test_position_weighting_catches_rearrangement(self):
        """A plain sum would collide when pixels merely move within a tile."""
        a = frame((TILE, TILE))
        b = a.copy()
        a.putpixel((2, 2), (0, 0, 0))
        b.putpixel((40, 40), (0, 0, 0))
        assert not (tile_signature(np_of(a)) == tile_signature(np_of(b))).all()

    def test_rejects_non_image_array(self):
        import numpy as np

        with pytest.raises(ValueError):
            tile_signature(np.zeros((10, 10)))


class TestMergeRegions:
    def test_overlapping_merge(self):
        merged = merge_regions([Region(0, 0, 10, 10), Region(5, 5, 20, 20)])
        assert merged == [Region(0, 0, 20, 20)]

    def test_disjoint_kept_separate(self):
        regions = [Region(0, 0, 10, 10), Region(500, 500, 510, 510)]
        assert len(merge_regions(regions)) == 2

    def test_chain_merges_transitively(self):
        chain = [Region(0, 0, 10, 10), Region(8, 0, 20, 10), Region(18, 0, 30, 10)]
        assert merge_regions(chain) == [Region(0, 0, 30, 10)]

    def test_empty(self):
        assert merge_regions([]) == []


class TestDirtyTracker:
    def test_first_frame_is_fully_dirty(self):
        regions = DirtyTracker().update(frame((800, 600)))
        assert regions == [Region(0, 0, 800, 600)]

    def test_identical_frame_is_clean(self):
        t = DirtyTracker()
        img = frame()
        t.update(img)
        assert t.update(img) == []

    def test_small_change_yields_small_region(self):
        t = DirtyTracker()
        img = frame((1920, 1080))
        t.update(img)
        regions = t.update(with_box(img, [900, 500, 940, 520]))
        assert len(regions) == 1
        assert DirtyTracker.coverage(regions, 1920, 1080) < 0.05

    def test_change_is_covered_by_reported_region(self):
        """The whole point: the changed pixels must be inside a dirty region."""
        t = DirtyTracker()
        img = frame((1920, 1080))
        t.update(img)
        box = (900, 500, 940, 520)
        regions = t.update(with_box(img, list(box)))
        changed = Region(*box)
        assert any(
            r.left <= changed.left and r.top <= changed.top
            and r.right >= changed.right and r.bottom >= changed.bottom
            for r in regions
        )

    def test_distant_changes_stay_separate(self):
        t = DirtyTracker()
        img = frame((1920, 1080))
        t.update(img)
        two = with_box(with_box(img, [50, 50, 90, 70]), [1700, 950, 1740, 970])
        assert len(t.update(two)) == 2

    def test_resize_forces_full_rescan(self):
        t = DirtyTracker()
        t.update(frame((1920, 1080)))
        assert t.update(frame((800, 600))) == [Region(0, 0, 800, 600)]

    def test_reset(self):
        t = DirtyTracker()
        img = frame()
        t.update(img)
        t.reset()
        assert t.update(img) == [Region(0, 0, *img.size)]


# --- ScreenModel, driven by a stub OCR so no backend is required -------------


class StubMatch:
    def __init__(self, text, left, top, width=40, height=12):
        self.text, self.left, self.top = text, left, top
        self.width, self.height = width, height
        self.confidence = 0.99


class StubCache:
    def invalidate(self):
        pass


class StubOCR:
    """Returns whatever text has been placed at given coordinates."""

    def __init__(self):
        self.items = []          # (text, left, top) in absolute coords
        self._cache = StubCache()
        self.calls = 0
        self.pixels = 0

    def read_all(self, image):
        self.calls += 1
        self.pixels += image.size[0] * image.size[1]
        # The model passes crops, so report anything inside this crop's bounds
        # relative to the crop, mirroring how a real engine behaves.
        origin = getattr(image, "_origin", (0, 0))
        w, h = image.size
        out = []
        for text, left, top in self.items:
            if origin[0] <= left < origin[0] + w and origin[1] <= top < origin[1] + h:
                out.append(StubMatch(text, left - origin[0], top - origin[1]))
        return out


class TaggedImage:
    """Wraps a PIL image so crops remember where they came from."""

    def __init__(self, img, origin=(0, 0)):
        self._img = img
        self._origin = origin

    @property
    def size(self):
        return self._img.size

    def convert(self, mode):
        return self._img.convert(mode)

    def crop(self, box):
        return TaggedImage(self._img.crop(box), (box[0], box[1]))


def np_of(img):
    import numpy as np

    return np.asarray(img.convert("RGB"))


class StubCapture:
    """Serves a fixed frame, so cascade rungs that capture can be tested."""

    def __init__(self, image=None):
        self.image = image if image is not None else TaggedImage(frame((640, 480)))
        self.calls = 0

    def screenshot(self, monitor=0, **kwargs):
        self.calls += 1
        return self.image

    def get_offset(self, region=None, monitor=0):
        return (0, 0)


@pytest.fixture
def model():
    from oswright.screenmodel import ScreenModel

    return ScreenModel(capture=StubCapture(), ocr=StubOCR())


class TestElement:
    def test_center(self):
        from oswright.screenmodel import Element

        e = Element("Save", Region(100, 50, 140, 70))
        assert e.center == (120, 60)

    def test_fingerprint_is_stable(self):
        from oswright.screenmodel import Element

        a = Element("Save", Region(10, 10, 50, 30))
        b = Element("Save", Region(10, 10, 50, 30))
        assert a.fingerprint == b.fingerprint

    def test_fingerprint_tracks_text_and_position(self):
        from oswright.screenmodel import Element

        base = Element("Save", Region(10, 10, 50, 30))
        assert base.fingerprint != Element("Open", Region(10, 10, 50, 30)).fingerprint
        assert base.fingerprint != Element("Save", Region(11, 10, 51, 30)).fingerprint


class TestScreenModel:
    def test_first_observation_scans_everything(self, model):
        model._ocr.items = [("Hello", 100, 100)]
        delta = model.observe(image=TaggedImage(frame((640, 480))))
        assert delta.full_rescan is True, "nothing is known yet, so everything is read"
        assert delta.scanned_fraction == pytest.approx(1.0, abs=0.01)
        assert [e.text for e in delta.added] == ["Hello"]

    def test_unchanged_frame_scans_nothing(self, model):
        model._ocr.items = [("Hello", 100, 100)]
        img = TaggedImage(frame((640, 480)))
        model.observe(image=img)
        calls = model._ocr.calls

        delta = model.observe(image=img)
        assert delta.regions == []
        assert delta.changed is False
        assert model._ocr.calls == calls, "an unchanged screen must not re-OCR"

    def test_elements_persist_across_observations(self, model):
        model._ocr.items = [("Hello", 100, 100)]
        img = TaggedImage(frame((640, 480)))
        model.observe(image=img)
        model.observe(image=img)
        assert [e.text for e in model.elements] == ["Hello"]

    def test_new_text_reported_as_added(self, model):
        base = frame((640, 480))
        model._ocr.items = [("Hello", 20, 20)]
        model.observe(image=TaggedImage(base))

        model._ocr.items.append(("World", 300, 300))
        changed = with_box(base, [300, 300, 340, 312])
        delta = model.observe(image=TaggedImage(changed))

        assert [e.text for e in delta.added] == ["World"]
        assert delta.removed == []
        assert {e.text for e in model.elements} == {"Hello", "World"}

    def test_vanished_text_reported_as_removed(self, model):
        base = frame((640, 480))
        model._ocr.items = [("Hello", 20, 20), ("World", 300, 300)]
        model.observe(image=TaggedImage(base))

        model._ocr.items = [("Hello", 20, 20)]
        delta = model.observe(image=TaggedImage(with_box(base, [300, 300, 340, 312])))

        assert [e.text for e in delta.removed] == ["World"]
        assert {e.text for e in model.elements} == {"Hello"}

    def test_untouched_regions_are_not_rescanned(self, model):
        """The saving only exists if a far-away change leaves the rest alone."""
        base = frame((1920, 1080))
        model._ocr.items = [("Hello", 20, 20), ("World", 1700, 1000)]
        model.observe(image=TaggedImage(base))

        pixels_before = model._ocr.pixels
        model.observe(image=TaggedImage(with_box(base, [1700, 1000, 1740, 1012])))
        scanned = model._ocr.pixels - pixels_before
        assert scanned < 1920 * 1080 * 0.2

    def test_invalidated_elements_are_fully_rescanned(self, model):
        """
        Anything dropped must be inside a scanned region.

        Otherwise a dirty rect clipping a text box deletes the element and
        re-detects only the fragment, silently losing text.
        """
        base = frame((1920, 1080))
        wide = ("A very wide label spanning many tiles", 200, 500)
        model._ocr.items = [wide]
        model.observe(image=TaggedImage(base))
        tracked = model.elements[0]

        # Touch one end of that label only.
        delta = model.observe(image=TaggedImage(with_box(base, [205, 500, 215, 512])))
        assert any(
            r.left <= tracked.region.left and r.right >= tracked.region.right
            for r in delta.regions
        ), "region did not grow to cover the element it invalidated"

    def test_find_is_case_insensitive_substring(self, model):
        model._ocr.items = [("Save As...", 10, 10)]
        model.observe(image=TaggedImage(frame((640, 480))))
        assert model.find("save")
        assert model.find("SAVE AS")
        assert not model.find("Open")

    def test_find_exact(self, model):
        model._ocr.items = [("Save As...", 10, 10), ("Save", 10, 100)]
        model.observe(image=TaggedImage(frame((640, 480))))
        assert len(model.find("Save")) == 2
        assert len(model.find("Save", exact=True)) == 1

    def test_reset_clears_state(self, model):
        model._ocr.items = [("Hello", 10, 10)]
        img = TaggedImage(frame((640, 480)))
        model.observe(image=img)
        model.reset()
        assert model.elements == []
        assert model.observe(image=img).regions != []

    def test_efficiency_reports_savings(self, model):
        base = frame((1920, 1080))
        model._ocr.items = [("Hello", 20, 20)]
        model.observe(image=TaggedImage(base))
        for _ in range(4):
            model.observe(image=TaggedImage(base))
        eff = model.efficiency()
        assert eff["observations"] == 5
        assert eff["fraction_scanned"] < 0.5

    def test_delta_serialises_without_pixels(self, model):
        model._ocr.items = [("Hello", 10, 10)]
        payload = model.observe(image=TaggedImage(frame((640, 480)))).to_dict()
        assert set(payload) >= {"changed", "added", "removed", "duration_ms"}
        assert "image" not in payload


class TestCompositorFastPath:
    """
    The compositor is used only as a fast negative.

    It answers "did anything change?" without transferring pixels, which is
    ~50x cheaper than capturing a frame. But it must never be trusted to say
    *what* changed: its rectangles are measured over a slightly different
    interval than the capture that follows, so they can under-report, and an
    under-reported region is text that never gets re-read.
    """

    def test_no_baseline_means_no_skip(self):
        """With nothing observed yet there is no state to preserve."""
        tracker = DirtyTracker()
        assert tracker.nothing_changed() is False

    def test_disabled_compositor_never_skips(self):
        tracker = DirtyTracker(use_compositor=False)
        tracker.update(frame((640, 480)))
        assert tracker.nothing_changed() is False
        assert tracker.compositor_active is False

    def test_unavailable_source_falls_back_silently(self, monkeypatch):
        tracker = DirtyTracker()
        tracker.update(frame((640, 480)))
        monkeypatch.setattr(tracker, "_compositor_tried", True)
        monkeypatch.setattr(tracker, "_compositor", None)
        assert tracker.nothing_changed() is False

    def test_skips_only_on_positive_confirmation(self, monkeypatch):
        """None means 'could not tell' and must not be read as 'unchanged'."""
        tracker = DirtyTracker()
        tracker.update(frame((640, 480)))

        class Source:
            failure_reason = None

            def __init__(self, answer):
                self.answer = answer

            def poll(self, timeout_ms=0):
                return self.answer

            def close(self):
                pass

        tracker._compositor_tried = True

        tracker._compositor = Source(None)          # could not tell
        assert tracker.nothing_changed() is False

        tracker._compositor = Source([(0, 0, 10, 10)])  # something moved
        assert tracker.nothing_changed() is False

        tracker._compositor = Source([])            # positively unchanged
        assert tracker.nothing_changed() is True
        assert tracker.compositor_skips == 1

    def test_close_is_safe_without_a_source(self):
        DirtyTracker(use_compositor=False).close()

    def test_capture_frame_rejects_wrong_size(self):
        """
        Desktop Duplication output 0 is the *primary monitor*; capture backends
        treat index 0 as the whole virtual desktop. On a multi-monitor setup
        those are different images, and substituting one for the other would
        put every derived coordinate in the wrong place.
        """
        tracker = DirtyTracker()

        class Source:
            failure_reason = None

            def poll(self, timeout_ms=0):
                return [(0, 0, 10, 10)]

            def capture(self):
                return frame((1920, 1080))

            def close(self):
                pass

        tracker._compositor_tried = True
        tracker._compositor = Source()

        assert tracker.capture_frame(expected_size=(3840, 1080)) is None
        assert tracker.capture_frame(expected_size=(1920, 1080)) is not None
        assert tracker.compositor_captures == 1

    def test_capture_frame_without_source_returns_none(self):
        tracker = DirtyTracker(use_compositor=False)
        assert tracker.capture_frame() is None

    def test_capture_frame_tolerates_source_failure(self):
        tracker = DirtyTracker()

        class Source:
            failure_reason = "simulated"

            def poll(self, timeout_ms=0):
                return None

            def capture(self):
                return None

            def close(self):
                pass

        tracker._compositor_tried = True
        tracker._compositor = Source()
        assert tracker.capture_frame() is None


class TestDxgiHelpers:
    def test_hresult_extraction(self):
        dxgi = pytest.importorskip("oswright._dxgi_windows")

        class ComLike(Exception):
            hresult = -2005270489

        assert dxgi._hresult_of(ComLike()) == -2005270489
        assert dxgi._hresult_of(OSError(5, "denied")) == 5
        assert dxgi._hresult_of(Exception("no code")) is None

    def test_timeout_constant_matches_dxgi(self):
        """DXGI_ERROR_WAIT_TIMEOUT is 0x887A0027 as a signed 32-bit HRESULT."""
        dxgi = pytest.importorskip("oswright._dxgi_windows")
        assert dxgi.DXGI_ERROR_WAIT_TIMEOUT & 0xFFFFFFFF == 0x887A0027

    def test_source_reports_unavailability_rather_than_raising(self):
        dxgi = pytest.importorskip("oswright._dxgi_windows")
        source = dxgi.DxgiDirtySource(output_index=99)  # no such output
        result = source.poll()
        assert result is None
        assert source.failure_reason
        source.close()

    def test_capture_without_a_held_frame_returns_none(self):
        """capture() must never invent pixels; it reads a frame poll() acquired."""
        dxgi = pytest.importorskip("oswright._dxgi_windows")
        source = dxgi.DxgiDirtySource()
        assert source.capture() is None  # nothing acquired yet
        source.close()

    def test_desktop_format_whitelist_is_bgra(self):
        """The staging copy interprets pixels as BGRX; other formats must be refused."""
        dxgi = pytest.importorskip("oswright._dxgi_windows")
        # DXGI_FORMAT_B8G8R8A8_UNORM = 87, _UNORM_SRGB = 91
        assert 87 in dxgi._SUPPORTED_FORMATS
        assert 91 in dxgi._SUPPORTED_FORMATS
        assert 28 not in dxgi._SUPPORTED_FORMATS  # R8G8B8A8, channel order differs


class TestCascadeRanking:
    def test_exact_match_outranks_containing_line(self):
        from oswright.cascade import Candidate, _rank

        line = Candidate("Save the document now", Region(0, 0, 200, 12), "ocr", 0)
        button = Candidate("Save", Region(0, 50, 40, 62), "ocr", 0)
        assert _rank([line, button], "Save")[0] is button

    def test_shorter_match_preferred(self):
        from oswright.cascade import Candidate, _rank

        short = Candidate("Save As", Region(0, 0, 60, 12), "ocr", 0)
        long = Candidate("Save As A Copy Somewhere", Region(0, 50, 200, 62), "ocr", 0)
        assert _rank([long, short], "Save")[0] is short

    def test_resolution_reports_failure_clearly(self):
        from oswright.cascade import Resolution

        payload = Resolution(query="nope").to_dict()
        assert payload["found"] is False
        assert "error" in payload

    def test_resolution_reports_rung(self):
        from oswright.cascade import Candidate, Resolution

        best = Candidate("Save", Region(10, 10, 50, 22), "model", 0)
        payload = Resolution(
            query="Save", found=True, best=best, candidates=[best], rung=0
        ).to_dict()
        assert payload["rung"] == 0
        assert (payload["x"], payload["y"]) == (30, 16)


class TestShortQueryRouting:
    """
    Very short targets go to the accessibility tree first.

    OCR does not merely misread isolated glyphs, it does not detect them.
    Measured on Calculator, Windows OCR returned 30 text elements from the
    window (DEG, MC, Function, Trigonometry, log...) and not one digit: text
    recognisers are trained on words and lines, and a lone character on a button
    has no line context to belong to. For such targets the pixel rungs are a
    guaranteed miss, not a cheaper path to the same answer.
    """

    def test_short_queries_prefer_accessibility(self):
        from oswright.cascade import _prefers_accessibility

        for query in ("7", "8", "OK", "X"):
            assert _prefers_accessibility(query, exact=False), query

    def test_word_queries_do_not(self):
        from oswright.cascade import _prefers_accessibility

        for query in ("Save", "Cancel", "File", "Multiply by"):
            assert not _prefers_accessibility(query, exact=False), query

    def test_whitespace_does_not_inflate_length(self):
        from oswright.cascade import _prefers_accessibility

        assert _prefers_accessibility("  7  ", exact=False)

    def test_short_query_tries_uia_before_pixels(self, model):
        """The routing must show up in the order rungs are attempted."""
        from oswright.cascade import resolve

        model._ocr.items = []
        model.observe(image=TaggedImage(frame((640, 480))))

        result = resolve("7", model, allow_text_pattern=False, allow_full_rescan=False)
        assert result.rungs_tried[:2] == ["model", "uia-first"]

    def test_long_query_keeps_the_cheap_path_first(self, model):
        from oswright.cascade import resolve

        model._ocr.items = []
        model.observe(image=TaggedImage(frame((640, 480))))

        result = resolve("Save", model, allow_text_pattern=False, allow_full_rescan=False)
        assert result.rungs_tried[:2] == ["model", "incremental"]


class TestCascadeOrder:
    def test_model_hit_costs_nothing_when_current(self, model, monkeypatch):
        """When the model is provably current, rung 0 answers from memory."""
        from oswright.cascade import resolve

        model._ocr.items = [("Save", 100, 200)]
        model.observe(image=TaggedImage(frame((640, 480))))
        calls = model._ocr.calls

        monkeypatch.setattr(model, "is_current", lambda: True)
        result = resolve("Save", model, allow_uia=False, allow_text_pattern=False)
        assert result.found
        assert result.rung == 0
        assert result.rungs_tried == ["model"]
        assert model._ocr.calls == calls, "rung 0 must not perceive anything"

    def test_stale_model_is_never_trusted(self, model, monkeypatch):
        """
        A model that cannot be confirmed current must not answer from memory.

        This is the regression a Calculator-only benchmark could not see,
        because Calculator always reopened in the same place. Against VS Code
        the unverified rung returned a coordinate from a previous window --
        (600, 66) for a file at (211, 157) -- and the agent clicked chrome.

        The earlier version of this test asserted only that rung 0 was fast,
        which is how the behaviour survived: it pinned the optimisation in
        place and said nothing about whether the answer was right.
        """
        from oswright.cascade import resolve

        model._ocr.items = [("Save", 100, 200)]
        model.observe(image=TaggedImage(frame((640, 480))))

        monkeypatch.setattr(model, "is_current", lambda: False)
        result = resolve(
            "Save", model,
            allow_uia=False, allow_text_pattern=False, allow_full_rescan=False,
        )
        assert result.rungs_tried[0] == "model"
        assert result.rung != 0, "answered from an unverified model"

    def test_missing_text_walks_the_cascade(self, model):
        from oswright.cascade import resolve

        model._ocr.items = [("Save", 100, 200)]
        model.observe(image=TaggedImage(frame((640, 480))))

        result = resolve(
            "Nonexistent", model,
            allow_uia=False, allow_text_pattern=False, allow_full_rescan=False,
        )
        assert not result.found
        assert result.rungs_tried == ["model", "incremental"]


# ---------------------------------------------------------------------------
# Shared compositor ownership
# ---------------------------------------------------------------------------
#
# Windows grants one Desktop Duplication per output per process. Two trackers
# exist in the server (settle detection and the screen model), and when each
# built its own, the second failed with E_INVALIDARG and silently degraded to
# hashing. Each component benchmarked fine alone, which is exactly why this
# needs a test rather than a benchmark.


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows only")
def test_trackers_share_one_duplication():
    from oswright._dxgi_windows import is_available, shared_refcount

    if not is_available():
        pytest.skip("Desktop Duplication unavailable")

    before = shared_refcount()
    first, second = DirtyTracker(), DirtyTracker()
    try:
        source = first._get_compositor()
        if source is None or source.failure_reason is not None:
            pytest.skip("compositor not usable in this session")

        assert second._get_compositor() is source, "each tracker built its own"
        assert second.compositor_active, "second tracker degraded to hashing"
        assert shared_refcount() == before + 2
    finally:
        first.close()
        second.close()

    assert shared_refcount() == before, "closing did not release the duplication"


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows only")
def test_tracker_can_reacquire_after_close():
    """A closed tracker must be able to borrow again, not stay dead."""
    from oswright._dxgi_windows import is_available

    if not is_available():
        pytest.skip("Desktop Duplication unavailable")

    tracker = DirtyTracker()
    was_active = tracker.compositor_active
    tracker.close()
    if not was_active:
        pytest.skip("compositor not usable in this session")

    try:
        assert tracker.compositor_active, "tracker stayed dead after close()"
    finally:
        tracker.close()


# ---------------------------------------------------------------------------
# Approximate text matching
# ---------------------------------------------------------------------------
#
# Windows OCR read `bravo_notes.txt` as `bravo notes.b(t`: the underscore came
# back as a space and the `x` as `(`. The text was plainly on screen and a
# literal search still missed it, which failed a task for no perception reason.
# The fallback exists for that, and must refuse rather than guess.

MANGLED = ["alpha_notes.b(t", "bravo notes.b(t", "charlie notes.b(t"]


def _named(text, x=0):
    from oswright.detect import ElementMatch

    return ElementMatch(x=x, y=0, left=x, top=0, width=10, height=10,
                        confidence=0.9, text=text, method="ocr")


class TestFuzzyMatching:
    def test_recovers_text_the_recogniser_mangled(self):
        from oswright.detect import fuzzy_select

        got = fuzzy_select(
            [_named(t, i) for i, t in enumerate(MANGLED)],
            "bravo_notes.txt", lambda e: e.text,
        )
        assert [e.text for e in got] == ["bravo notes.b(t"]

    def test_picks_the_right_one_among_similar_names(self):
        """Three near-identical filenames must not collapse into one answer."""
        from oswright.detect import fuzzy_select

        for target, expected in (
            ("alpha_notes.txt", "alpha_notes.b(t"),
            ("charlie_notes.txt", "charlie notes.b(t"),
        ):
            got = fuzzy_select(
                [_named(t, i) for i, t in enumerate(MANGLED)],
                target, lambda e: e.text,
            )
            assert [e.text for e in got] == [expected]

    def test_refuses_when_two_candidates_are_indistinguishable(self):
        """Clicking the wrong element is worse than reporting a miss."""
        from oswright.detect import fuzzy_select

        got = fuzzy_select(
            [_named("bravo notes.b(t", 0), _named("bravo notes.b1t", 1)],
            "bravo_notes.txt", lambda e: e.text,
        )
        assert got == []

    def test_refuses_short_targets(self):
        """Short strings approximately match almost anything."""
        from oswright.detect import fuzzy_select

        assert fuzzy_select([_named("ab_x")], "ab", lambda e: e.text) == []

    def test_refuses_unrelated_text(self):
        from oswright.detect import fuzzy_select

        assert fuzzy_select(
            [_named("totally different")], "bravo_notes.txt", lambda e: e.text
        ) == []

    def test_returns_every_box_carrying_the_winning_text(self):
        """A recogniser reports the same string as both a word and a line."""
        from oswright.detect import fuzzy_select

        got = fuzzy_select(
            [_named("bravo notes.b(t", 0), _named("bravo notes.b(t", 500)],
            "bravo_notes.txt", lambda e: e.text,
        )
        assert len(got) == 2

    def test_literal_match_is_preferred_and_unchanged(self, model):
        """The fallback must never alter what a literal search already answers."""
        model._ocr.items = [("Save", 100, 200), ("Saved", 300, 400)]
        model.observe(image=TaggedImage(frame((640, 480))))

        hits = model.find("Save")
        assert {h.text for h in hits} == {"Save", "Saved"}

    def test_model_recovers_mangled_text(self, model):
        model._ocr.items = [("bravo notes.b(t", 100, 200)]
        model.observe(image=TaggedImage(frame((640, 480))))

        assert [h.text for h in model.find("bravo_notes.txt")] == ["bravo notes.b(t"]


class TestSingleModePostures:
    """
    The cascade claims neither pixels nor accessibility wins everywhere.

    These switches exist so that claim can be measured against the single-mode
    alternatives rather than argued, so they need to actually disable a path.
    """

    def test_pixels_disabled_skips_every_pixel_rung(self, model):
        from oswright.cascade import resolve

        model._ocr.items = [("Save", 100, 200)]
        model.observe(image=TaggedImage(frame((640, 480))))
        calls = model._ocr.calls

        result = resolve(
            "Save", model, allow_uia=False, allow_text_pattern=False,
            allow_pixels=False,
        )
        assert not result.found, "answered from a pixel rung that was disabled"
        assert "incremental" not in result.rungs_tried
        assert "full-rescan" not in result.rungs_tried
        assert model._ocr.calls == calls, "perceived with pixels disabled"

    def test_pixels_enabled_still_answers(self, model, monkeypatch):
        from oswright.cascade import resolve

        model._ocr.items = [("Save", 100, 200)]
        model.observe(image=TaggedImage(frame((640, 480))))
        monkeypatch.setattr(model, "is_current", lambda: True)

        result = resolve(
            "Save", model, allow_uia=False, allow_text_pattern=False,
            allow_pixels=True,
        )
        assert result.found


class TestWindowScoping:
    """
    `window_title` has to constrain the pixel rungs, not just accessibility.

    The pixel rungs read the whole screen. Without scoping, a request to click
    "Eight" in Calculator can land on the word "Eight" in an unrelated window
    that happens to be visible -- which is exactly what happened when this
    benchmark's own console output contained the labels it was asking for.
    """

    def test_matches_outside_the_named_window_are_discarded(self, model, monkeypatch):
        import oswright.cascade as C

        model._ocr.items = [("Eight", 1587, 452)]
        model.observe(image=TaggedImage(frame((1920, 1080))))
        monkeypatch.setattr(model, "is_current", lambda: True)
        monkeypatch.setattr(C, "_window_bounds", lambda title: (767, 297, 1185, 972))

        result = C.resolve(
            "Eight", model, window_title="Calculator",
            allow_uia=False, allow_text_pattern=False, allow_full_rescan=False,
        )
        assert not result.found, "clicked text belonging to another window"

    def test_matches_inside_the_named_window_are_kept(self, model, monkeypatch):
        import oswright.cascade as C

        model._ocr.items = [("Eight", 976, 796)]
        model.observe(image=TaggedImage(frame((1920, 1080))))
        monkeypatch.setattr(model, "is_current", lambda: True)
        monkeypatch.setattr(C, "_window_bounds", lambda title: (767, 297, 1185, 972))

        result = C.resolve(
            "Eight", model, window_title="Calculator",
            allow_uia=False, allow_text_pattern=False, allow_full_rescan=False,
        )
        assert result.found
        assert result.rung == 0

    def test_no_window_named_means_no_filtering(self, model, monkeypatch):
        """Filtering against an unknown rectangle would discard every answer."""
        import oswright.cascade as C

        model._ocr.items = [("Eight", 1587, 452)]
        model.observe(image=TaggedImage(frame((1920, 1080))))
        monkeypatch.setattr(model, "is_current", lambda: True)

        result = C.resolve(
            "Eight", model,
            allow_uia=False, allow_text_pattern=False, allow_full_rescan=False,
        )
        assert result.found
