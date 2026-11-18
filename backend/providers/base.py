from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Literal

from backend.currency import validate_currency_code
from backend.product_attributes import ProductAttribute


PriceInput = Decimal | float | int | str


class ProviderError(RuntimeError):
    def __init__(self, provider_key: str, message: str) -> None:
        self.provider_key = provider_key
        super().__init__(f"{provider_key}: {message}")


class ProviderUnavailableError(ProviderError):
    pass


class ProviderOfferNotFoundError(ProviderError):
    pass


class ProviderResolutionTimeoutError(ProviderError):
    pass


class ProviderCapacityError(ProviderError):
    pass


class InvalidProductDataError(ProviderError):
    pass


@dataclass(frozen=True)
class NormalizedOffer:
    offer_id: str
    provider_key: str
    provider_offer_key: str
    provider_product_id: str
    canonical_product_id: str | None
    product_name: str
    brand: str | None
    model: str | None
    price_minor: int | None
    currency: str | None
    product_url: str | None
    image_url: str | None
    availability: Literal[
        "in_stock", "out_of_stock", "preorder", "backorder", "unknown"
    ]
    last_checked_at: str
    expires_at: str | None
    created_at: str
    updated_at: str
    product_attributes: tuple[ProductAttribute, ...] | None = None

    def __post_init__(self) -> None:
        if (self.price_minor is None) != (self.currency is None):
            raise ValueError("Offer price and currency must both be known or unknown.")
        if self.product_attributes is not None and not all(
            isinstance(attribute, ProductAttribute)
            for attribute in self.product_attributes
        ):
            raise ValueError("Product attributes must use the structured attribute model.")
        if self.price_minor is not None:
            if (
                not isinstance(self.price_minor, int)
                or isinstance(self.price_minor, bool)
                or self.price_minor < 0
            ):
                raise ValueError("Offer price must be a non-negative integer.")
            validate_currency_code(self.currency)

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        if self.product_attributes is not None:
            data["product_attributes"] = [
                attribute.as_dict()
                for attribute in self.product_attributes
            ]
        return data


@dataclass(frozen=True)
class ResolvedProviderProduct:
    offer: NormalizedOffer
    category: str | None
    description: str | None
    is_organic: bool | None
    average_rating: float | None
    review_count: int | None
    product_attributes: tuple[ProductAttribute, ...] | None = None

    def __post_init__(self) -> None:
        if self.review_count == 0 and self.average_rating is not None:
            raise ValueError("A product with no reviews cannot have a rating.")
        if self.product_attributes is not None and not all(
            isinstance(attribute, ProductAttribute)
            for attribute in self.product_attributes
        ):
            raise ValueError("Product attributes must use the structured attribute model.")


class ProviderAdapter(ABC):
    @property
    def supports_external_cart(self) -> bool:
        """Providers must opt in explicitly; exact quote resolution remains required."""
        return False

    @property
    def search_filter_capabilities(self) -> frozenset[str]:
        """Filters are opt-in so adapters are never assumed to enforce them."""
        return frozenset()

    @property
    @abstractmethod
    def provider_key(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def display_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        query: str = "",
        *,
        max_price: PriceInput | None = None,
        min_price: PriceInput | None = None,
        currency: str | None = None,
        is_organic: bool | None = None,
        category: str | None = None,
    ) -> list[NormalizedOffer]:
        raise NotImplementedError

    @abstractmethod
    def normalize_offer(
        self,
        product: Mapping[str, object],
    ) -> NormalizedOffer:
        raise NotImplementedError

    def resolve_products(
        self,
        provider_product_ids: Sequence[str],
    ) -> tuple[ResolvedProviderProduct, ...]:
        if not provider_product_ids:
            return ()
        raise ProviderUnavailableError(
            self.provider_key, "Product reference resolution is unavailable."
        )

    def resolve_offer(self, provider_offer_key: str) -> NormalizedOffer:
        raise ProviderUnavailableError(
            self.provider_key, "Exact offer resolution is unavailable."
        )
