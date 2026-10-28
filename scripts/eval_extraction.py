"""Evaluate text extraction against the real Meta Model API.

Runs every phrase of tests/data/phrases.json through extract_from_text and
compares the result with the expected actions. Not run by pytest: it needs
MODEL_API_KEY (read from .env or the environment) and makes one API call per
phrase.

    python scripts/eval_extraction.py              # all 30 phrases
    python scripts/eval_extraction.py --lang pcm   # only Pidgin
    python scripts/eval_extraction.py --id 10 -v   # one phrase, show the model output

Scoring, per phrase (OK only if every action matches, in order):
- action, quantity, price, total_price must be equal (missing = null);
  with quantity 1, price and total_price are interchangeable
- product and customer: same normalized name, or one contained in the other
- unit is shown but not scored (sac / bag / bags are all fine)
Language detection is scored separately.
"""

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHRASES = ROOT / "tests" / "data" / "phrases.json"

FIELDS = ("action", "product", "quantity", "unit", "price", "total_price", "customer")


def _key(name: str | None) -> str | None:
    from app.services.normalize import normalize_name

    return normalize_name(name) if name else None


def _names_match(expected: str | None, got: str | None) -> bool:
    expected, got = _key(expected), _key(got)
    if expected is None or got is None:
        return expected == got
    return expected in got or got in expected


def _amount(action: dict) -> set:
    return {action.get("price"), action.get("total_price")} - {None}


def compare_action(expected: dict, got: dict) -> list[str]:
    """Differences between an expected action and the model's, as readable strings."""
    diffs = []
    if expected.get("action") != got.get("action"):
        diffs.append(f"action {got.get('action')} != {expected.get('action')}")
    for field in ("product", "customer"):
        if not _names_match(expected.get(field), got.get(field)):
            diffs.append(f"{field} {got.get(field)!r} != {expected.get(field)!r}")
    exp_qty, got_qty = expected.get("quantity"), got.get("quantity")
    if (None if exp_qty is None else Decimal(str(exp_qty))) != (None if got_qty is None else Decimal(str(got_qty))):
        diffs.append(f"quantity {got_qty} != {exp_qty}")
    if exp_qty == 1:
        if _amount(expected) != _amount(got):
            diffs.append(f"amount {_amount(got) or None} != {_amount(expected) or None}")
    else:
        for field in ("price", "total_price"):
            if expected.get(field) != got.get(field):
                diffs.append(f"{field} {got.get(field)} != {expected.get(field)}")
    return diffs


def compare(expected: list[dict], got: list[dict]) -> list[str]:
    if len(expected) != len(got):
        return [f"{len(got)} action(s) instead of {len(expected)}"]
    return [f"#{i + 1} {d}" for i, (e, g) in enumerate(zip(expected, got)) for d in compare_action(e, g)]


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lang", choices=("fr", "en", "pcm"), help="only phrases in this language")
    parser.add_argument("--id", type=int, action="append", help="only this phrase id (repeatable)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print the model output for each phrase")
    args = parser.parse_args()

    os.chdir(ROOT)  # so Settings finds .env
    sys.path.insert(0, str(ROOT))
    from app import meta_client
    from app.config import get_settings

    phrases = json.loads(PHRASES.read_text(encoding="utf-8"))
    if args.lang:
        phrases = [p for p in phrases if p["language"] == args.lang]
    if args.id:
        phrases = [p for p in phrases if p["id"] in args.id]

    print(f"Model: {get_settings().model_text}  ·  {len(phrases)} phrase(s)\n")
    print(f"{'#':>3}  {'lang':<4} {'res':<4} {'lang?':<6} {'time':>5}  {'phrase':<58} details")
    print("-" * 120)

    ok = lang_ok = 0
    for phrase in phrases:
        start = time.monotonic()
        try:
            extraction = meta_client.extract_from_text(phrase["text"])
        except meta_client.ExtractionError as exc:
            elapsed = time.monotonic() - start
            print(f"{phrase['id']:>3}  {phrase['language']:<4} {'KO':<4} {'-':<6} {elapsed:>4.1f}s  "
                  f"{_short(phrase['text'], 58):<58} API error: {_short(str(exc), 60)}")
            continue
        elapsed = time.monotonic() - start

        got = [a.model_dump(mode="json") for a in extraction.actions]
        diffs = compare(phrase["expected"], got)
        language_matches = extraction.language == phrase["language"]
        ok += not diffs
        lang_ok += language_matches
        print(f"{phrase['id']:>3}  {phrase['language']:<4} {'OK' if not diffs else 'KO':<4} "
              f"{'ok' if language_matches else str(extraction.language):<6} {elapsed:>4.1f}s  "
              f"{_short(phrase['text'], 58):<58} {'; '.join(diffs)}")
        if args.verbose:
            for action in got:
                print("        " + json.dumps({k: action[k] for k in FIELDS if action.get(k) is not None}, ensure_ascii=False))

    total = len(phrases)
    if total:
        print("-" * 120)
        print(f"Actions : {ok}/{total} ({ok / total:.0%})")
        print(f"Language: {lang_ok}/{total} ({lang_ok / total:.0%})")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
