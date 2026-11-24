"""Money handling. All amounts are integers in minor units (paisa for BDT). No floats, ever."""
from __future__ import annotations

import re
from decimal import Decimal

MAX_MINOR = 10**13  # 100 billion major units; far beyond any personal ledger, well within int64

_BENGALI_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_STRIP_TOKENS = ("৳", "BDT", "bdt", "Tk.", "TK.", "tk.", "Tk", "TK", "tk", "taka", "Taka")
_AMOUNT_RE = re.compile(r"^(\d+(\.\d*)?|\.\d+)$")


class MoneyError(ValueError):
    """Raised for unparseable or out-of-range amounts. The message is user-facing."""


def parse_amount(text, minor_unit: int = 2) -> int:
    """Parse user input like '1,500', '৳ 2,000.50' or '১২৫০' into integer minor units.

    Rejects negatives, exponent notation and more decimals than the currency allows
    (so '1.005' is an error, not a silently rounded value).
    """
    if text is None or isinstance(text, bool):
        raise MoneyError("Enter an amount.")
    if isinstance(text, int):
        text = str(text)
    elif isinstance(text, float):
        raise MoneyError("Amounts must be sent as text, not floating point.")
    s = str(text).strip().translate(_BENGALI_DIGITS)
    for token in _STRIP_TOKENS:
        s = s.replace(token, "")
    s = s.replace(",", "").replace(" ", "").replace(" ", "")
    if not s:
        raise MoneyError("Enter an amount.")
    if s.startswith("-"):
        raise MoneyError("Amounts can't be negative. Pick the right transaction type instead.")
    if not _AMOUNT_RE.match(s):
        raise MoneyError("Enter a valid amount, for example 1500 or 1,500.50.")
    value = Decimal(s)
    quantum = Decimal(1).scaleb(-minor_unit)
    if value != value.quantize(quantum):
        places = "no decimal places" if minor_unit == 0 else f"at most {minor_unit} decimal places"
        raise MoneyError(f"Use {places}.")
    minor = int(value.scaleb(minor_unit))
    if minor > MAX_MINOR:
        raise MoneyError("That amount is too large.")
    return minor


def _group(digits: str, grouping: str) -> str:
    if grouping == "indian" and len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        return ",".join(parts + [tail])
    return f"{int(digits):,}"


def format_amount(minor: int, minor_unit: int = 2, grouping: str = "western") -> str:
    """Format minor units as '1,234.50' (or '12,34,567.50' with Indian grouping)."""
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(int(minor)), 10**minor_unit)
    out = _group(str(whole), grouping)
    if minor_unit:
        out += "." + str(frac).zfill(minor_unit)
    return sign + out


def format_money(minor: int, currency) -> str:
    """Format with currency symbol: '৳1,234.50' / '−৳300.00'. `currency` is a row/dict."""
    body = format_amount(abs(int(minor)), currency["minor_unit"], currency["grouping"])
    sign = "\u2212" if minor < 0 else ""  # typographic minus, matching the transaction lists
    return f"{sign}{currency['symbol']}{body}"


def to_decimal_string(minor: int, minor_unit: int = 2) -> str:
    """Plain machine-readable decimal ('1234.50') for CSV/JSON; never a float."""
    return format_amount(minor, minor_unit, "none").replace(",", "")
