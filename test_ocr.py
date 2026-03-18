"""Test OCR with downsampling fix."""
import sys
import traceback

def main():
    from oswright.capture import ScreenCapture
    from oswright.detect import OCREngine

    print(f"MAX_OCR_WIDTH: {OCREngine.MAX_OCR_WIDTH}")
    print(f"Has _preprocess_image: {hasattr(OCREngine, '_preprocess_image')}")

    print("Capturing screenshot...")
    cap = ScreenCapture()
    img = cap.screenshot()
    print(f"Screenshot size: {img.size}")

    print("Loading OCR...")
    ocr = OCREngine(["en"])

    # Test preprocessing
    processed, scale = ocr._preprocess_image(img)
    print(f"Processed size: {processed.size}, scale: {scale}")

    print("Finding text (with downsampling)...")
    matches = ocr.find_text(img, "oswright")
    print(f"Found {len(matches)} matches")

    print("Done!")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
