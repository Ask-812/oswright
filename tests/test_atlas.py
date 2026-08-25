"""
Tests for the screen atlas.

The dangerous failure is reusing a remembered layout for a screen that is no
longer the same one, because that makes an agent click somewhere arbitrary.
Most of these tests are about that failing closed.

All synthetic — no display or OCR backend required.
"""

import numpy as np
import pytest
from PIL import Image, ImageDraw

from oswright.atlas import (
    MATCH_TOLERANCE,
    AtlasEntry,
    ScreenContext,
    UIAtlas,
    Verifier,
    _choose_verifier_regions,
    _thumbnail,
    _thumbnails_match,
    layout_signature,
    signature_distance,
)
from oswright.dirty import Region
from oswright.screenmodel import Element


def make_screen(label="Settings", extra=None, shift=0, noise=0, size=(800, 600)):
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.rectangle([50, 40, size[0] - 50, 90], outline="black", width=2)
    d.text((60, 58), label, fill="black")
    for i in range(6):
        y = 140 + i * 60 + shift
        d.rectangle([50, y, 320, y + 42], outline="black", width=2)
        d.text((62, y + 14), f"Field number {i}", fill="black")
    if extra:
        d.rectangle([400, 200, 740, 420], outline="black", width=3)
        d.text((420, 300), extra, fill="black")
    for i in range(noise):
        img.putpixel((700 + i % 60, 520 + i // 60), (0, 0, 0))
    return img


def elements_for(screen_label="Settings"):
    """Elements roughly where make_screen draws its text."""
    items = [Element(text=screen_label, region=Region(60, 52, 200, 72))]
    for i in range(6):
        y = 140 + i * 60
        items.append(
            Element(text=f"Field number {i}", region=Region(62, y + 8, 260, y + 30))
        )
    return items


@pytest.fixture
def context():
    return ScreenContext(app="app.exe", window_class="Win32", width=800, height=600)


@pytest.fixture
def atlas(tmp_path):
    return UIAtlas(path=tmp_path / "atlas.json", autoload=False)


class TestLayoutSignature:
    def test_identical_screens_match_exactly(self):
        a = layout_signature(make_screen())
        b = layout_signature(make_screen())
        assert signature_distance(a, b) == 0.0

    def test_tolerates_small_noise(self):
        """A blinking caret or ticking clock must not evict a remembered screen."""
        a = layout_signature(make_screen())
        b = layout_signature(make_screen(noise=200))
        assert signature_distance(a, b) <= MATCH_TOLERANCE

    def test_detects_structural_change(self):
        a = layout_signature(make_screen())
        b = layout_signature(make_screen(extra="A whole new panel"))
        assert signature_distance(a, b) > 0

    def test_mismatched_shapes_are_maximally_distant(self):
        a = layout_signature(make_screen(size=(800, 600)))
        b = np.zeros((4, 4), dtype=bool)
        assert signature_distance(a, b) == 1.0

    def test_signature_is_not_normalised_against_its_own_maximum(self):
        """
        Normalising by the frame's strongest edge would make every cell depend
        on one pixel anywhere on screen. Adding a hard black mark far from the
        layout must not perturb the rest of the signature.
        """
        plain = make_screen()
        marked = plain.copy()
        ImageDraw.Draw(marked).rectangle([770, 570, 780, 580], fill="black")
        assert signature_distance(
            layout_signature(plain), layout_signature(marked)
        ) <= MATCH_TOLERANCE


class TestThumbnailVerification:
    def test_identical_region_matches(self):
        screen = make_screen()
        region = Region(50, 140, 320, 182)
        assert _thumbnails_match(_thumbnail(screen, region), _thumbnail(screen, region))

    def test_changed_region_does_not_match(self):
        before = _thumbnail(make_screen(), Region(50, 40, 750, 90))
        after = _thumbnail(make_screen(label="Totally Different Heading"), Region(50, 40, 750, 90))
        assert not _thumbnails_match(before, after)

    def test_out_of_bounds_region_yields_nothing(self):
        screen = make_screen()
        assert _thumbnail(screen, Region(700, 500, 5000, 5000)) is None
        assert _thumbnail(screen, Region(-10, -10, 50, 50)) is None

    def test_degenerate_region_yields_nothing(self):
        assert _thumbnail(make_screen(), Region(10, 10, 12, 12)) is None

    def test_missing_thumbnails_never_match(self):
        assert not _thumbnails_match(None, b"x" * 128)
        assert not _thumbnails_match(b"x" * 128, None)
        assert not _thumbnails_match(b"x" * 4, b"x" * 128)


class TestVerifierSelection:
    def test_prefers_substantial_regions(self):
        tiny = Element(text="ok", region=Region(0, 0, 8, 6))
        big = Element(text="Something substantial", region=Region(0, 40, 300, 70))
        chosen = _choose_verifier_regions([tiny, big])
        assert big in chosen
        assert tiny not in chosen

    def test_returns_nothing_when_nothing_is_usable(self):
        tiny = [Element(text="x", region=Region(0, 0, 4, 4))]
        assert _choose_verifier_regions(tiny) == []

    def test_spreads_picks_vertically(self):
        elements = [
            Element(text=f"Row number {i}", region=Region(0, i * 50, 300, i * 50 + 30))
            for i in range(20)
        ]
        chosen = _choose_verifier_regions(elements, count=4)
        tops = [e.region.top for e in chosen]
        assert tops == sorted(tops)
        assert max(tops) - min(tops) > 300, "picks are clustered, not spread"


class TestAtlasRecall:
    def test_unknown_screen_is_not_recognised(self, atlas, context):
        assert atlas.lookup(make_screen(), context) is None

    def test_remembered_screen_is_recalled(self, atlas, context):
        screen = make_screen()
        atlas.remember(screen, context, elements_for())
        assert atlas.recall(screen, context) is not None

    def test_recall_survives_small_noise(self, atlas, context):
        atlas.remember(make_screen(), context, elements_for())
        assert atlas.recall(make_screen(noise=150), context) is not None

    def test_different_application_never_matches(self, atlas, context):
        screen = make_screen()
        atlas.remember(screen, context, elements_for())
        other = ScreenContext(app="other.exe", window_class="Win32", width=800, height=600)
        assert atlas.lookup(screen, other) is None

    def test_different_window_size_never_matches(self, atlas, context):
        screen = make_screen()
        atlas.remember(screen, context, elements_for())
        resized = ScreenContext(app="app.exe", window_class="Win32", width=1024, height=768)
        assert atlas.lookup(screen, resized) is None

    def test_changed_content_is_rejected_by_verification(self, atlas, context):
        """
        The important case. A screen whose layout still matches but whose
        content changed must not have its old element positions reused.
        """
        atlas.remember(make_screen(label="Settings"), context, elements_for("Settings"))
        changed = make_screen(label="Totally Different Heading")
        assert atlas.lookup(changed, context) is not None, "layout still matches"
        assert atlas.recall(changed, context) is None, "but content check must reject it"

    def test_entry_without_verifiers_is_never_stored(self, atlas, context):
        """A screen that cannot be checked later must not be remembered."""
        unusable = [Element(text="x", region=Region(0, 0, 4, 4))]
        assert atlas.remember(make_screen(), context, unusable) is None
        assert len(atlas) == 0

    def test_empty_element_list_is_not_stored(self, atlas, context):
        assert atlas.remember(make_screen(), context, []) is None

    def test_verify_fails_closed_without_verifiers(self, atlas, context):
        entry = AtlasEntry(
            signature=layout_signature(make_screen()),
            context_key=context.key(),
            elements=elements_for(),
            verifiers=[],
        )
        assert atlas.verify(entry, make_screen()) is False

    def test_verify_fails_closed_on_unreadable_region(self, atlas, context):
        entry = AtlasEntry(
            signature=layout_signature(make_screen()),
            context_key=context.key(),
            elements=elements_for(),
            verifiers=[
                Verifier(region=Region(0, 0, 9000, 9000), thumbnail=b"\x00" * 128)
            ],
        )
        assert atlas.verify(entry, make_screen()) is False


class TestAtlasBookkeeping:
    def test_revisiting_updates_rather_than_duplicates(self, atlas, context):
        atlas.remember(make_screen(), context, elements_for())
        atlas.remember(make_screen(noise=100), context, elements_for())
        assert len(atlas) == 1

    def test_distinct_screens_are_kept_separately(self, atlas, context):
        atlas.remember(make_screen(), context, elements_for())
        atlas.remember(make_screen(extra="An entirely new panel"), context, elements_for())
        assert len(atlas) == 2

    def test_eviction_respects_the_limit(self, tmp_path, context):
        small = UIAtlas(path=tmp_path / "a.json", max_entries=3, autoload=False)
        for i in range(6):
            ctx = ScreenContext(app=f"app{i}.exe", width=800, height=600)
            small.remember(make_screen(label=f"Screen {i}"), ctx, elements_for())
        assert len(small) <= 3

    def test_eviction_keeps_the_useful_entries(self, tmp_path):
        small = UIAtlas(path=tmp_path / "a.json", max_entries=2, autoload=False)
        kept_ctx = ScreenContext(app="kept.exe", width=800, height=600)
        screen = make_screen(label="Keep me")
        entry = small.remember(screen, kept_ctx, elements_for())
        entry.hits = 99  # proven useful

        for i in range(4):
            ctx = ScreenContext(app=f"filler{i}.exe", width=800, height=600)
            small.remember(make_screen(label=f"Filler {i}"), ctx, elements_for())

        assert any(e.hits == 99 for e in small.entries), "a proven entry was evicted"

    def test_forget_removes_an_entry(self, atlas, context):
        entry = atlas.remember(make_screen(), context, elements_for())
        atlas.forget(entry)
        assert len(atlas) == 0


class TestAtlasPersistence:
    def test_roundtrip(self, tmp_path, context):
        path = tmp_path / "atlas.json"
        first = UIAtlas(path=path, autoload=False)
        screen = make_screen()
        first.remember(screen, context, elements_for())
        assert first.save()

        second = UIAtlas(path=path)
        assert len(second) == 1
        recalled = second.recall(screen, context)
        assert recalled is not None
        assert [e.text for e in recalled.elements] == [e.text for e in elements_for()]

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert UIAtlas(path=tmp_path / "nope.json").load() is False

    def test_corrupt_file_degrades_to_empty(self, tmp_path):
        """A broken cache must never break the agent."""
        path = tmp_path / "atlas.json"
        path.write_text("{ this is not json", encoding="utf-8")
        loaded = UIAtlas(path=path)
        assert len(loaded) == 0

    def test_save_creates_missing_directories(self, tmp_path, context):
        path = tmp_path / "deep" / "nested" / "atlas.json"
        atlas = UIAtlas(path=path, autoload=False)
        atlas.remember(make_screen(), context, elements_for())
        assert atlas.save()
        assert path.exists()


class TestScreenContext:
    def test_key_excludes_the_title(self):
        """
        Titles track content ("report.txt" vs "notes.txt") while the layout does
        not, so including them would miss most legitimate matches.
        """
        a = ScreenContext(app="x.exe", window_class="C", title="report.txt", width=800, height=600)
        b = ScreenContext(app="x.exe", window_class="C", title="notes.txt", width=800, height=600)
        assert a.key() == b.key()

    def test_key_distinguishes_app_and_size(self):
        base = ScreenContext(app="x.exe", window_class="C", width=800, height=600)
        assert base.key() != ScreenContext(app="y.exe", window_class="C", width=800, height=600).key()
        assert base.key() != ScreenContext(app="x.exe", window_class="C", width=801, height=600).key()
