"""Short WhatsApp confirmations, in the merchant's language (fr, en, pcm)."""

from decimal import Decimal

from app.models import Language
from app.schemas import Action, ExtractedAction, Extraction
from app.services.ledger import ActionResult, LedgerResult, LedgerWarning
from app.services import products
from app.services.products import ProductChoice
from app.services.normalize import is_generic_unit

FR, EN, PCM = Language.FR, Language.EN, Language.PIDGIN

TEXTS: dict[str, dict[Language, str]] = {
    "sale": {
        FR: "✅ {item} {sold}, {total}.",
        EN: "✅ {item} sold, {total}.",
        PCM: "✅ {item} don sell, {total}.",
    },
    "sale_no_price": {
        FR: "✅ {item} {sold}.",
        EN: "✅ {item} sold.",
        PCM: "✅ {item} don sell.",
    },
    "credit_sale": {
        FR: "📝 {item} à crédit pour {customer}, {total}.",
        EN: "📝 {item} on credit for {customer}, {total}.",
        PCM: "📝 {customer} don take {item} for credit, {total}.",
    },
    "credit_sale_no_price": {
        FR: "📝 {item} à crédit pour {customer}.",
        EN: "📝 {item} on credit for {customer}.",
        PCM: "📝 {customer} don take {item} for credit.",
    },
    "remaining": {FR: " Reste : {stock}.", EN: " Left: {stock}.", PCM: " Remain: {stock}."},
    "set_stock": {
        FR: "📦 {product} : {stock} en stock, c'est noté.",
        EN: "📦 {product}: {stock} in stock, noted.",
        PCM: "📦 {product}: {stock} dey for stock, I don write am.",
    },
    "choose_product": {
        FR: "🤔 Quel {query} : {options} ?",
        EN: "🤔 Which {query}: {options}?",
        PCM: "🤔 Which {query}: {options}?",
    },
    "or": {FR: "ou", EN: "or", PCM: "or"},
    "welcome": {
        FR: "👋 {greeting} ! Envoie-moi tes ventes, achats ou dettes, en texte, en vocal ou en photo, par exemple « vendu 3 savons à 250 ». Tape RÉSUMÉ pour le bilan du jour.",
        EN: "👋 {greeting}! Send me your sales, purchases or debts, by text, voice note or photo, for example \"sold 3 soaps at 250\". Type SUMMARY for today's summary.",
        PCM: "👋 {greeting}! Send me your sales, wetin you buy or who dey owe, for text, voice or photo, like \"I sell 3 soap 250 each\". Type SUMMARY make you see how market go today.",
    },
    "purchase": {
        FR: "📦 {item} {added} au stock. Stock : {stock}.",
        EN: "📦 {item} added to stock. Stock: {stock}.",
        PCM: "📦 {item} don enter stock. Stock: {stock}.",
    },
    "payment": {
        FR: "💵 {customer} a payé {total}.",
        EN: "💵 {customer} paid {total}.",
        PCM: "💵 {customer} don pay {total}.",
    },
    "balance_owes": {
        FR: "📝 {customer} doit maintenant {balance}.",
        EN: "📝 {customer} now owes {balance}.",
        PCM: "📝 {customer} dey owe {balance} now.",
    },
    "balance_clear": {
        FR: "📝 {customer} ne doit plus rien.",
        EN: "📝 {customer} owes nothing now.",
        PCM: "📝 {customer} no owe anything again.",
    },
    "balance_credit": {
        FR: "📝 {customer} a payé {balance} de trop : c'est toi qui lui dois.",
        EN: "📝 {customer} overpaid by {balance}: you owe them.",
        PCM: "📝 {customer} don pay pass by {balance}: na you dey owe am.",
    },
    "stock_check": {
        FR: "📦 {product} : {stock} en stock.",
        EN: "📦 {product}: {stock} in stock.",
        PCM: "📦 {product}: {stock} dey for stock.",
    },
    LedgerWarning.NEGATIVE_STOCK: {
        FR: "⚠️ Le stock de {product} est à {stock}. Pas grave : pense à noter tes achats pour le corriger.",
        EN: "⚠️ {product} stock is at {stock}. No problem, just record your purchases to correct it.",
        PCM: "⚠️ {product} stock don reach {stock}. No wahala, just record wetin you buy make e correct.",
    },
    LedgerWarning.MISSING_PRICE: {
        FR: "💰 Je n'ai pas le prix de {product} : c'est noté sans montant. Dis-moi le prix la prochaine fois.",
        EN: "💰 I don't have the price of {product}, so it's recorded without an amount. Tell me the price next time.",
        PCM: "💰 I no get price for {product}, I don write am without money. Tell me the price next time.",
    },
    LedgerWarning.MISSING_CUSTOMER: {
        FR: "⚠️ Pour quel client ? Renvoie le message avec son nom, par exemple « Marie a pris 2 sucres à crédit ».",
        EN: "⚠️ Which customer? Send it again with their name, e.g. \"Marie took 2 sugar on credit\".",
        PCM: "⚠️ Which customer? Send am again with the name, like \"Marie take 2 sugar for credit\".",
    },
    LedgerWarning.MISSING_QUANTITY: {
        FR: "⚠️ Combien de {product} ? Renvoie le message avec la quantité.",
        EN: "⚠️ How many {product}? Send it again with the quantity.",
        PCM: "⚠️ How many {product}? Send am again with the number.",
    },
    LedgerWarning.MISSING_PRODUCT: {
        FR: "⚠️ Quel produit ? Renvoie le message avec le nom du produit.",
        EN: "⚠️ Which product? Send it again with the product name.",
        PCM: "⚠️ Which product? Send am again with the product name.",
    },
    LedgerWarning.MISSING_AMOUNT: {
        FR: "⚠️ Combien {customer} a payé ? Renvoie le message avec le montant.",
        EN: "⚠️ How much did {customer} pay? Send it again with the amount.",
        PCM: "⚠️ How much {customer} pay? Send am again with the money.",
    },
    LedgerWarning.UNKNOWN_PRODUCT: {
        FR: "📦 Je ne connais pas encore « {product} ». Envoie un achat pour l'ajouter.",
        EN: "📦 I don't know \"{product}\" yet. Send a purchase to add it.",
        PCM: "📦 I never know \"{product}\" yet. Send wetin you buy make I add am.",
    },
    LedgerWarning.FORBIDDEN_PRODUCT: {
        FR: "🤔 « {product} » n'est pas un produit. De quel produit tu parles ?",
        EN: "🤔 \"{product}\" isn't a product. Which product do you mean?",
        PCM: "🤔 \"{product}\" no be product. Which product you mean?",
    },
    LedgerWarning.STOCK_UNKNOWN: {
        FR: "💡 Dis-moi combien tu as {of_product} en stock pour que je le suive.",
        EN: "💡 Tell me how many {product} you have in stock so I can follow it.",
        PCM: "💡 Tell me how many {product} you get for stock make I follow am.",
    },
    "not_understood": {
        FR: "🤔 Je n'ai pas compris. Écris par exemple : « vendu 3 savons à 250 » ou « Marie a pris 2 kg de sucre à crédit ».",
        EN: "🤔 I didn't understand. Try for example: \"sold 3 soaps at 250\" or \"Marie took 2 kg of sugar on credit\".",
        PCM: "🤔 I no understand. Write am like: \"I sell 3 soap 250 each\" or \"Marie take 2 kilo sugar for credit\".",
    },
    "partly_understood": {
        FR: "🤔 Je n'ai pas compris une partie du message.",
        EN: "🤔 I didn't understand part of the message.",
        PCM: "🤔 Part of the message no clear for me.",
    },
    "heard": {
        FR: "🎙️ J'ai compris : \"{text}\"",
        EN: "🎙️ I heard: \"{text}\"",
        PCM: "🎙️ I hear say: \"{text}\"",
    },
    "voice_not_heard": {
        FR: "🎙️ Je n'ai pas bien entendu. Tu peux répéter plus près du téléphone, ou écrire ton message ?",
        EN: "🎙️ I couldn't hear that clearly. Can you say it again closer to the phone, or type your message?",
        PCM: "🎙️ I no hear am well. Talk am again near the phone, or write the message?",
    },
    "voice_too_long": {
        FR: "🎙️ Ta note vocale est trop longue pour moi (5 minutes maximum). Envoie un message plus court ou écris-le.",
        EN: "🎙️ Your voice note is too long for me (5 minutes max). Send a shorter one or type your message.",
        PCM: "🎙️ Your voice note too long for me (5 minutes max). Send short one or write am.",
    },
    "voice_failed": {
        FR: "🎙️ Je n'ai pas pu écouter ta note vocale. Réessaie dans un instant, ou écris ton message.",
        EN: "🎙️ I couldn't listen to your voice note. Try again in a moment, or type your message.",
        PCM: "🎙️ I no fit listen your voice note. Try again small time, or write the message.",
    },
    "summary_header": {
        FR: "🧾 Voici ce que j'ai lu :",
        EN: "🧾 Here's what I read:",
        PCM: "🧾 See wetin I read:",
    },
    "summary_purchase": {FR: "• Achat : {item}{amount}", EN: "• Purchase: {item}{amount}", PCM: "• Buy: {item}{amount}"},
    "summary_sale": {FR: "• Vente : {item}{amount}", EN: "• Sale: {item}{amount}", PCM: "• Sell: {item}{amount}"},
    "summary_credit_sale": {
        FR: "• Crédit pour {customer} : {item}{amount}",
        EN: "• Credit for {customer}: {item}{amount}",
        PCM: "• Credit for {customer}: {item}{amount}",
    },
    "summary_debt_payment": {
        FR: "• Remboursement de {customer}{amount}",
        EN: "• Payment from {customer}{amount}",
        PCM: "• {customer} pay{amount}",
    },
    "summary_stock_check": {FR: "• Stock de {product}", EN: "• Stock of {product}", PCM: "• Stock for {product}"},
    "summary_set_stock": {
        FR: "• En stock : {product}, {count}",
        EN: "• In stock: {product}, {count}",
        PCM: "• For stock: {product}, {count}",
    },
    "price_unknown": {FR: "prix ?", EN: "price?", PCM: "price?"},
    "summary_total": {FR: "Total : {total}", EN: "Total: {total}", PCM: "Total: {total}"},
    "summary_total_partial": {
        FR: "Total : {total} (sans les lignes sans prix)",
        EN: "Total: {total} (lines without a price not included)",
        PCM: "Total: {total} (the ones wey no get price no dey inside)",
    },
    "summary_question": {
        FR: "Je l'enregistre ? Réponds OUI ou NON",
        EN: "Shall I save it? Reply YES or NO",
        PCM: "Make I save am? Answer YES or NO",
    },
    "cancelled": {
        FR: "❌ Annulé, je n'ai rien enregistré.",
        EN: "❌ Cancelled, nothing was saved.",
        PCM: "❌ I don cancel am, I no save anything.",
    },
    "already_saved": {
        FR: "✅ C'est déjà enregistré.",
        EN: "✅ That's already saved.",
        PCM: "✅ I don already save am.",
    },
    "photo_nothing": {
        FR: "🖼️ Je n'ai rien trouvé à enregistrer sur cette photo. Envoie une facture, un ticket, ou une photo de produit avec une légende comme « vendu 3 à 500 ».",
        EN: "🖼️ I found nothing to record on this photo. Send an invoice, a receipt, or a product photo with a caption like \"sold 3 at 500\".",
        PCM: "🖼️ I no see anything to record for this photo. Send invoice, receipt, or product photo with caption like \"I sell 3, 500 each\".",
    },
    "photo_unsupported": {
        FR: "🖼️ Je lis seulement les photos JPEG, PNG ou WebP. Envoie une photo de la facture ou du produit.",
        EN: "🖼️ I can only read JPEG, PNG or WebP photos. Send a photo of the invoice or the product.",
        PCM: "🖼️ I fit read only JPEG, PNG or WebP photo. Send photo of the invoice or the product.",
    },
    "photo_too_large": {
        FR: "🖼️ Cette photo est trop lourde pour moi (5 Mo maximum). Envoie-la en qualité normale.",
        EN: "🖼️ This photo is too big for me (5 MB max). Send it in normal quality.",
        PCM: "🖼️ This photo too big for me (5 MB max). Send am for normal quality.",
    },
    "photo_failed": {
        FR: "🖼️ Je n'ai pas pu lire ta photo. Réessaie dans un instant, ou écris ton message.",
        EN: "🖼️ I couldn't read your photo. Try again in a moment, or type your message.",
        PCM: "🖼️ I no fit read your photo. Try again small time, or write the message.",
    },
    "apology": {
        FR: "Je n'ai pas pu lire ton message, réessaie dans un instant.",
        EN: "I couldn't read your message, please try again in a moment.",
        PCM: "I no fit read your message, try again small time.",
    },
}


def format_amount(amount: int) -> str:
    """25000 -> "25 000 FCFA"."""
    sign = "-" if amount < 0 else ""
    return f"{sign}{abs(amount):,}".replace(",", " ") + " FCFA"


def format_quantity(quantity: Decimal, language: Language) -> str:
    """3.000 -> "3", 1.5 -> "1,5" in French."""
    text = format(quantity.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",") if language == FR else text


# Abbreviations never take an "s": 3 kg, 2 l.
_INVARIABLE_UNITS = {"kg", "g", "l", "cl", "ml", "m", "cm"}


def _is_plural(quantity: Decimal | None, language: Language) -> bool:
    if quantity is None:
        return False
    # French: plural from 2 (1,5 kg de sucre); English: anything but 1 (1.5 bags).
    return abs(quantity) >= 2 if language == FR else abs(quantity) != 1


def _plural(word: str, quantity: Decimal | None, language: Language) -> str:
    if language == PCM or not _is_plural(quantity, language):
        return word
    if word.lower() in _INVARIABLE_UNITS or word[-1:] in ("s", "x"):
        return word
    return word + "s"


def quantity_with_unit(quantity: Decimal, unit: str | None, language: Language) -> str:
    """"12", "3 sacs", "1,5 kg"."""
    text = format_quantity(quantity, language)
    return text if is_generic_unit(unit) else f"{text} {_plural(unit, quantity, language)}"


def _item(result: ActionResult, language: Language) -> str:
    """"3 sacs de riz", "3 bags of rice", "4 tin milk", "3 savons"."""
    quantity = format_quantity(result.quantity, language)
    product = result.product or ""
    if not is_generic_unit(result.unit):
        unit = _plural(result.unit, result.quantity, language)
        joiner = {FR: " de ", EN: " of ", PCM: " "}[language]
        if language == FR and product[:1].lower() in "aeiouhâéèêîôœ" and product:
            joiner = " d'"  # 3 bidons d'huile
        return f"{quantity} {unit}{joiner}{product}"
    first, _, rest = product.partition(" ")
    return f"{quantity} {_plural(first, result.quantity, language)}{' ' + rest if rest else ''}"


def _t(key, language: Language, **values) -> str:
    return TEXTS[key][language].format(**values)


def _balance_line(result: ActionResult, language: Language) -> str:
    balance = result.customer_balance
    if balance > 0:
        return _t("balance_owes", language, customer=result.customer, balance=format_amount(balance))
    if balance == 0:
        return _t("balance_clear", language, customer=result.customer)
    return _t("balance_credit", language, customer=result.customer, balance=format_amount(-balance))


def _action_lines(result: ActionResult, language: Language) -> list[str]:
    lines: list[str] = []
    stock = format_quantity(result.stock, language) if result.stock is not None else None
    if result.recorded:
        values = {
            "item": _item(result, language) if result.quantity is not None else "",
            "total": format_amount(result.total) if result.total is not None else "",
            "stock": stock,
            "customer": result.customer,
            "sold": "vendus" if _is_plural(result.quantity, FR) else "vendu",
            "added": "ajoutés" if _is_plural(result.quantity, FR) else "ajouté",
        }
        remaining = _t("remaining", language, stock=stock) if stock is not None else ""
        if result.action == Action.SALE or (result.action == Action.CREDIT_SALE and not result.customer):
            lines.append(_t("sale" if result.total is not None else "sale_no_price", language, **values) + remaining)
        elif result.action == Action.CREDIT_SALE:
            key = "credit_sale" if result.total is not None else "credit_sale_no_price"
            lines.append(_t(key, language, **values) + remaining)
        elif result.action == Action.PURCHASE:
            lines.append(_t("purchase", language, **values))
        elif result.action == Action.SET_STOCK:
            lines.append(_t("set_stock", language, product=_capitalized(result.product), stock=_stock_text(result, language)))
        elif result.action == Action.DEBT_PAYMENT:
            lines.append(_t("payment", language, **values))
        if result.customer_balance is not None and result.action in (Action.CREDIT_SALE, Action.DEBT_PAYMENT):
            lines.append(_balance_line(result, language))
    elif result.action == Action.STOCK_CHECK and result.stock is not None:
        lines.append(_t("stock_check", language, product=_capitalized(result.product), stock=_stock_text(result, language)))

    for warning in result.warnings:
        if warning == LedgerWarning.NOT_UNDERSTOOD:
            continue  # handled once for the whole message
        lines.append(
            _t(
                warning, language, product=result.product or "", stock=stock, customer=result.customer or "",
                of_product=_of(result.product or ""),
            )
        )
    return lines


def _capitalized(name: str) -> str:
    """First letter only: "savon Azur" -> "Savon Azur" (str.capitalize would give "Savon azur")."""
    return name[:1].upper() + name[1:]


def _stock_text(result: ActionResult, language: Language) -> str:
    stock = format_quantity(result.stock, language)
    return stock if is_generic_unit(result.unit) else f"{stock} {_plural(result.unit, result.stock, language)}"


def _of(product: str) -> str:
    """French "de savon" / "d'huile"."""
    return f"d'{product}" if product[:1].lower() in "aeiouhâéèêîôœ" and product else f"de {product}"


def confirmation(result: LedgerResult) -> str:
    language = result.language
    understood = [a for a in result.actions if a.action != Action.UNKNOWN]
    if not understood:
        return _t("not_understood", language)
    lines = [line for action in understood for line in _action_lines(action, language)]
    if len(understood) < len(result.actions):
        lines.append(_t("partly_understood", language))
    return "\n".join(lines)


def apology(language: Language = FR) -> str:
    return _t("apology", language)


MAX_HEARD_CHARS = 300


def heard(transcript: str, language: Language) -> str:
    """First line of a voice-note reply: what Boutiko understood."""
    text = " ".join(transcript.split())
    if len(text) > MAX_HEARD_CHARS:
        text = text[: MAX_HEARD_CHARS - 1] + "…"
    return _t("heard", language, text=text)


def voice_not_heard(language: Language) -> str:
    return _t("voice_not_heard", language)


def voice_too_long(language: Language) -> str:
    return _t("voice_too_long", language)


def voice_failed(language: Language) -> str:
    return _t("voice_failed", language)


def _line_amount(action: ExtractedAction) -> int | None:
    """What the line will cost or bring, as the ledger will record it."""
    if action.total_price is not None:
        return action.total_price
    if action.price is not None and action.quantity is not None:
        return int((action.price * action.quantity).to_integral_value())
    if action.price is not None:
        return action.price
    return None


def _summary_line(action: ExtractedAction, language: Language) -> str | None:
    if action.action == Action.UNKNOWN:
        return None
    if action.action == Action.STOCK_CHECK:
        return _t("summary_stock_check", language, product=action.product or "?")
    if action.action == Action.SET_STOCK:
        count = quantity_with_unit(action.quantity, action.unit, language) if action.quantity is not None else "?"
        return _t("summary_set_stock", language, product=action.product or "?", count=count)
    amount = _line_amount(action)
    amount_text = format_amount(amount) if amount is not None else _t("price_unknown", language)
    if action.action == Action.DEBT_PAYMENT:
        return _t("summary_debt_payment", language, customer=action.customer or "?", amount=f" — {amount_text}")
    if action.quantity is not None:
        item = _item(ActionResult(action=action.action, product=action.product or "?", unit=action.unit, quantity=action.quantity), language)
    else:
        item = action.product or "?"
    key = f"summary_{action.action.value}"
    return _t(key, language, item=item, amount=f" — {amount_text}", customer=action.customer or "?")


def pending_summary(extraction: Extraction, language: Language, clarification: str = "") -> str:
    """Recap of what was read on a photo, then the YES / NO question."""
    lines = [_t("summary_header", language)]
    lines += [line for a in extraction.actions if (line := _summary_line(a, language))]
    goods = [a for a in extraction.actions if a.action in (Action.SALE, Action.CREDIT_SALE, Action.PURCHASE)]
    amounts = [amount for a in goods if (amount := _line_amount(a)) is not None]
    if amounts:
        key = "summary_total" if len(amounts) == len(goods) else "summary_total_partial"
        lines.append(_t(key, language, total=format_amount(sum(amounts))))
    if clarification:
        lines.append(f"⚠️ {clarification.strip()}")
    lines.append(_t("summary_question", language))
    return "\n".join(lines)


def clarification(question: str) -> str:
    return f"❓ {question.strip()}"


def cancelled(language: Language) -> str:
    return _t("cancelled", language)


def already_saved(language: Language) -> str:
    return _t("already_saved", language)


def photo_nothing(language: Language) -> str:
    return _t("photo_nothing", language)


def photo_unsupported(language: Language) -> str:
    return _t("photo_unsupported", language)


def photo_too_large(language: Language) -> str:
    return _t("photo_too_large", language)


def photo_failed(language: Language) -> str:
    return _t("photo_failed", language)


def product_choice(choice: ProductChoice, language: Language) -> str:
    """"🤔 Quel savon : Azur ou Palmida ?"."""
    labels = products.option_labels(choice.query, choice.options)
    options = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f" {_t('or', language)} {labels[-1]}"
    return _t("choose_product", language, query=products.query_label(choice.query, choice.options), options=options)


def welcome(language: Language, greeting: str) -> str:
    return _t("welcome", language, greeting=greeting)
