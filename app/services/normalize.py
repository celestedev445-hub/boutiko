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


def _singular(word: str) -> str:
    if len(word) <= 3:  # riz, jus, gaz
        return word
    if word.endswith("eaux"):  # gateaux -> gateau
        return word[:-1]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
