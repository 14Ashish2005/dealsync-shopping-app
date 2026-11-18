from __future__ import annotations

from decimal import Decimal, InvalidOperation


SUPPORTED_CURRENCY_EXPONENTS = {
    "INR": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CAD": 2,
    "AUD": 2,
    "JPY": 0,
}
MAX_SQLITE_INTEGER = (1 << 63) - 1


def validate_currency_code(value: object) -> str:
    if not isinstance(value, str) or value not in SUPPORTED_CURRENCY_EXPONENTS:
        raise ValueError("Currency must be a supported ISO 4217 code.")
    return value


def validate_new_product_currency(value: object) -> str:
    """Validate currency for regular new product writes under Policy Option 1.

    Legacy seed rows retain NULL currency, but all new product writes must
    supply an explicit valid ISO 4217 currency code.
    """
    if value is None:
        raise ValueError("New product writes require an explicit valid ISO 4217 currency code.")
    return validate_currency_code(value)


def is_unverified_seed_currency(currency: object) -> bool:
    """Return True if the currency metadata is unverified (Option 1 policy)."""
    return currency is None


def currency_minor_unit_exponent(currency: str) -> int:
    return SUPPORTED_CURRENCY_EXPONENTS[validate_currency_code(currency)]


def parse_major_amount(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("Price must be a non-negative amount.")

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Price must be a non-negative amount.") from exc

    if not amount.is_finite() or amount < 0:
        raise ValueError("Price must be a non-negative amount.")
    return amount


def to_minor_units(value: object, currency: str) -> int:
    code = validate_currency_code(currency)
    amount = parse_major_amount(value)

    exponent = currency_minor_unit_exponent(code)
    minor_units = amount * (10 ** exponent)
    if minor_units != minor_units.to_integral_value():
        raise ValueError(f"Price cannot be represented exactly in {code} minor units.")

    result = int(minor_units)
    if result > MAX_SQLITE_INTEGER:
        raise ValueError("Price is outside the supported range.")
    return result


def format_major_amount(value: object, currency: object) -> str:
    if value is None or isinstance(value, bool):
        return "Price unavailable"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "Price unavailable"
    if not amount.is_finite():
        return "Price unavailable"

    if currency is None:
        return f"{amount:.2f} (currency unverified)"
    code = validate_currency_code(currency)
    exponent = currency_minor_unit_exponent(code)
    return f"{code} {amount:.{exponent}f}"
