"""Write the evening summary: a Meta model drafts it from the figures, a guard checks the amounts.

If the model invents an amount (or can't be reached), a deterministic
template with the same figures and no advice is used instead.
"""

import json
import logging
import re

import openai

from app import meta_client
from app.config import get_settings
from app.models import Language
from app.services.replies import format_amount, format_quantity, quantity_with_unit
from app.services.summary import DayFigures

logger = logging.getLogger(__name__)

FR, EN, PCM = Language.FR, Language.EN, Language.PIDGIN
REASONING_EFFORT = "low"
MAX_LIST = 5  # products listed per line in the template
MAX_CHARS = 1500  # anything longer is not "short"

LANGUAGE_NAMES = {FR: "French", EN: "English", PCM: "Cameroonian Pidgin English"}
_WEEKDAYS = {
    FR: ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
    EN: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
_MONTHS = {
    FR: ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
         "novembre", "décembre"],
    EN: ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
         "November", "December"],
}

SUMMARY_PROMPT = """You write the evening summary of a small shop in Cameroon, sent to the
shopkeeper on WhatsApp. Write in {language}.

- 8 to 12 short lines, warm and encouraging, a few simple emojis, no markdown
  headings, no tables.
- Use ONLY the figures in the JSON. Write every amount exactly as given in the
  "*_text" fields (for example "25 000 FCFA"). Never calculate a new amount: no
  sums, differences, averages or estimates.
- Mention sales (and credit sales), purchases, debts repaid and new debts,
  what customers still owe, the best sellers, low or empty stock, and the
  comparison with yesterday, skipping what is zero or empty.
- End with exactly ONE concrete piece of advice drawn from the figures, for
  example restocking a product that is out of stock or reminding the customer
  who owes the most.
- Only the message text, nothing before or after it."""

TEMPLATES: dict[str, dict[Language, str]] = {
    "header": {FR: "🌙 Bilan du {day}", EN: "🌙 Summary for {day}", PCM: "🌙 How market go for {day}"},
    "sales": {
        FR: "🛒 Ventes : {count} pour {total}",
        EN: "🛒 Sales: {count} for {total}",
        PCM: "🛒 Sales: {count} for {total}",
    },
    "sales_credit": {FR: " (dont {count} à crédit : {total})", EN: " ({count} on credit: {total})", PCM: " ({count} for credit: {total})"},
    "sales_unpriced": {FR: " · {count} sans prix", EN: " · {count} without a price", PCM: " · {count} no get price"},
    "no_sales": {FR: "🛒 Aucune vente", EN: "🛒 No sales", PCM: "🛒 No sale"},
    "yesterday": {FR: "{arrow} Hier : {total}{percent}", EN: "{arrow} Yesterday: {total}{percent}", PCM: "{arrow} Yesterday: {total}{percent}"},
    "top": {FR: "🏆 Top : {items}", EN: "🏆 Best sellers: {items}", PCM: "🏆 Wetin sell pass: {items}"},
    "purchases": {FR: "📦 Achats : {total}", EN: "📦 Purchases: {total}", PCM: "📦 Wetin you buy: {total}"},
    "debts_today": {
        FR: "💵 Dettes encaissées : {repaid} · nouvelles dettes : {new}",
        EN: "💵 Debts repaid: {repaid} · new debts: {new}",
        PCM: "💵 Debt wey dem pay: {repaid} · new debt: {new}",
    },
    "outstanding": {
        FR: "📝 On te doit en tout : {total} ({count} client{s})",
        EN: "📝 Customers owe you: {total} ({count} customer{s})",
        PCM: "📝 People dey owe you: {total} ({count} customer{s})",
    },
    "low_stock": {FR: "⚠️ Stock bas : {items}", EN: "⚠️ Low stock: {items}", PCM: "⚠️ Stock don low: {items}"},
    "out_of_stock": {FR: "❌ Épuisé : {items}", EN: "❌ Out of stock: {items}", PCM: "❌ E don finish: {items}"},
    "closing": {FR: "Bonne soirée ! 🙏", EN: "Have a good evening! 🙏", PCM: "Good evening o! 🙏"},
    "empty": {
        FR: "🌙 Bilan du {day} : aucune opération enregistrée. Envoie-moi tes ventes au fil de la journée, je m'occupe du reste 💪",
        EN: "🌙 Summary for {day}: nothing recorded. Send me your sales during the day and I'll take care of the rest 💪",
        PCM: "🌙 How market go for {day}: nothing recorded. Send me your sales as the day dey go, I go handle the rest 💪",
    },
    "offer_today": {
        FR: "💡 Tape RÉSUMÉ pour voir le bilan du jour.",
        EN: "💡 Type SUMMARY to see today's summary.",
        PCM: "💡 Type SUMMARY make you see how market go today.",
    },
    "offer_yesterday": {
        FR: "💡 Tape RÉSUMÉ pour voir le bilan d'hier.",
        EN: "💡 Type SUMMARY to see yesterday's summary.",
        PCM: "💡 Type SUMMARY make you see how market go yesterday.",
    },
    "hour_set": {
        FR: "⏰ C'est noté : ton résumé arrivera chaque soir à {hour}h.",
        EN: "⏰ Done: your summary will arrive every day at {hour}:00.",
        PCM: "⏰ I don hear: your summary go come every day by {hour}:00.",
    },
}


def _t(key: str, language: Language, **values) -> str:
    return TEMPLATES[key][language].format(**values)


def day_label(figures: DayFigures, language: Language) -> str:
    """"mardi 27 octobre", "Tuesday 27 October"."""
    names = FR if language == FR else EN
    day = figures.day
    return f"{_WEEKDAYS[names][day.weekday()]} {day.day} {_MONTHS[names][day.month - 1]}"


def _percent(value: int | None) -> str:
    return "" if value is None else f" ({value:+d} %)"


def template(figures: DayFigures, language: Language) -> str:
    """Every figure, no advice: used when the model can't be trusted or reached."""
    lines = [_t("header", language, day=day_label(figures, language))]
    if figures.sales_count:
        line = _t("sales", language, count=figures.sales_count, total=format_amount(figures.sales_total))
        if figures.credit_sales_count:
            line += _t("sales_credit", language, count=figures.credit_sales_count, total=format_amount(figures.credit_sales_total))
        if figures.unpriced_sales_count:
            line += _t("sales_unpriced", language, count=figures.unpriced_sales_count)
        lines.append(line)
    else:
        lines.append(_t("no_sales", language))
    if figures.yesterday_sales_total or figures.sales_total:
        arrow = "📈" if figures.sales_change >= 0 else "📉"
        lines.append(_t("yesterday", language, arrow=arrow, total=format_amount(figures.yesterday_sales_total),
                        percent=_percent(figures.sales_change_percent)))
    if figures.top_products:
        items = ", ".join(
            f"{p.name} ({quantity_with_unit(p.quantity, p.unit, language)}"
            + (f" · {format_amount(p.amount)})" if p.amount else ")")
            for p in figures.top_products
        )
        lines.append(_t("top", language, items=items))
    if figures.purchases_count:
        lines.append(_t("purchases", language, total=format_amount(figures.purchases_total)))
    if figures.debt_repaid_total or figures.new_debt_total:
        lines.append(_t("debts_today", language, repaid=format_amount(figures.debt_repaid_total),
                        new=format_amount(figures.new_debt_total)))
    if figures.debts_outstanding_total:
        lines.append(_t("outstanding", language, total=format_amount(figures.debts_outstanding_total),
                        count=figures.debtors_count, s="s" if figures.debtors_count > 1 else ""))
    if figures.low_stock:
        items = ", ".join(f"{s.name} ({format_quantity(s.stock, language)})" for s in figures.low_stock[:MAX_LIST])
        lines.append(_t("low_stock", language, items=items))
    if figures.out_of_stock:
        lines.append(_t("out_of_stock", language, items=", ".join(s.name for s in figures.out_of_stock[:MAX_LIST])))
    lines.append(_t("closing", language))
    return "\n".join(lines)


def empty_day(figures: DayFigures, language: Language) -> str:
    return _t("empty", language, day=day_label(figures, language))


def offer(language: Language, yesterday: bool) -> str:
    return _t("offer_yesterday" if yesterday else "offer_today", language)


def hour_set(hour: int, language: Language) -> str:
    return _t("hour_set", language, hour=hour)


def figures_for_model(figures: DayFigures, language: Language) -> dict:
    """The figures as JSON for the model, every amount also pre-formatted."""
    def stock_list(items):
        return [{"product": s.name, "stock": format_quantity(s.stock, language)} for s in items[:MAX_LIST]]

    return {
        "day": day_label(figures, language),
        "sales": {
            "count": figures.sales_count,
            "total_text": format_amount(figures.sales_total),
            "credit_count": figures.credit_sales_count,
            "credit_total_text": format_amount(figures.credit_sales_total),
            "without_price_count": figures.unpriced_sales_count,
        },
        "yesterday": {
            "sales_total_text": format_amount(figures.yesterday_sales_total),
            "change_text": format_amount(abs(figures.sales_change)),
            "change_direction": "up" if figures.sales_change > 0 else "down" if figures.sales_change < 0 else "same",
            "change_percent": figures.sales_change_percent,
        },
        "purchases": {"count": figures.purchases_count, "total_text": format_amount(figures.purchases_total)},
        "debts": {
            "repaid_today_text": format_amount(figures.debt_repaid_total),
            "new_today_text": format_amount(figures.new_debt_total),
            "owed_to_shop_text": format_amount(figures.debts_outstanding_total),
            "customers_owing": figures.debtors_count,
            "biggest_debtor": (
                {"name": figures.top_debtor.name, "amount_text": format_amount(figures.top_debtor.amount)}
                if figures.top_debtor else None
            ),
        },
        "best_sellers": [
            {
                "product": p.name,
                "quantity": quantity_with_unit(p.quantity, p.unit, language),
                "amount_text": format_amount(p.amount),
            }
            for p in figures.top_products
        ],
        "low_stock": stock_list(figures.low_stock),
        "out_of_stock": [s.name for s in figures.out_of_stock[:MAX_LIST]],
    }


# An amount is a number followed by a currency ("25 000 FCFA", "25.000 F", "1500 francs"),
# or any number written with thousands separators ("25 000").
_SEPARATED = r"\d{1,3}(?:[   .,]\d{3})+"
_AMOUNT = re.compile(
    rf"(?<![\d.,])({_SEPARATED}|\d+)\s*(?:FCFA|F\s?CFA|XAF|francs?|frs?|F)\b|(?<![\d.,])({_SEPARATED})(?![\d])",
    re.IGNORECASE,
)
_THOUSANDS_SHORTHAND = re.compile(r"\d+\s?[kK]\b")  # "25k": not allowed, amounts must be written in full


def unknown_amounts(text: str, figures: DayFigures) -> list[str]:
    """Amounts in the text that are not in the figures (empty list: the text can be sent)."""
    allowed = figures.amounts()
    unknown = []
    for match in _AMOUNT.finditer(text):
        raw = match.group(1) or match.group(2)
        value = int(re.sub(r"\D", "", raw))
        if value not in allowed:
            unknown.append(match.group(0).strip())
    unknown += [m.group(0) for m in _THOUSANDS_SHORTHAND.finditer(text)]
    return unknown


def _write_with_model(figures: DayFigures, language: Language) -> str:
    response = meta_client.create_completion(
        model=get_settings().model_text,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT.format(language=LANGUAGE_NAMES[language])},
            {"role": "user", "content": json.dumps(figures_for_model(figures, language), ensure_ascii=False)},
        ],
        reasoning_effort=REASONING_EFFORT,
    )
    return (response.choices[0].message.content or "").strip()


def write_summary(figures: DayFigures, language: Language) -> str:
    """The message to send: the model's draft if every amount checks out, the template otherwise."""
    if figures.is_empty:
        return empty_day(figures, language)
    try:
        text = _write_with_model(figures, language)
    except (openai.OpenAIError, meta_client.MissingAPIKeyError) as exc:
        logger.warning("Summary for shop %s written from the template: model call failed (%s)", figures.shop_id, type(exc).__name__)
        return template(figures, language)
    if not text or len(text) > MAX_CHARS:
        logger.warning("Summary for shop %s written from the template: model text empty or too long", figures.shop_id)
        return template(figures, language)
    unknown = unknown_amounts(text, figures)
    if unknown:
        logger.warning(
            "Summary for shop %s written from the template: %d amount(s) not in the figures", figures.shop_id, len(unknown)
        )
        return template(figures, language)
    return text
