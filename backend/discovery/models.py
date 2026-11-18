from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from backend.providers.base import NormalizedOffer


ProviderStatus = Literal[
    "success",
    "unavailable",
    "error",
    "timeout",
    "overloaded",
    "not_started",
    "unsupported",
]


@dataclass(frozen=True)
class ProviderOutcome:
    provider_key: str
    status: ProviderStatus
    offer_count: int
    error_code: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProductGroup:
    group_id: str
    canonical_product_id: str | None
    offers: tuple[NormalizedOffer, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "canonical_product_id": self.canonical_product_id,
            "offers": [offer.as_dict() for offer in self.offers],
        }


@dataclass(frozen=True)
class PriceSummary:
    currency: str
    minimum_price_minor: int
    maximum_price_minor: int
    offer_count: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResult:
    offers: tuple[NormalizedOffer, ...]
    providers: tuple[ProviderOutcome, ...]
    groups: tuple[ProductGroup, ...]
    price_summaries: tuple[PriceSummary, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "offers": [offer.as_dict() for offer in self.offers],
            "providers": [provider.as_dict() for provider in self.providers],
            "groups": [group.as_dict() for group in self.groups],
            "price_summaries": [
                summary.as_dict() for summary in self.price_summaries
            ],
        }
