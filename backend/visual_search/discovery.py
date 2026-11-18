from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from backend.discovery.engine import DiscoveryEngine
from backend.discovery.models import DiscoveryResult
from backend.providers.base import NormalizedOffer, PriceInput
from backend.visual_search.analyzer import VisionAnalyzer
from backend.visual_search.image_processing import NormalizedImage
from backend.visual_search.models import VisualAnalysis


class VisualSearchQueryError(ValueError):
    pass


_ATTRIBUTE_WEIGHTS = {
    "product_category": 0.10,
    "product_type": 0.15,
    "brand": 0.20,
    "model": 0.20,
    "primary_color": 0.08,
    "secondary_colors": 0.07,
    "shape_or_design": 0.10,
    "distinctive_features": 0.10,
}
_IDENTITY_CONFIDENCE_THRESHOLD = 0.6
_TOKEN_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)


@dataclass(frozen=True)
class RankedOffer:
    offer: NormalizedOffer
    relevance_score: float
    matched_attributes: tuple[str, ...]
    match_basis: Literal["catalog_text", "no_catalog_text_evidence"]

    def as_dict(self) -> dict[str, object]:
        return {
            "offer_id": self.offer.offer_id,
            "relevance_score": self.relevance_score,
            "matched_attributes": list(self.matched_attributes),
            "match_basis": self.match_basis,
        }


@dataclass(frozen=True)
class VisualDiscoveryResult:
    analysis: VisualAnalysis
    search_query: str
    discovery: DiscoveryResult
    ranked_offers: tuple[RankedOffer, ...]

    @property
    def partial(self) -> bool:
        has_success = any(
            outcome.status == "success"
            for outcome in self.discovery.providers
        )
        return has_success and any(
            outcome.status != "success"
            for outcome in self.discovery.providers
        )

    def as_dict(self) -> dict[str, object]:
        result = self.discovery.as_dict()
        result["offers"] = [
            ranked_offer.offer.as_dict()
            for ranked_offer in self.ranked_offers
        ]
        result["visual_analysis"] = self.analysis.model_dump(mode="json")
        result["search_query"] = self.search_query
        result["rankings"] = [
            ranked_offer.as_dict()
            for ranked_offer in self.ranked_offers
        ]
        result["ranking_method"] = "uncalibrated_catalog_text"
        result["partial"] = self.partial
        return result


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(_TOKEN_PATTERN.findall(normalized))


def _contains_phrase(candidate_texts: tuple[str, ...], value: str) -> bool:
    phrase = _tokens(value)
    if not phrase:
        return False

    for candidate_text in candidate_texts:
        candidate = _tokens(candidate_text)
        if any(
            candidate[index : index + len(phrase)] == phrase
            for index in range(len(candidate) - len(phrase) + 1)
        ):
            return True
    return False


def _token_coverage(candidate_tokens: set[str], value: str) -> float:
    value_tokens = set(_tokens(value))
    if not value_tokens:
        return 0.0
    return len(candidate_tokens.intersection(value_tokens)) / len(value_tokens)


def _analysis_attributes(
    analysis: VisualAnalysis,
) -> tuple[tuple[str, str, float, bool], ...]:
    attributes: list[tuple[str, str, float, bool]] = []
    values: dict[str, str | None] = {
        "product_category": analysis.product_category,
        "product_type": analysis.product_type,
        "brand": analysis.brand,
        "model": analysis.model,
        "primary_color": analysis.primary_color,
        "secondary_colors": " ".join(analysis.secondary_colors) or None,
        "shape_or_design": analysis.shape_or_design,
        "distinctive_features": " ".join(analysis.distinctive_features) or None,
    }

    for name, value in values.items():
        confidence = getattr(analysis.confidence, name)
        if not value or confidence is None or confidence <= 0:
            continue
        identity_attribute = name in {"brand", "model"}
        if identity_attribute and confidence < _IDENTITY_CONFIDENCE_THRESHOLD:
            continue
        attributes.append((name, value, confidence, identity_attribute))
    return tuple(attributes)


def _rank_offer(
    offer: NormalizedOffer,
    attributes: tuple[tuple[str, str, float, bool], ...],
) -> RankedOffer:
    candidate_texts = tuple(
        value
        for value in (offer.product_name, offer.brand, offer.model)
        if value is not None and value.strip()
    )
    candidate_tokens = {
        token
        for value in candidate_texts
        for token in _tokens(value)
    }

    score = 0.0
    matched_attributes: list[str] = []
    for name, value, confidence, identity_attribute in attributes:
        if identity_attribute:
            coverage = (
                1.0 if _contains_phrase(candidate_texts, value) else 0.0
            )
        else:
            coverage = _token_coverage(candidate_tokens, value)
        if coverage <= 0:
            continue
        score += _ATTRIBUTE_WEIGHTS[name] * confidence * coverage
        matched_attributes.append(name)

    return RankedOffer(
        offer=offer,
        relevance_score=round(min(score, 1.0), 4),
        matched_attributes=tuple(matched_attributes),
        match_basis=(
            "catalog_text"
            if matched_attributes
            else "no_catalog_text_evidence"
        ),
    )


def _rank_offers(
    offers: tuple[NormalizedOffer, ...],
    analysis: VisualAnalysis,
) -> tuple[RankedOffer, ...]:
    attributes = _analysis_attributes(analysis)
    ranked = [
        _rank_offer(offer, attributes)
        for offer in offers
    ]
    return tuple(
        item[1]
        for item in sorted(
            enumerate(ranked),
            key=lambda item: (-item[1].relevance_score, item[0]),
        )
    )


class VisualDiscoveryService:
    def __init__(
        self,
        analyzer: VisionAnalyzer,
        discovery_engine: DiscoveryEngine,
    ) -> None:
        self._analyzer = analyzer
        self._discovery_engine = discovery_engine

    def discover(
        self,
        image: NormalizedImage,
        *,
        provider_key: str | None = None,
        max_price: PriceInput | None = None,
        currency: str | None = None,
        is_organic: bool | None = None,
    ) -> VisualDiscoveryResult:
        analysis = self._analyzer.analyze(image)
        search_query = analysis.search_query
        if not isinstance(search_query, str) or not search_query.strip():
            empty_result = DiscoveryResult(
                offers=(),
                providers=(),
                groups=(),
                price_summaries=(),
            )
            return VisualDiscoveryResult(
                analysis=analysis,
                search_query="",
                discovery=empty_result,
                ranked_offers=(),
            )
        search_query = search_query.strip()

        discovery_result = self._discovery_engine.search(
            search_query,
            provider_key=provider_key,
            max_price=max_price,
            currency=currency,
            is_organic=is_organic,
        )
        return VisualDiscoveryResult(
            analysis=analysis,
            search_query=search_query,
            discovery=discovery_result,
            ranked_offers=_rank_offers(discovery_result.offers, analysis),
        )
