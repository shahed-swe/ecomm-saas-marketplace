import re

_BD = re.compile(r"^\+8801[3-9]\d{8}$")


def normalize_bd_phone(raw: str) -> str | None:
    digits = re.sub(r"[\s\-()]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("01") and len(digits) == 11:
        digits = "+88" + digits
    elif digits.startswith("8801"):
        digits = "+" + digits
    return digits if _BD.match(digits) else None


def mask_phone(phone: str) -> str:
    return phone[:6] + "*****" + phone[-2:] if len(phone) > 8 else "***"
