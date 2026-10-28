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


def _singular(word: str) -> str:
    if len(word) <= 3:  # riz, jus, gaz
        return word
    if word.endswith("eaux"):  # gateaux -> gateau
        return word[:-1]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
