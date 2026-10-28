"""Show (and optionally send) a shop's evening summary.

Builds the figures from the database, writes the message (Meta model with the
amount guard, or the template), and prints both. Nothing is sent or recorded
unless --send is given. Not run by pytest: it uses DATABASE_URL and
MODEL_API_KEY from .env.

    python scripts/demo_summary.py 237690001234
    python scripts/demo_summary.py 237690001234 --day 2026-10-27 --lang en
    python scripts/demo_summary.py 237690001234 --no-model
    python scripts/demo_summary.py 237690001234 --send

With Docker, run it inside the API container so it reaches the database:

    docker compose exec api python scripts/demo_summary.py 237690001234
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("wa_id", help="the merchant's WhatsApp number, e.g. 237690001234")
    parser.add_argument("--day", type=date.fromisoformat, help="local day, YYYY-MM-DD (default: today)")
    parser.add_argument("--lang", choices=("fr", "en", "pcm"), help="language (default: the shop's)")
    parser.add_argument("--no-model", action="store_true", help="use the template, don't call the model")
    parser.add_argument("--send", action="store_true", help="send it on WhatsApp and record it")
    args = parser.parse_args()

    os.chdir(ROOT)  # so Settings finds .env
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import select

    from app import clock
    from app.db import SessionLocal
    from app.logging_config import mask_phone
    from app.models import Language, Shop, SummaryChannel
    from app.services import daily
    from app.services.summary import build_daily_summary, local_now
    from app.services.summary_text import template, unknown_amounts, write_summary
    from app.whatsapp import client

    with SessionLocal() as session:
        shop = session.scalar(select(Shop).where(Shop.owner_phone == args.wa_id))
        if shop is None:
            print(f"No shop for {mask_phone(args.wa_id)}.")
            return 1
        day = args.day or local_now().date()
        language = Language(args.lang) if args.lang else shop.language
        figures = build_daily_summary(session, shop, day)

        print(f"Shop {shop.id} ({mask_phone(shop.owner_phone)}), {day.isoformat()}, language {language.value}")
        print(f"  sales {figures.sales_count} = {figures.sales_total} FCFA (credit {figures.credit_sales_total}), "
              f"purchases {figures.purchases_total}, repaid {figures.debt_repaid_total}, new debts {figures.new_debt_total}, "
              f"owed {figures.debts_outstanding_total}, yesterday {figures.yesterday_sales_total}")
        print(f"  top: {[p.name for p in figures.top_products]}  low: {[s.name for s in figures.low_stock]}  "
              f"out: {[s.name for s in figures.out_of_stock]}")
        print()

        text = template(figures, language) if args.no_model else write_summary(figures, language)
        source = "template" if args.no_model or text == template(figures, language) else "model"
        print(f"--- message ({source}, {len(text.splitlines())} lines) ---")
        print(text)
        print("---")
        if unknown_amounts(text, figures):
            print("warning: amounts not in the figures:", unknown_amounts(text, figures))

        if args.send:
            client.send_text(shop.owner_phone, text, phone_number_id=shop.phone_number_id)
            daily.record_summary(session, shop, day, SummaryChannel.MANUAL, clock.now())
            print(f"Sent to {mask_phone(shop.owner_phone)} and recorded as a manual summary.")
            print("WhatsApp refuses free-form messages if the merchant hasn't written in the last 24 h.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
