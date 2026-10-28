"""Evaluate photo understanding against the real Meta Model API.

For every image (.jpg, .jpeg, .png, .webp) in a folder (default
tests/data/vision/), call the vision model and show the image kind, the
actions read and the time taken. An optional .json file with the same name
gives the caption to send and what is expected:

    {
      "caption": "vendu 3 à 500",          optional, sent with the photo
      "language": "fr",                     optional, merchant's usual language
      "image_kind": "product",              optional, scored if present
      "actions": [{"action": "sale", "product": "Savon Azur", "quantity": 3, "price": 500}]
    }

Actions are scored like scripts/eval_extraction.py. Not run by pytest: it
needs MODEL_API_KEY (read from .env or the environment) and makes one API
call per image.

    python scripts/eval_vision.py
    python scripts/eval_vision.py path/to/photos -v
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FOLDER = ROOT / "tests" / "data" / "vision"
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _describe(action) -> str:
    parts = [action.action.value]
    if action.customer:
        parts.append(action.customer)
    if action.quantity is not None:
        parts.append(f"{action.quantity:g}" + (f" {action.unit}" if action.unit else ""))
    if action.product:
        parts.append(action.product)
    if action.price is not None:
        parts.append(f"@{action.price}")
    if action.total_price is not None:
        parts.append(f"total {action.total_price}")
    return " ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", nargs="?", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("-v", "--verbose", action="store_true", help="print the raw actions as JSON")
    args = parser.parse_args()
    folder = args.folder.resolve()

    os.chdir(ROOT)  # so Settings finds .env
    sys.path.insert(0, str(ROOT))
    from app.config import get_settings
    from app.models import Language
    from app.services import vision
    from scripts.eval_extraction import compare

    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in EXTENSIONS) if folder.is_dir() else []
    if not files:
        print(f"No image in {folder}. See tests/data/vision/README.md.")
        return 1

    print(f"Vision model: {get_settings().model_vision}  ·  {len(files)} image(s) in {folder}\n")
    scored = ok = kinds_scored = kinds_ok = 0
    for index, path in enumerate(files, 1):
        expected_path = path.with_suffix(".json")
        expected = json.loads(expected_path.read_text(encoding="utf-8")) if expected_path.exists() else {}
        data = path.read_bytes()
        mime_type = vision.sniff_mime_type(data)
        print(f"[{index}/{len(files)}] {path.name}" + (f"  caption: {expected['caption']!r}" if expected.get("caption") else ""))
        if mime_type is None:
            print("  not a JPEG, PNG or WebP file\n")
            continue
        if len(data) > vision.MAX_IMAGE_BYTES:
            print(f"  warning: {len(data) / 1024 / 1024:.1f} MB, Boutiko refuses photos above 5 MB")

        start = time.monotonic()
        try:
            reading = vision.analyze_image(
                data, mime_type, expected.get("caption"), Language(expected.get("language", "fr"))
            )
        except vision.VisionError as exc:
            print(f"  ERROR ({time.monotonic() - start:.1f} s): {exc}\n")
            continue
        elapsed = time.monotonic() - start

        kind = reading.image_kind.value
        kind_note = ""
        if "image_kind" in expected:
            kinds_scored += 1
            kinds_ok += kind == expected["image_kind"]
            kind_note = "  ok" if kind == expected["image_kind"] else f"  expected {expected['image_kind']}"
        print(f"  kind     : {kind}{kind_note}   ({elapsed:.1f} s, language {reading.language})")
        for action in reading.actions:
            print(f"  action   : {_describe(action)}")
        if not reading.actions:
            print("  action   : none")
        if reading.needs_clarification:
            print(f"  question : {reading.needs_clarification}")
        if args.verbose:
            print("  raw      : " + json.dumps([a.model_dump(mode='json') for a in reading.actions], ensure_ascii=False))

        if "actions" in expected:
            scored += 1
            diffs = compare(expected["actions"], [a.model_dump(mode="json") for a in reading.actions])
            ok += not diffs
            print("  result   : " + ("OK" if not diffs else "KO  " + "; ".join(diffs)))
        print()

    print("-" * 80)
    if scored:
        print(f"Actions   : {ok}/{scored} ({ok / scored:.0%})")
    if kinds_scored:
        print(f"Image kind: {kinds_ok}/{kinds_scored} ({kinds_ok / kinds_scored:.0%})")
    if not scored and not kinds_scored:
        print("No .json file: nothing to score.")
    return 0 if ok == scored and kinds_ok == kinds_scored else 1


if __name__ == "__main__":
    sys.exit(main())
