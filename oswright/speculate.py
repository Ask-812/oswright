"""
Predicting what an action will do, instead of looking to find out.

Perception has been made cheap. This makes it unnecessary.

An agent's loop is: act, wait, look. But applications are deterministic --
clicking File in the same window produces the same menu every time -- so after
the first observation the outcome of an action is *known*. There is no need to
read the screen again to discover something already learned; it is enough to
confirm the expected thing happened.

That is speculative execution applied to perception. Predict the resulting
screen, verify the prediction cheaply, and pay for a real observation only when
the prediction was wrong. A correct prediction costs a few small image
comparisons; a wrong one costs those plus the observation that would have
happened anyway.

The two pieces this depends on already exist:

- The atlas (`atlas.py`) remembers what a screen looks like and can confirm one
  by comparing pixels, without OCR.
- The compositor (`settle.py`) says when the screen has stopped changing, for a
  fraction of a millisecond and without transferring pixels.

A failed prediction is not merely a cache miss. It means something unexpected
happened -- a dialog that does not usually appear, an error, a slow load -- and
that is worth telling the agent about rather than silently re-reading.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

# Coordinate clicks are bucketed before being used as part of a transition key.
# Two clicks a few pixels apart are almost always aimed at the same control, and
# treating them as different actions would mean never seeing the same
# transition twice.
CLICK_GRID = 16

DEFAULT_MAX_TRANSITIONS = 2000

# A transition has to be seen this many times before it is trusted enough to
# predict from. One observation could easily be a coincidence.
MIN_OBSERVATIONS = 2


def action_key(kind: str, target: Optional[str] = None, x=None, y=None) -> str:
    """
    Canonical name for an action, used to index transitions.

    Semantic targets are preferred over coordinates: clicking the element
    labelled "Save" is the same action wherever that button happens to sit,
    whereas a raw coordinate stops matching the moment the window moves.
    """
    kind = kind.strip().lower()
    if target:
        normalised = re.sub(r"\s+", " ", target.strip().lower())[:60]
        return f"{kind}:{normalised}"
    if x is not None and y is not None:
        return f"{kind}@{int(x) // CLICK_GRID},{int(y) // CLICK_GRID}"
    return kind


@dataclass
class Transition:
    """What was observed to follow an action taken on a particular screen."""

    from_id: str
    action: str
    to_id: str
    observations: int = 1
    correct: int = 0
    wrong: int = 0
    last_used_at: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str]:
        return (self.from_id, self.action)

    @property
    def confidence(self) -> float:
        """How often predicting from this transition has turned out right."""
        attempts = self.correct + self.wrong
        if attempts == 0:
            return 0.0
        return self.correct / attempts

    @property
    def trusted(self) -> bool:
        """
        Whether this transition is worth predicting from.

        A transition seen only once might be a coincidence. One that has been
        predicted and proved wrong more often than right is actively misleading
        and is retired rather than kept.
        """
        if self.observations < MIN_OBSERVATIONS:
            return False
        if self.correct + self.wrong >= 4 and self.confidence < 0.5:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "from_id": self.from_id,
            "action": self.action,
            "to_id": self.to_id,
            "observations": self.observations,
            "correct": self.correct,
            "wrong": self.wrong,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transition":
        return cls(
            from_id=data["from_id"],
            action=data["action"],
            to_id=data["to_id"],
            observations=data.get("observations", 1),
            correct=data.get("correct", 0),
            wrong=data.get("wrong", 0),
            last_used_at=data.get("last_used_at", time.time()),
        )


@dataclass
class Prediction:
    """The outcome of trying to predict rather than observe."""

    attempted: bool = False
    correct: bool = False
    action: str = ""
    elements: int = 0
    duration_ms: float = 0.0
    surprise: str = ""

    def to_dict(self) -> dict:
        payload = {
            "predicted": self.attempted,
            "confirmed": self.correct,
            "check_ms": round(self.duration_ms, 2),
        }
        if self.attempted and self.correct:
            payload["elements"] = self.elements
        if self.surprise:
            # A failed prediction is information, not just a cache miss: the
            # screen did something it does not normally do.
            payload["surprise"] = self.surprise
        return payload


class TransitionModel:
    """
    Remembers what actions do, so their effects can be predicted.

    Usage:
        model = TransitionModel(atlas)
        before = model.snapshot(frame, context)      # note where we are
        ...perform the action, wait for the screen to settle...
        prediction = model.predict_and_verify(before, key, frame, context)
        if not prediction.correct:
            observe_properly()
            model.record(before, key, frame, context, elements)
    """

    def __init__(
        self,
        atlas,
        path: Optional[Path] = None,
        max_transitions: int = DEFAULT_MAX_TRANSITIONS,
        autoload: bool = True,
    ):
        self.atlas = atlas
        self.path = Path(path) if path else atlas.path.with_name("transitions.json")
        self.max_transitions = max_transitions
        self._transitions: dict[tuple[str, str], Transition] = {}
        self.stats = {
            "predictions": 0,
            "confirmed": 0,
            "surprises": 0,
            "recorded": 0,
        }
        if autoload:
            self.load()

    def __len__(self) -> int:
        return len(self._transitions)

    # --- observing ---

    def snapshot(self, image: Image.Image, context, elements: Optional[list] = None) -> Optional[str]:
        """
        Identify the screen we are about to act on.

        If the screen is not yet known and its contents are available, it is
        remembered now. Without this the model could never start: a transition
        is only recorded when its starting screen has an identity, and nothing
        else would ever give it one.
        """
        entry = self.atlas.lookup(image, context)
        if entry is None and elements:
            entry = self.atlas.remember(image, context, elements)
        return entry.entry_id if entry else None

    def record(
        self, from_id: Optional[str], action: str,
        image: Image.Image, context, elements: list,
    ) -> Optional[Transition]:
        """Learn that `action` on `from_id` led to the screen now on display."""
        if not from_id or not action:
            return None

        entry = self.atlas.remember(image, context, elements)
        if entry is None:
            return None

        key = (from_id, action)
        existing = self._transitions.get(key)
        if existing is not None and existing.to_id == entry.entry_id:
            existing.observations += 1
            existing.last_used_at = time.time()
            self.stats["recorded"] += 1
            return existing

        # A different outcome than last time: the action is not deterministic
        # here, so start counting again rather than averaging two behaviours.
        transition = Transition(from_id=from_id, action=action, to_id=entry.entry_id)
        self._transitions[key] = transition
        self.stats["recorded"] += 1
        self._evict()
        return transition

    # --- predicting ---

    def predict_and_verify(
        self, from_id: Optional[str], action: str,
        image: Image.Image, context,
    ) -> Prediction:
        """
        Check whether the expected screen is the one now displayed.

        Returns a Prediction describing what happened. When `correct` is True
        the caller can use `entry.elements` from `last_entry` instead of
        observing; otherwise it must observe normally.
        """
        started = time.perf_counter()
        self.last_entry = None

        if not from_id or not action:
            return Prediction()

        transition = self._transitions.get((from_id, action))
        if transition is None or not transition.trusted:
            return Prediction()

        expected = self.atlas.find_by_id(transition.to_id)
        if expected is None:
            # The screen it pointed at has been evicted; the transition is dead.
            self._transitions.pop((from_id, action), None)
            return Prediction()

        self.stats["predictions"] += 1

        # Two independent checks, the same pair the atlas uses for recall.
        # The verifiers only cover the regions they sample, so a change outside
        # all of them would slip through on its own; the layout signature covers
        # the whole screen and catches structural differences the samples miss.
        from oswright.atlas import MATCH_TOLERANCE, layout_signature, signature_distance

        drift = signature_distance(layout_signature(image), expected.signature)
        confirmed = drift <= MATCH_TOLERANCE and self.atlas.verify(expected, image)
        elapsed = (time.perf_counter() - started) * 1000

        if confirmed:
            transition.correct += 1
            transition.last_used_at = time.time()
            self.stats["confirmed"] += 1
            self.last_entry = expected
            return Prediction(
                attempted=True, correct=True, action=action,
                elements=len(expected.elements), duration_ms=elapsed,
            )

        transition.wrong += 1
        self.stats["surprises"] += 1
        return Prediction(
            attempted=True, correct=False, action=action, duration_ms=elapsed,
            surprise=(
                f"{action!r} usually leads to a known screen, but the result "
                f"looks different this time"
            ),
        )

    # --- housekeeping ---

    def _evict(self):
        if len(self._transitions) <= self.max_transitions:
            return
        ranked = sorted(
            self._transitions.items(),
            key=lambda kv: (kv[1].correct, kv[1].observations, kv[1].last_used_at),
        )
        for key, _ in ranked[: len(self._transitions) - self.max_transitions]:
            del self._transitions[key]

    def load(self) -> bool:
        """Load previously learned transitions. Never raises."""
        try:
            if not self.path.exists():
                return False
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._transitions = {}
            for item in data.get("transitions", []):
                transition = Transition.from_dict(item)
                self._transitions[transition.key] = transition
            return True
        except Exception:
            logger.warning("Could not load transitions from %s; starting empty", self.path)
            self._transitions = {}
            return False

    def save(self) -> bool:
        """Persist learned transitions. Never raises."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "transitions": [t.to_dict() for t in self._transitions.values()],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except Exception:
            logger.warning("Could not save transitions to %s", self.path, exc_info=True)
            return False

    def summary(self) -> dict:
        attempted = self.stats["predictions"]
        return {
            "transitions": len(self._transitions),
            "trusted": sum(1 for t in self._transitions.values() if t.trusted),
            **self.stats,
            "accuracy": (
                round(self.stats["confirmed"] / attempted, 3) if attempted else 0.0
            ),
        }
