"""
Tests for speculative perception.

The dangerous failure is a prediction that is wrong but treated as right, since
the agent would then act on a screen that is not there. Most of these tests are
about that path.

All synthetic — no display or OCR backend required.
"""

import pytest
from test_atlas import elements_for, make_screen

from oswright.atlas import ScreenContext, UIAtlas
from oswright.dirty import Region
from oswright.screenmodel import Element
from oswright.settle import (
    QUIET_AREA_PX,
    SettleResult,
    wait_until_settled,
)
from oswright.speculate import (
    MIN_OBSERVATIONS,
    Prediction,
    Transition,
    TransitionModel,
    action_key,
)


@pytest.fixture
def context():
    return ScreenContext(app="app.exe", window_class="Win32", width=800, height=600)


@pytest.fixture
def atlas(tmp_path):
    return UIAtlas(path=tmp_path / "atlas.json", autoload=False)


@pytest.fixture
def model(atlas, tmp_path):
    return TransitionModel(atlas, path=tmp_path / "transitions.json", autoload=False)


class TestActionKey:
    def test_semantic_target_preferred_over_coordinates(self):
        assert action_key("click", target="Save") == "click:save"

    def test_whitespace_and_case_normalised(self):
        assert action_key("click", target="  Save   As  ") == action_key("click", target="save as")

    def test_nearby_coordinates_collapse(self):
        """Two clicks a few pixels apart are aimed at the same control."""
        assert action_key("click", x=100, y=200) == action_key("click", x=108, y=205)

    def test_distant_coordinates_differ(self):
        assert action_key("click", x=100, y=200) != action_key("click", x=400, y=200)

    def test_kind_alone_is_valid(self):
        assert action_key("scroll") == "scroll"


class TestTransitionTrust:
    def test_single_observation_is_not_trusted(self):
        """One observation could easily be a coincidence."""
        t = Transition(from_id="a", action="click:save", to_id="b")
        assert t.observations < MIN_OBSERVATIONS
        assert not t.trusted

    def test_repeated_observation_becomes_trusted(self):
        t = Transition(from_id="a", action="click:save", to_id="b", observations=2)
        assert t.trusted

    def test_consistently_wrong_transition_is_retired(self):
        t = Transition(
            from_id="a", action="click:save", to_id="b",
            observations=5, correct=1, wrong=4,
        )
        assert t.confidence < 0.5
        assert not t.trusted, "a transition that keeps being wrong must stop being used"

    def test_mostly_right_transition_stays_trusted(self):
        t = Transition(
            from_id="a", action="click:save", to_id="b",
            observations=5, correct=4, wrong=1,
        )
        assert t.trusted

    def test_confidence_with_no_attempts(self):
        assert Transition(from_id="a", action="x", to_id="b").confidence == 0.0


class TestPredictionCycle:
    def test_nothing_is_predicted_before_anything_is_learned(self, model, context):
        screen = make_screen()
        assert model.predict_and_verify("unknown", "click:save", screen, context).attempted is False

    def test_learning_then_predicting(self, model, atlas, context):
        before_screen = make_screen("Editor")
        after_screen = make_screen("Editor", extra="Save dialog is open")

        atlas.remember(before_screen, context, elements_for("Editor"))
        from_id = model.snapshot(before_screen, context)
        assert from_id is not None

        # First time: observed, not predicted.
        model.record(from_id, "click:save", after_screen, context, elements_for("Editor"))
        assert model.predict_and_verify(from_id, "click:save", after_screen, context).attempted is False

        # Seen twice, it becomes trustworthy.
        model.record(from_id, "click:save", after_screen, context, elements_for("Editor"))
        prediction = model.predict_and_verify(from_id, "click:save", after_screen, context)
        assert prediction.attempted
        assert prediction.correct
        assert model.last_entry is not None

    def test_wrong_outcome_is_reported_as_surprise(self, model, atlas, context):
        before_screen = make_screen("Editor")
        expected = make_screen("Editor", extra="Save dialog is open")

        atlas.remember(before_screen, context, elements_for("Editor"))
        from_id = model.snapshot(before_screen, context)
        # Elements include the panel, as they would in reality: the screen model
        # OCRs the post-action screen, so its new text is available to sample.
        after_elements = elements_for("Editor") + [
            Element(text="Save dialog is open", region=Region(410, 290, 700, 320))
        ]
        for _ in range(2):
            model.record(from_id, "click:save", expected, context, after_elements)

        # This time something else happened entirely.
        surprising = make_screen("Editor", extra="Disk full. Cannot save.")
        prediction = model.predict_and_verify(from_id, "click:save", surprising, context)

        assert prediction.attempted
        assert not prediction.correct
        assert prediction.surprise, "a failed prediction must say something happened"
        assert model.last_entry is None

    def test_failed_prediction_erodes_trust(self, model, atlas, context):
        before_screen = make_screen("Editor")
        expected = make_screen("Editor", extra="Save dialog is open")
        atlas.remember(before_screen, context, elements_for("Editor"))
        from_id = model.snapshot(before_screen, context)
        after_elements = elements_for("Editor") + [
            Element(text="Save dialog is open", region=Region(410, 290, 700, 320))
        ]
        for _ in range(2):
            model.record(from_id, "click:save", expected, context, after_elements)

        wrong = make_screen("Editor", extra="Something else entirely happened")
        for _ in range(4):
            model.predict_and_verify(from_id, "click:save", wrong, context)

        transition = model._transitions[(from_id, "click:save")]
        assert not transition.trusted, "repeated failure must retire the transition"

    def test_change_outside_every_sampled_region_is_not_detected(self, model, atlas, context):
        """
        A known and accepted limitation, recorded so it is not mistaken for a bug.

        Verification proves the *layout* is the one expected -- same controls,
        same places. It does not prove every character is identical, and it
        cannot: a single changed digit alters fewer pixels than a blinking
        caret, so no whole-screen check at any resolution separates them.
        Measured across grids from 64x36 to 320x180, a changed digit scored
        ~0.00000 while a caret scored 0.00017 to 0.00043 -- the wrong way round.

        The practical consequence is bounded: a confirmed prediction means the
        screen is safe to act on, not that volatile text such as a clock or a
        counter is current. `observe(force_full=True)` is the escape hatch.
        """
        before_screen = make_screen("Editor")
        expected = make_screen("Editor", extra="Save dialog is open")
        atlas.remember(before_screen, context, elements_for("Editor"))
        from_id = model.snapshot(before_screen, context)

        # Deliberately omit the panel from the elements, so nothing samples it.
        for _ in range(2):
            model.record(from_id, "click:save", expected, context, elements_for("Editor"))

        different_text = make_screen("Editor", extra="Something else entirely")
        prediction = model.predict_and_verify(from_id, "click:save", different_text, context)
        assert prediction.correct, (
            "documents current behaviour: an unsampled small text change is "
            "not detected"
        )

    def test_prediction_dies_with_its_target(self, model, atlas, context):
        """A transition pointing at an evicted screen must not be used."""
        before_screen = make_screen("Editor")
        after_screen = make_screen("Editor", extra="A dialog")
        atlas.remember(before_screen, context, elements_for("Editor"))
        from_id = model.snapshot(before_screen, context)
        for _ in range(2):
            model.record(from_id, "click:save", after_screen, context, elements_for("Editor"))

        target = atlas.find_by_id(model._transitions[(from_id, "click:save")].to_id)
        atlas.forget(target)

        assert model.predict_and_verify(from_id, "click:save", after_screen, context).attempted is False
        assert (from_id, "click:save") not in model._transitions

    def test_changed_outcome_restarts_counting(self, model, atlas, context):
        """An action that stops being deterministic must not stay trusted."""
        before_screen = make_screen("Editor")
        atlas.remember(before_screen, context, elements_for("Editor"))
        from_id = model.snapshot(before_screen, context)

        for _ in range(3):
            model.record(from_id, "click:go", make_screen("Editor", extra="Panel A"),
                         context, elements_for("Editor"))
        assert model._transitions[(from_id, "click:go")].trusted

        model.record(from_id, "click:go", make_screen("Editor", extra="A different Panel B"),
                     context, elements_for("Editor"))
        assert not model._transitions[(from_id, "click:go")].trusted

    def test_records_nothing_without_a_starting_screen(self, model, context):
        assert model.record(None, "click:save", make_screen(), context, elements_for()) is None

    def test_records_nothing_for_an_unverifiable_screen(self, model, context):
        """If the outcome cannot be remembered, the transition is useless."""
        from oswright.dirty import Region
        from oswright.screenmodel import Element

        unusable = [Element(text="x", region=Region(0, 0, 4, 4))]
        assert model.record("some-id", "click:save", make_screen(), context, unusable) is None


class TestTransitionPersistence:
    def test_roundtrip(self, atlas, tmp_path, context):
        path = tmp_path / "transitions.json"
        first = TransitionModel(atlas, path=path, autoload=False)
        atlas.remember(make_screen(), context, elements_for())
        from_id = first.snapshot(make_screen(), context)
        first.record(from_id, "click:save", make_screen("Editor"), context, elements_for("Editor"))
        assert first.save()

        second = TransitionModel(atlas, path=path)
        assert len(second) == 1

    def test_corrupt_file_degrades_to_empty(self, atlas, tmp_path):
        path = tmp_path / "transitions.json"
        path.write_text("not json at all", encoding="utf-8")
        assert len(TransitionModel(atlas, path=path)) == 0

    def test_eviction_respects_the_limit(self, atlas, tmp_path, context):
        model = TransitionModel(atlas, path=tmp_path / "t.json", max_transitions=3, autoload=False)
        atlas.remember(make_screen(), context, elements_for())
        from_id = model.snapshot(make_screen(), context)
        for i in range(8):
            model.record(from_id, f"click:item{i}",
                         make_screen("Editor", extra=f"Panel {i}"), context, elements_for("Editor"))
        assert len(model) <= 3


class TestPredictionSerialisation:
    def test_unattempted_prediction_is_quiet(self):
        payload = Prediction().to_dict()
        assert payload["predicted"] is False
        assert "surprise" not in payload

    def test_confirmed_prediction_reports_elements(self):
        payload = Prediction(attempted=True, correct=True, elements=42).to_dict()
        assert payload["confirmed"] is True
        assert payload["elements"] == 42

    def test_surprise_is_surfaced(self):
        payload = Prediction(attempted=True, correct=False, surprise="unexpected dialog").to_dict()
        assert payload["confirmed"] is False
        assert payload["surprise"] == "unexpected dialog"


class TestSettle:
    class Source:
        """A scripted compositor: each poll returns the next canned result."""

        def __init__(self, script):
            self.script = list(script)
            self.polls = 0

        def poll(self, timeout_ms=0):
            self.polls += 1
            if self.script:
                return self.script.pop(0)
            return []

    def test_no_source_is_not_settled(self):
        result = wait_until_settled(None)
        assert not result.settled
        assert "no compositor" in result.reason

    def test_quiet_screen_settles_immediately(self):
        result = wait_until_settled(self.Source([[], [], []]), quiet_ms=0)
        assert result.settled
        assert not result.changed

    def test_small_changes_are_ignored_as_noise(self):
        """A blinking caret covers ~32px and must not count as activity."""
        caret = [(0, 0, 8, 4)]  # 32px
        result = wait_until_settled(self.Source([caret] * 5), quiet_ms=0)
        assert result.settled
        assert not result.changed, "caret-sized noise was treated as a real change"

    def test_large_change_is_noticed(self):
        big = [(0, 0, 400, 400)]  # 160,000px
        source = self.Source([big, big, [], [], [], [], [], [], [], []])
        result = wait_until_settled(source, quiet_ms=20)
        assert result.settled
        assert result.changed

    def test_never_settling_times_out(self):
        big = [(0, 0, 800, 800)]
        source = self.Source([big] * 500)
        result = wait_until_settled(source, timeout_s=0.15)
        assert not result.settled
        assert "still changing" in result.reason

    def test_compositor_failure_is_not_settled(self):
        result = wait_until_settled(self.Source([None]))
        assert not result.settled
        assert "stopped answering" in result.reason

    def test_noise_threshold_is_above_measured_caret_size(self):
        """Idle caret/clock updates measured at a median of 32px, p90 64px."""
        assert QUIET_AREA_PX > 64 * 4

    def test_result_serialises(self):
        payload = SettleResult(True, 12.0, 70.0, changed=True).to_dict()
        assert payload["settled"] is True
        assert payload["settle_ms"] == 12.0
