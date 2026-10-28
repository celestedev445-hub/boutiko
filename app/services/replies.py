"""Short WhatsApp confirmations, in the merchant's language (fr, en, pcm)."""

from decimal import Decimal

from app.models import Language
from app.schemas import Action
from app.services.ledger import ActionResult, LedgerResult, LedgerWarning

FR, EN, PCM = Language.FR, Language.EN, Language.PIDGIN

TEXTS: dict[str, dict[Language, str]] = {
    "sale": {
        FR: "✅ {item} {sold}, {total}. Reste : {stock}.",
        EN: "✅ {item} sold, {total}. Left: {stock}.",
        PCM: "✅ {item} don sell, {total}. Remain: {stock}.",
    },
    "sale_no_price": {
        FR: "✅ {item} {sold}. Reste : {stock}.",
        EN: "✅ {item} sold. Left: {stock}.",
        PCM: "✅ {item} don sell. Remain: {stock}.",
    },
    "credit_sale": {
        FR: "📝 {item} à crédit pour {customer}, {total}. Reste : {stock}.",
        EN: "📝 {item} on credit for {customer}, {total}. Left: {stock}.",
        PCM: "📝 {customer} don take {item} for credit, {total}. Remain: {stock}.",
    },
    "credit_sale_no_price": {
        FR: "📝 {item} à crédit pour {customer}. Reste : {stock}.",
        EN: "📝 {item} on credit for {customer}. Left: {stock}.",
        PCM: "📝 {customer} don take {item} for credit. Remain: {stock}.",
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
        FR: "📦 Je ne connais pas encore « {product} ». Note un achat pour l'ajouter au stock.",
        EN: "📦 I don't know \"{product}\" yet. Record a purchase to add it to your stock.",
        PCM: "📦 I never know \"{product}\" yet. Record wetin you buy make I add am.",
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


def _item(result: ActionResult, language: Language) -> str:
    """"3 sacs de riz", "3 bags of rice", "4 tin milk", "3 savons"."""
    quantity = format_quantity(result.quantity, language)
    product = result.product or ""
    if result.unit:
        unit = _plural(result.unit, result.quantity, language)
        joiner = {FR: " de ", EN: " of ", PCM: " "}[language]
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
        if result.action == Action.SALE:
            lines.append(_t("sale" if result.total is not None else "sale_no_price", language, **values))
        elif result.action == Action.CREDIT_SALE:
            if result.customer:
                key = "credit_sale" if result.total is not None else "credit_sale_no_price"
                lines.append(_t(key, language, **values))
            else:
                lines.append(_t("sale" if result.total is not None else "sale_no_price", language, **values))
        elif result.action == Action.PURCHASE:
            lines.append(_t("purchase", language, **values))
        elif result.action == Action.DEBT_PAYMENT:
            lines.append(_t("payment", language, **values))
        if result.customer_balance is not None and result.action in (Action.CREDIT_SALE, Action.DEBT_PAYMENT):
            lines.append(_balance_line(result, language))
    elif result.action == Action.STOCK_CHECK and result.stock is not None:
        unit = f" {_plural(result.unit, result.stock, language)}" if result.unit else ""
        lines.append(_t("stock_check", language, product=result.product.capitalize(), stock=stock + unit))

    for warning in result.warnings:
        if warning == LedgerWarning.NOT_UNDERSTOOD:
            continue  # handled once for the whole message
        lines.append(
            _t(warning, language, product=result.product or "", stock=stock, customer=result.customer or "")
        )
    return lines


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
