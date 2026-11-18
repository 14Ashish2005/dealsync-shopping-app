from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from backend.providers.base import NormalizedOffer
from backend.discovery.models import PriceSummary, ProductGroup


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _attribute_identity(value: object, unit: str | None) -> tuple[str, str, str]:
    return (
        type(value).__name__,
        _normalized_text(str(value)),
        _normalized_text(unit or ""),
    )


def _has_conflicting_attributes(offers: list[NormalizedOffer]) -> bool:
    values_by_name: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for offer in offers:
        for attribute in offer.product_attributes or ():
            if attribute.value is not None:
                values_by_name[attribute.name].add(
                    _attribute_identity(attribute.value, attribute.unit)
                )
    return any(len(values) > 1 for values in values_by_name.values())


def _has_conflicting_identity(offers: list[NormalizedOffer]) -> bool:
    names = {_normalized_text(offer.product_name) for offer in offers}
    if len(names) != 1:
        return True

    brands = {
        _normalized_text(offer.brand)
        for offer in offers
        if offer.brand is not None and offer.brand.strip()
    }
    if len(brands) > 1:
        return True

    models = {
        _normalized_text(offer.model)
        for offer in offers
        if offer.model is not None and offer.model.strip()
    }
    return len(models) > 1 or _has_conflicting_attributes(offers)


def group_offers(offers: Iterable[NormalizedOffer]) -> tuple[ProductGroup, ...]:
    offers_by_canonical_id: dict[
        str, list[tuple[int, NormalizedOffer]]
    ] = defaultdict(list)
    unmatched: list[tuple[int, NormalizedOffer]] = []

    for position, offer in enumerate(offers):
        canonical_id = offer.canonical_product_id
        if canonical_id is None or not canonical_id.strip():
            unmatched.append((position, offer))
        else:
            offers_by_canonical_id[canonical_id].append((position, offer))

    positioned_groups: list[tuple[int, ProductGroup]] = []
    for canonical_id, indexed_candidates in offers_by_canonical_id.items():
        candidates = [offer for _position, offer in indexed_candidates]
        product_ids_by_provider: dict[str, set[str]] = defaultdict(set)
        for offer in candidates:
            product_ids_by_provider[offer.provider_key].add(
                offer.provider_product_id
            )

        provider_collision = any(
            len(product_ids) > 1 for product_ids in product_ids_by_provider.values()
        )
        if provider_collision or _has_conflicting_identity(candidates):
            unmatched.extend(indexed_candidates)
            continue

        positioned_groups.append(
            (
                indexed_candidates[0][0],
                ProductGroup(
                    group_id=f"canonical:{canonical_id}",
                    canonical_product_id=canonical_id,
                    offers=tuple(candidates),
                ),
            )
        )

    for position, offer in unmatched:
        positioned_groups.append(
            (
                position,
                ProductGroup(
                    group_id=f"offer:{offer.provider_key}:{offer.provider_offer_key}",
                    canonical_product_id=None,
                    offers=(offer,),
                ),
            )
        )

    positioned_groups.sort(key=lambda item: item[0])
    return tuple(group for _position, group in positioned_groups)


def summarize_prices(
    offers: Iterable[NormalizedOffer],
) -> tuple[PriceSummary, ...]:
    prices_by_currency: dict[str, list[int]] = defaultdict(list)
    for offer in offers:
        if offer.price_minor is not None and offer.currency is not None:
            prices_by_currency[offer.currency].append(offer.price_minor)

    return tuple(
        PriceSummary(
            currency=currency,
            minimum_price_minor=min(prices),
            maximum_price_minor=max(prices),
            offer_count=len(prices),
        )
        for currency, prices in sorted(prices_by_currency.items())
    )
