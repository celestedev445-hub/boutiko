"""Evaluate voice-note transcription and extraction against the real Meta Model API.

For every .ogg / .wav file in a folder (default tests/data/voice/): convert to
WAV 16 kHz mono, transcribe with MODEL_ASR and with MODEL_ASR_FALLBACK, then
extract actions from the best transcript (MODEL_ASR's, unless it is empty).
If a .txt file with the same name exists, it holds the expected sentence and
a similarity score is shown.

Not run by pytest: it needs MODEL_API_KEY (read from .env or the
environment), ffmpeg on the PATH, and makes three API calls per file.

    python scripts/eval_voice.py
    python scripts/eval_voice.py path/to/recordings
"""

import argparse
import difflib
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FOLDER = ROOT / "tests" / "data" / "voice"
EXTENSIONS = {".ogg", ".oga", ".opus", ".wav"}


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("'", "").replace("’", "")  # j'ai -> jai
    text = re.sub(r"(?<=\d)[\s.](?=\d{3}\b)", "", text)  # 25 000 / 25.000 -> 25000
    return re.findall(r"\w+", text)


def similarity(expected: str, got: str) -> float:
    """Word-level similarity between 0 and 1, ignoring case, accents and punctuation."""
    return difflib.SequenceMatcher(None, _words(expected), _words(got)).ratio()


def _timed(function, *args):
    start = time.monotonic()
    try:
        return function(*args), None, time.monotonic() - start
    except Exception as exc:  # report and keep going with the next file
        return None, exc, time.monotonic() - start


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
    args = parser.parse_args()
    folder = args.folder.resolve()

    os.chdir(ROOT)  # so Settings finds .env
    sys.path.insert(0, str(ROOT))
    from app import meta_client
    from app.config import get_settings
    from app.services import audio, transcribe

    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in EXTENSIONS) if folder.is_dir() else []
    if not files:
        print(f"No .ogg or .wav file in {folder}. See tests/data/voice/README.md.")
        return 1

    settings = get_settings()
    models = {"asr": settings.model_asr, "fallback": settings.model_asr_fallback}
    print(f"ASR: {models['asr']}  ·  fallback: {models['fallback']}  ·  extraction: {settings.model_text}")
    print(f"{len(files)} file(s) in {folder}\n")

    scores: dict[str, list[float]] = {"asr": [], "fallback": []}
    errors = {"asr": 0, "fallback": 0}
    for index, path in enumerate(files, 1):
        expected_path = path.with_suffix(".txt")
        expected = expected_path.read_text(encoding="utf-8").strip() if expected_path.exists() else None

        wav, error, elapsed = _timed(audio.convert_to_wav, path.read_bytes())
        if error:
            print(f"[{index}/{len(files)}] {path.name}: conversion failed: {error}\n")
            continue
        print(f"[{index}/{len(files)}] {path.name}  ({wav.duration_seconds:.1f} s of audio, converted in {elapsed:.1f} s)")
        if expected:
            print(f"  expected      : {expected}")

        transcripts: dict[str, str] = {}
        for key, function in (("asr", transcribe.transcribe_with_asr), ("fallback", transcribe.transcribe_with_chat)):
            result, error, elapsed = _timed(function, wav.data)
            label = f"{key:<8} ({elapsed:4.1f} s)"
            if error:
                errors[key] += 1
                print(f"  {label}: ERROR {error}")
                continue
            transcripts[key] = result.text
            score = ""
            if expected:
                value = similarity(expected, result.text)
                scores[key].append(value)
                score = f"  [{value:.0%}]"
            session = f"  (session {result.session_id})" if result.session_id else ""
            print(f"  {label}: \"{result.text}\"{score}{session}")

        best = transcripts.get("asr") or transcripts.get("fallback")
        if best:
            extraction, error, elapsed = _timed(meta_client.extract_from_text, best)
            if error:
                print(f"  actions  ({elapsed:4.1f} s): ERROR {error}")
            else:
                described = "; ".join(_describe(a) for a in extraction.actions) or "none"
                print(f"  actions  ({elapsed:4.1f} s): [{extraction.language}] {described}")
        print()

    print("-" * 80)
    for key in ("asr", "fallback"):
        values = scores[key]
        average = f"average similarity {sum(values) / len(values):.0%} on {len(values)} file(s)" if values else "no .txt to compare"
        print(f"{key:<8} {models[key]:<28} {average}, {errors[key]} error(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
