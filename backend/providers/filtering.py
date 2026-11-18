from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from backend.currency import (
    currency_minor_unit_exponent,
    parse_major_amount,
    validate_currency_code,
)
from backend.providers.base import NormalizedOffer, PriceInput


_CATEGORY_ALIASES = {
    "smart phone": "smartphones",
    "smart phones": "smartphones",
    "smartphone": "smartphones",
    "smartphones": "smartphones",
    "laptop": "laptops",
    "laptops": "laptops",
    "smart watch": "smartwatches",
    "smart watches": "smartwatches",
    "smartwatch": "smartwatches",
    "smartwatches": "smartwatches",
    "headphone": "headphones",
    "headphones": "headphones",
    "wireless headphone": "wireless headphones",
    "wireless headphones": "wireless headphones",
}


def _normalize_category(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    if not normalized:
        return None
    return _CATEGORY_ALIASES.get(normalized, normalized)


def remove_category_terms(query: str, category: str | None) -> str:
    if not query or category is None:
        return query
    normalized_category = _normalize_category(category)
    if normalized_category is None:
        return query

    aliases = {
        alias
        for alias, canonical in _CATEGORY_ALIASES.items()
        if canonical == normalized_category
    }
    aliases.add(normalized_category)
    for alias in sorted(aliases, key=len, reverse=True):
        phrase = r"\s+".join(re.escape(token) for token in alias.split())
        query = re.sub(
            rf"(?<!\w){phrase}(?!\w)",
            " ",
            query,
            flags=re.IGNORECASE,
        )
    return re.sub(r"\s+", " ", query).strip(" \t\r\n,.;:!?")


@dataclass(frozen=True)
class ProviderSearchFilters:
    min_price: Decimal | None
    max_price: Decimal | None
    currency: str | None
    category: str | None

    @property
    def has_price_filter(self) -> bool:
        return self.min_price is not None or self.max_price is not None

    def matches(
        self,
        offer: NormalizedOffer,
        product_category: str | None,
    ) -> bool:
        if self.category is not None:
            if _normalize_category(product_category) != self.category:
                return False

        if not self.has_price_filter:
            return True
        if (
            self.currency is None
            or offer.currency != self.currency
            or offer.price_minor is None
        ):
            return False

        exponent = currency_minor_unit_exponent(self.currency)
        price = Decimal(offer.price_minor) / (10**exponent)
        return not (
            (self.min_price is not None and price < self.min_price)
            or (self.max_price is not None and price > self.max_price)
        )


def build_provider_search_filters(
    *,
    min_price: PriceInput | None = None,
    max_price: PriceInput | None = None,
    currency: str | None = None,
    category: str | None = None,
) -> ProviderSearchFilters:
    price_filter_requested = min_price is not None or max_price is not None
    if price_filter_requested and currency is None:
        raise ValueError("A currency code is required when filtering by price.")
    if currency is not None:
        try:
            currency = validate_currency_code(currency)
        except ValueError as exc:
            raise ValueError(
                "Search currency must be a supported ISO 4217 code."
            ) from exc

    def parse_bound(
        value: PriceInput | None,
        name: str,
    ) -> Decimal | None:
        if value is None:
            return None
        try:
            return parse_major_amount(value)
        except ValueError as exc:
            raise ValueError(
                f"{name} must be a non-negative amount."
            ) from exc

    minimum = parse_bound(min_price, "Minimum price")
    maximum = parse_bound(max_price, "Maximum price")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("Minimum price cannot exceed maximum price.")

    if category is not None:
        if not isinstance(category, str) or not category.strip():
            raise ValueError("Category filter must be non-empty text.")
        normalized_category = _normalize_category(category)
    else:
        normalized_category = None

    return ProviderSearchFilters(
        min_price=minimum,
        max_price=maximum,
        currency=currency,
        category=normalized_category,
    )
