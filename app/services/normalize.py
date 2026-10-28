import re
import unicodedata


def normalize_name(name: str) -> str:
    """Lookup key for a product or customer name.

    Lowercase, accents and extra spaces removed, each word put in a simple
    singular, so "Savons ", "savon" and "SAVON" are the same product.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w\s'-]", " ", text)
    return " ".join(_singular(word) for word in text.split())


# Politeness words a merchant may add after a short command: "oui merci", "résumé stp".
_FILLERS = {"merci", "please", "pls", "plz", "stp", "svp", "thanks", "thank", "you", "boss", "sir", "ma", "oh"}


def normalize_command(text: str) -> str:
    """Short command as plain words: "Résumé, stp !" -> "resume", "D'accord" -> "d accord"."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("'", " ").replace("\u2019", " ")
    words = re.findall(r"[a-z0-9]+", text)
    while len(words) > 1 and words[-1] in _FILLERS:
        words.pop()
    return " ".join(words)


# "3 pièces de savon" just means 3 soaps: these units are not worth showing.
_GENERIC_UNITS = {"piece", "pc", "pcs", "unite", "unit", "u", "x", "article", "item", "pce"}


def is_generic_unit(unit: str | None) -> bool:
    return unit is None or normalize_name(unit) in _GENERIC_UNITS or not unit.strip()


_ARTICLES = {"le", "la", "les", "l", "de", "du", "des", "d", "the", "of", "un", "une", "a", "an"}
# Generic units, as they come out of _singular.
_GENERIC_UNIT_WORDS = {"piece", "pc", "pcs", "pce", "unite", "unit"}
# Words that name the shop or "things" in general, never a product: "how many soap remain for shop?".
FORBIDDEN_PRODUCT_KEYS = {"shop", "boutique", "magasin", "store", "stock", "produit", "article", "chose", "comptoir", "item"}


def product_key(name: str) -> str:
    """Lookup key for a product: case, accents, simple plurals, articles and generic units ignored.

    "Savons", "savon", "les savons" and "3 pièces de savon" all give "savon";
    "Savon Azur" gives "savon azur".
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("'", " ").replace("’", " ")
    words = [_singular(word) for word in re.findall(r"[a-z0-9]+", text)]
    words = [w for w in words if w not in _ARTICLES and w not in _GENERIC_UNIT_WORDS and not w.isdigit()]
    return " ".join(words)


def is_forbidden_product_key(key: str) -> bool:
    """Not a product name: the shop itself, "stock", "things", or too short."""
    return len(key) < 2 or key in FORBIDDEN_PRODUCT_KEYS


def _singular(word: str) -> str:
    if len(word) <= 3:  # riz, jus, gaz
        return word
    if word.endswith(("eaux", "oux", "eux")):  # gateaux -> gateau, choux -> chou, jeux -> jeu
        return word[:-1]
    # English -es, only where French has no such plural: boxes -> box, tomatoes -> tomato
    # (but caisses -> caisse, vaches -> vache below).
    if word.endswith(("xes", "oes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
