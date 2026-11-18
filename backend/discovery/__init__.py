from backend.discovery.engine import DiscoveryEngine
from backend.discovery.matcher import group_offers
from backend.discovery.models import (
    DiscoveryResult,
    PriceSummary,
    ProductGroup,
    ProviderOutcome,
)

__all__ = [
    "DiscoveryEngine",
    "DiscoveryResult",
    "PriceSummary",
    "ProductGroup",
    "ProviderOutcome",
    "group_offers",
]
