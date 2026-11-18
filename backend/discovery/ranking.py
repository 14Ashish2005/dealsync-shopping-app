from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from backend.providers.base import NormalizedOffer
from backend.providers.filtering import remove_category_terms


_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_DEMO_SAMPLE_PREFIX = re.compile(r"^\[DEMO SAMPLE\]\s*", re.IGNORECASE)
_EXACT_NAME_POINTS = 40
_NAME_TOKEN_COVERAGE_POINTS = 40
_EXPLICIT_BRAND_POINTS = 10
_EXPLICIT_MODEL_POINTS = 10
_GENERIC_INTENT_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "best",
        "buy",
        "deal",
        "deals",
        "find",
        "for",
        "get",
        "item",
        "items",
        "looking",
        "look",
        "me",
        "need",
        "or",
        "please",
        "product",
        "products",
        "rated",
        "search",
        "show",
        "the",
        "top",
        "want",
        "with",
    }
)


@dataclass(frozen=True)
class RelevanceScore:
    exact_product_name: int
    product_name_token_coverage: int
    explicit_brand_match: int
    explicit_model_match: int

    @property
    def total(self) -> int:
        return (
            self.exact_product_name
            + self.product_name_token_coverage
            + self.explicit_brand_match
            + self.explicit_model_match
        )


@dataclass(frozen=True)
class RankedOffer:
    offer: NormalizedOffer
    score: RelevanceScore


def _meaningful_tokens(value: str, category: str | None) -> tuple[str, ...]:
    category_free = remove_category_terms(value, category)
    tokens = (
        token
        for token in _TOKEN_PATTERN.findall(category_free.casefold())
        if token not in _GENERIC_INTENT_WORDS
    )
    return tuple(dict.fromkeys(tokens))


def _contains_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    if not phrase or len(phrase) > len(tokens):
        return False
    return any(
        tokens[index : index + len(phrase)] == phrase
        for index in range(len(tokens) - len(phrase) + 1)
    )


def score_offer(
    offer: NormalizedOffer,
    *,
    query: str,
    category: str | None = None,
) -> RelevanceScore:
    """Score lexical evidence only; filters and non-text offer data are not signals."""
    query_tokens = _meaningful_tokens(query, category)
    product_name = _DEMO_SAMPLE_PREFIX.sub("", offer.product_name, count=1)
    name_tokens = _meaningful_tokens(product_name, category)

    exact_product_name = (
        _EXACT_NAME_POINTS if query_tokens and query_tokens == name_tokens else 0
    )
    if query_tokens:
        matching_tokens = len(set(query_tokens).intersection(name_tokens))
        token_coverage = (
            _NAME_TOKEN_COVERAGE_POINTS * matching_tokens // len(query_tokens)
        )
    else:
        token_coverage = 0

    explicit_brand_match = 0
    if offer.brand:
        brand_tokens = _meaningful_tokens(offer.brand, category)
        if _contains_phrase(query_tokens, brand_tokens):
            explicit_brand_match = _EXPLICIT_BRAND_POINTS

    explicit_model_match = 0
    if offer.model:
        model_tokens = _meaningful_tokens(offer.model, category)
        if _contains_phrase(query_tokens, model_tokens):
            explicit_model_match = _EXPLICIT_MODEL_POINTS

    return RelevanceScore(
        exact_product_name=exact_product_name,
        product_name_token_coverage=token_coverage,
        explicit_brand_match=explicit_brand_match,
        explicit_model_match=explicit_model_match,
    )


def rank_offers(
    offers: Iterable[NormalizedOffer],
    *,
    query: str,
    category: str | None = None,
) -> tuple[RankedOffer, ...]:
    """Rank already-filtered offers by explainable text relevance, stably."""
    ranked = [
        RankedOffer(
            offer=offer,
            score=score_offer(offer, query=query, category=category),
        )
        for offer in offers
    ]
    return tuple(
        sorted(ranked, key=lambda item: item.score.total, reverse=True)
    )
