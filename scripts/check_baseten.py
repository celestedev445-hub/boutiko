"""Check the Baseten fallback: send one test message straight to Muse Spark Open.

Reads BASETEN_API_KEY (and BASETEN_BASE_URL, BASETEN_MODEL) from .env or the
environment, sends "vendu 3 savons à 500" with the extraction prompt and strict
JSON schema, and prints the validated extraction. Not run by pytest: it calls
the real API.

    python scripts/check_baseten.py
    python scripts/check_baseten.py "Marie a pris 2 kg de sucre à crédit, 1500"
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MESSAGE = "vendu 3 savons à 500"


def main() -> int:
    os.chdir(ROOT)  # so Settings finds .env
    sys.path.insert(0, str(ROOT))
    from pydantic import ValidationError

    from app import baseten_client, meta_client
    from app.config import get_settings
    from app.schemas import Extraction

    message = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MESSAGE
    settings = get_settings()
    if not settings.baseten_api_key:
        print("BASETEN_API_KEY is not set (.env or environment)")
        return 1
    print(f"{settings.baseten_model} at {settings.baseten_base_url}")
    print(f"Message: {message}")

    started = time.monotonic()
    try:
        content = baseten_client.complete(
            [
                {"role": "system", "content": meta_client.EXTRACTION_PROMPT},
                {"role": "user", "content": message},
            ],
            meta_client.extraction_response_format(),
        )
    except baseten_client.BasetenError as exc:
        print(f"Baseten failed: {exc}")
        return 1
    print(f"Answered in {time.monotonic() - started:.1f} s")

    try:
        extraction = Extraction.model_validate_json(content)
    except ValidationError as exc:
        print(f"Invalid JSON for the extraction schema:\n{content}\n\n{exc}")
        return 1
    print(extraction.model_dump_json(indent=2, exclude_none=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
