"""Deterministic value normalizers for `service_agreement.v1`.

Each returns `(value, None)` or `(None, reason)` with reason `unparseable` or
`ambiguous`. Nothing is guessed: a numeric date that reads validly both
day-first and month-first is ambiguous; a notice period in months is not a
number of days; an amount or currency outside the supported forms is
unparseable. The supported forms are exactly those the contract needs and the
development corpus shows; anything else is refused rather than approximated.
"""

import re
from datetime import date
from decimal import Decimal

from api.extraction.contract import MAX_NOTICE_DAYS, MAX_TEXT_VALUE, Money, Reason

type Normalized[T] = tuple[T, None] | tuple[None, Reason]

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}
MAX_AMOUNT_INTEGER_DIGITS = 15  # as in contract.Money
# Active ISO 4217 codes accepted in "EUR 1,000.00" form (a deliberate, fixed list).
CURRENCY_CODES = frozenset(
    {
        "AUD",
        "CAD",
        "CHF",
        "CNY",
        "CZK",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "HUF",
        "INR",
        "JPY",
        "NOK",
        "NZD",
        "PLN",
        "SEK",
        "SGD",
        "USD",
        "ZAR",
    }
)
# Only symbols that name exactly one currency. "$" is shared by many and is not accepted.
CURRENCY_SYMBOLS = {"€": "EUR"}

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2}) ([A-Z][a-z]+) (\d{4})$")
_MONTH_DAY_YEAR = re.compile(r"^([A-Z][a-z]+) (\d{1,2}), (\d{4})$")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_MONEY = re.compile(
    r"^(?:(?P<code>[A-Z]{3}) ?|(?P<symbol>€))"
    r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,15}(?:\.\d{1,2})?)$"
)
_DAYS = re.compile(r"^(\d{1,4}) (?:calendar )?days?$")


def normalize_date(raw: str) -> Normalized[str]:
    text = _without_final_period(raw)
    if match := _ISO_DATE.match(text):
        return _date(int(match[1]), int(match[2]), int(match[3]))
    if match := _DAY_MONTH_YEAR.match(text):
        return _named(match[2], int(match[1]), int(match[3]))
    if match := _MONTH_DAY_YEAR.match(text):
        return _named(match[1], int(match[2]), int(match[3]))
    if match := _NUMERIC_DATE.match(text):
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        readings = {
            reading
            for reading in (_date(year, second, first)[0], _date(year, first, second)[0])
            if reading is not None
        }
        if len(readings) == 1:
            return readings.pop(), None
        return (None, "ambiguous") if readings else (None, "unparseable")
    return None, "unparseable"


def normalize_money(raw: str) -> Normalized[Money]:
    match = _MONEY.match(_without_final_period(raw))
    if not match:
        return None, "unparseable"
    currency = match["code"] or CURRENCY_SYMBOLS[match["symbol"]]
    if currency not in CURRENCY_CODES:
        return None, "unparseable"
    digits = match["amount"].replace(",", "")
    # The contract's amount allows at most 15 integer digits (Money): refuse larger
    # amounts here rather than letting validation fail (and echo the text) later.
    if len(digits.split(".")[0].lstrip("0")) > MAX_AMOUNT_INTEGER_DIGITS:
        return None, "unparseable"
    amount = Decimal(digits).quantize(Decimal("0.01"))
    return Money(amount=f"{amount:f}", currency=currency), None


def normalize_days(raw: str) -> Normalized[int]:
    match = _DAYS.match(_without_final_period(raw))
    if not match or int(match[1]) > MAX_NOTICE_DAYS:
        return None, "unparseable"
    return int(match[1]), None


def normalize_text(raw: str) -> Normalized[str]:
    """Names and jurisdictions as written, with whitespace collapsed."""
    text = " ".join(raw.split())
    if not text or len(text) > MAX_TEXT_VALUE or not any(c.isalpha() for c in text):
        return None, "unparseable"
    return text, None


def _without_final_period(raw: str) -> str:
    text = raw.strip()
    return text[:-1] if text.endswith(".") else text


def _named(month: str, day: int, year: int) -> Normalized[str]:
    number = MONTHS.get(month)
    return _date(year, number, day) if number else (None, "unparseable")


def _date(year: int, month: int, day: int) -> Normalized[str]:
    try:
        return date(year, month, day).isoformat(), None
    except ValueError:
        return None, "unparseable"
