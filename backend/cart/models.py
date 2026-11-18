from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from backend.currency import MAX_SQLITE_INTEGER, validate_currency_code


QUOTE_FRESHNESS_WINDOW = timedelta(minutes=5)
MAX_CART_QUANTITY = 9_007_199_254_740_991
MAX_PROVIDER_KEY_LENGTH = 64
MAX_PROVIDER_REFERENCE_LENGTH = 256
MAX_PRODUCT_ID_LENGTH = 128
MAX_PRODUCT_NAME_LENGTH = 200
MAX_PRODUCT_URL_LENGTH = 2048

OfferAvailability = Literal[
    "in_stock", "out_of_stock", "preorder", "backorder", "unknown"
]
OfferQuoteStatus = Literal[
    "verified_current",
    "stale_unverified",
    "unavailable",
    "price_change_pending",
]

_AVAILABILITY_VALUES = {
    "in_stock",
    "out_of_stock",
    "preorder",
    "backorder",
    "unknown",
}


class ExternalCartError(Exception):
    """Base error for external-offer cart operations."""


class InvalidCartInputError(ExternalCartError, ValueError):
    """Raised when a cart reference, quantity, or quote is invalid."""


class ExternalCartSchemaError(ExternalCartError):
    """Raised when the required external-cart schema is not installed."""


class ExternalCartStorageError(ExternalCartError):
    """Raised when a cart operation cannot be persisted or read."""


class UnknownOfferReferenceError(ExternalCartError):
    """Raised when an exact provider offer reference cannot be resolved."""


class OfferQuoteMismatchError(ExternalCartError):
    """Raised when supplied trusted quote evidence disagrees with stored offer data."""


class OfferNotEligibleError(ExternalCartError):
    """Raised when an offer cannot be added to the purchase-ready cart."""


class PriceConfirmationRequiredError(ExternalCartError):
    """Raised when a pending quote must be explicitly confirmed first."""


class CartItemNotFoundError(ExternalCartError):
    """Raised when an item is not in the specified owner's cart."""


def _validate_text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise InvalidCartInputError(
            f"{field} must contain 1 to {maximum} valid characters."
        )
    return value


def _parse_timestamp(value: object, field: str) -> tuple[str, datetime]:
    timestamp = _validate_text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidCartInputError(f"{field} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidCartInputError(f"{field} must include a timezone.")
    verified_at = parsed.astimezone(timezone.utc)
    if verified_at > datetime.now(timezone.utc):
        raise InvalidCartInputError(f"{field} cannot be in the future.")
    return timestamp, verified_at


def is_quote_timestamp_fresh(value: object) -> bool:
    _, verified_at = _parse_timestamp(value, "Quote verification timestamp")
    age = datetime.now(timezone.utc) - verified_at
    return timedelta(0) <= age <= QUOTE_FRESHNESS_WINDOW


def effective_quote_status(
    status: OfferQuoteStatus,
    verified_at: str | None,
) -> OfferQuoteStatus:
    is_fresh = (
        is_quote_timestamp_fresh(verified_at)
        if verified_at is not None
        else False
    )
    if status == "verified_current" and not is_fresh:
        return "stale_unverified"
    return status


def _validate_timestamp(value: object, field: str) -> str:
    timestamp, _ = _parse_timestamp(value, field)
    return timestamp


def validate_provider_url(value: object) -> str | None:
    if value is None:
        return None
    url = _validate_text(value, "Provider URL", MAX_PRODUCT_URL_LENGTH)
    if any(character.isspace() or character == "\\" for character in url):
        raise InvalidCartInputError("Provider URL must not contain whitespace.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
        parsed.port
    except ValueError as exc:
        raise InvalidCartInputError("Provider URL is invalid.") from exc
    if parsed.scheme != "https" or not hostname or has_credentials:
        raise InvalidCartInputError(
            "Provider URL must be an HTTPS URL without credentials."
        )
    return url


@dataclass(frozen=True)
class ResolvedOfferQuote:
    """Server-resolved offer data; never construct this from client quote fields."""

    offer_id: str
    provider_key: str
    provider_offer_key: str
    provider_product_id: str
    product_name: str
    price_minor: int | None
    currency: str | None
    product_url: str | None
    availability: OfferAvailability
    verified_at: str

    def __post_init__(self) -> None:
        _validate_text(self.offer_id, "Offer ID", MAX_PROVIDER_REFERENCE_LENGTH)
        _validate_text(self.provider_key, "Provider key", MAX_PROVIDER_KEY_LENGTH)
        _validate_text(
            self.provider_offer_key,
            "Provider offer key",
            MAX_PROVIDER_REFERENCE_LENGTH,
        )
        _validate_text(
            self.provider_product_id,
            "Provider product ID",
            MAX_PRODUCT_ID_LENGTH,
        )
        _validate_text(self.product_name, "Product name", MAX_PRODUCT_NAME_LENGTH)

        if (self.price_minor is None) != (self.currency is None):
            raise InvalidCartInputError(
                "Offer price and currency must both be known or both be unknown."
            )
        if self.price_minor is not None:
            if (
                not isinstance(self.price_minor, int)
                or isinstance(self.price_minor, bool)
                or self.price_minor < 0
                or self.price_minor > MAX_SQLITE_INTEGER
            ):
                raise InvalidCartInputError(
                    "Offer price must be a non-negative integer in minor units."
                )
            try:
                validate_currency_code(self.currency)
            except ValueError as exc:
                raise InvalidCartInputError(
                    "Offer currency must be an explicitly supported ISO 4217 code."
                ) from exc

        if (
            not isinstance(self.availability, str)
            or self.availability not in _AVAILABILITY_VALUES
        ):
            raise InvalidCartInputError("Offer availability is invalid.")
        validate_provider_url(self.product_url)
        _validate_timestamp(self.verified_at, "Quote verification timestamp")


@dataclass(frozen=True)
class ExternalCartItem:
    offer_id: str
    provider_key: str
    provider_offer_key: str
    provider_product_id: str
    product_name: str
    price_minor: int
    currency: str
    quantity: int
    product_url: str | None
    availability: OfferAvailability
    verified_at: str | None
    added_at: str
    quote_status: OfferQuoteStatus
    pending_price_minor: int | None = None
    pending_currency: str | None = None
    pending_verified_at: str | None = None
    pending_product_name: str | None = None
    pending_product_url: str | None = None
    pending_availability: OfferAvailability | None = None

    @property
    def snapshot_subtotal_minor(self) -> int:
        return self.price_minor * self.quantity


@dataclass(frozen=True)
class CurrencyTotal:
    currency: str
    snapshot_total_minor: int
    item_count: int
    all_quotes_current: bool


@dataclass(frozen=True)
class ExternalCart:
    items: tuple[ExternalCartItem, ...]
    currency_totals: tuple[CurrencyTotal, ...]
