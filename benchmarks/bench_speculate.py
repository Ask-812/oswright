"""
What speculation saves, and what it does not guarantee.

Applications are deterministic, so after the first observation the outcome of an
action is already known. Confirming the expected screen is far cheaper than
reading it again.

This measures the saving, and also demonstrates the limit. Verification proves
that the *layout* is the one expected -- the same controls in the same places.
It does not prove that every character is identical, and it cannot: a single
changed digit alters a few pixels, which is less than a blinking caret does, so
no whole-screen check at any resolution distinguishes them.

The practical reading: a confirmed prediction means the screen is safe to act
on, not that volatile text like a clock or a counter is up to date. Use
`observe(force_full=True)` when the exact current text matters.

Run:  python benchmarks/bench_speculate.py
"""

import statistics
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

from oswright.atlas import ScreenContext, UIAtlas
from oswright.capture import ScreenCapture
from oswright.detect import OCREngine
from oswright.screenmodel import ScreenModel
from oswright.speculate import TransitionModel, action_key

ROUNDS = 8


def panel(title, body, tick=None, size=(900, 640)):
    """A synthetic application screen, optionally with a changing element."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.rectangle([30, 25, size[0] - 30, 80], outline="black", width=2)
    d.text((45, 45), title, fill="black")
    for i, line in enumerate(body):
        y = 120 + i * 54
        d.rectangle([40, y, 520, y + 40], outline="black", width=2)
        d.text((52, y + 13), line, fill="black")
    if tick is not None:
        # Stands in for a clock or a log line: content that never repeats.
        d.text((600, 560), f"live value {tick}", fill="black")
    return img


def elements_of(model, image):
    model.reset()
    model.observe(image=image)
    return model.elements


def run(label, frames_for_round, cap, ocr, tmp):
    """Drive the predict/observe loop and report what it cost."""
    atlas = UIAtlas(path=tmp / f"{label}.json", autoload=False)
    transitions = TransitionModel(atlas, path=tmp / f"{label}-t.json", autoload=False)
    model = ScreenModel(cap, ocr, atlas=atlas)
    context = ScreenContext(app=label, window_class="bench", width=900, height=640)

    observed_ms, predicted_ms, predictions = [], [], 0

    for round_index in range(ROUNDS):
        before_frame, after_frame = frames_for_round(round_index)

        before_id = transitions.snapshot(
            before_frame, context, elements=elements_of(model, before_frame)
        )
        key = action_key("click", target="Continue")

        started = time.perf_counter()
        prediction = transitions.predict_and_verify(before_id, key, after_frame, context)

        if prediction.correct:
            predicted_ms.append((time.perf_counter() - started) * 1000)
            predictions += 1
        else:
            elements = elements_of(model, after_frame)
            observed_ms.append((time.perf_counter() - started) * 1000)
            transitions.record(before_id, key, after_frame, context, elements)

    model.close()
    return {
        "label": label,
        "observed": observed_ms,
        "predicted": predicted_ms,
        "predictions": predictions,
        "summary": transitions.summary(),
    }


def report(result):
    print(f"\n--- {result['label']} ---")
    observed, predicted = result["observed"], result["predicted"]
    if observed:
        print(f"  observed   : {len(observed):>2} rounds, median {statistics.median(observed):7.1f} ms")
    if predicted:
        print(f"  predicted  : {len(predicted):>2} rounds, median {statistics.median(predicted):7.2f} ms")
    if observed and predicted:
        speedup = statistics.median(observed) / max(statistics.median(predicted), 0.001)
        print(f"  prediction is {speedup:,.0f}x cheaper than observing")
    print(f"  {result['summary']}")


def main():
    cap = ScreenCapture()
    ocr = OCREngine()
    tmp = Path(tempfile.mkdtemp(prefix="oswright_spec_"))

    body = ["Account settings", "Notification rules", "Privacy controls"]
    stable_before = panel("Preferences", body)
    stable_after = panel("Preferences", body + ["Changes saved successfully"])

    print("A deterministic screen: the same action always produces the same result.")
    report(run("deterministic", lambda i: (stable_before, stable_after), cap, ocr, tmp))

    print("\n\nThe same screen with a changing counter in the corner.")
    report(run(
        "volatile-text",
        lambda i: (panel("Console", body, tick=i), panel("Console", body + ["Done"], tick=i + 100)),
        cap, ocr, tmp,
    ))

    print(
        "\nBoth predict, and that is the point worth understanding. Verification\n"
        "confirms the layout, not every character: a changed digit moves fewer\n"
        "pixels than a blinking caret, so no whole-screen check can separate\n"
        "them at any resolution. A confirmed prediction means the screen is safe\n"
        "to act on; call observe(force_full=True) when exact text matters."
    )
    cap.close()


if __name__ == "__main__":
    main()
