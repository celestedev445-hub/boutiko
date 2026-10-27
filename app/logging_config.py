import logging
import re

# hub.verify_token arrives in the GET query string, which uvicorn's access log prints as-is.
_SECRET_QUERY = re.compile(r"(hub\.verify_token=)[^&\s]*")


def redact(text: str) -> str:
    return _SECRET_QUERY.sub(r"\1***", text)


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def mask_phone(phone: str) -> str:
    """Keep only the last 4 digits: 237690001234 -> ********1234."""
    return "*" * max(len(phone) - 4, 0) + phone[-4:]


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(RedactSecretsFilter())
    # httpx logs every request URL at INFO; our own logs already cover Graph calls.
    logging.getLogger("httpx").setLevel(logging.WARNING)
