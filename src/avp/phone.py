"""Normalisation des numéros de téléphone (France par défaut) au format E.164."""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\d+")


class InvalidPhoneNumber(ValueError):
    pass


def normalize_phone(raw: str, default_country: str = "33") -> str:
    """Retourne le numéro au format E.164 (+33XXXXXXXXX).

    Accepte : "05 62 00 00 00", "0562000000", "+33 5 62 00 00 00", "0033562000000",
    "33562000000", "+33 (0)5 62 00 00 00".
    Lève InvalidPhoneNumber si le numéro est inexploitable.
    """
    if raw is None:
        raise InvalidPhoneNumber("numéro vide")
    s = raw.strip()
    if not s:
        raise InvalidPhoneNumber("numéro vide")
    plus = s.startswith("+")
    s = s.replace("(0)", "")
    digits = "".join(_DIGITS.findall(s))
    if not digits:
        raise InvalidPhoneNumber(f"aucun chiffre dans {raw!r}")

    if plus:
        e164 = "+" + digits
    elif digits.startswith("00"):
        e164 = "+" + digits[2:]
    elif digits.startswith("0") and len(digits) == 10:
        e164 = "+" + default_country + digits[1:]
    elif digits.startswith(default_country) and len(digits) == len(default_country) + 9:
        e164 = "+" + digits
    else:
        raise InvalidPhoneNumber(f"format non reconnu : {raw!r}")

    if e164.startswith("+33"):
        national = e164[3:]
        if len(national) != 9 or national[0] == "0":
            raise InvalidPhoneNumber(f"numéro français invalide : {raw!r}")
    elif not (8 <= len(e164) - 1 <= 15):
        raise InvalidPhoneNumber(f"longueur E.164 invalide : {raw!r}")
    return e164


def is_mobile_fr(e164: str) -> bool:
    return e164.startswith(("+336", "+337"))


def to_national_fr(e164: str) -> str:
    """+33562000000 → 05 62 00 00 00 (pour l'affichage et la lecture vocale)."""
    if not e164.startswith("+33") or len(e164) != 12:
        return e164
    n = "0" + e164[3:]
    return " ".join(n[i : i + 2] for i in range(0, 10, 2))
