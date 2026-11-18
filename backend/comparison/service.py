from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import TypeAlias

from backend.currency import currency_minor_unit_exponent
from backend.product_attributes import ProductAttribute
from backend.providers.base import (
    ProviderError,
    ProviderUnavailableError,
    ResolvedProviderProduct,
)
from backend.providers.registry import ProviderRegistry

from .models import (
    CandidateAssessment,
    ComparisonBudget,
    ComparisonCandidate,
    ComparisonPriceSummary,
    ComparisonRequest,
    ComparisonResult,
    CriterionEvaluation,
    EvidenceValue,
    MustHaveCriterion,
    PriceEvidence,
    ProductReference,
    Recommendation,
)


MAX_EVIDENCE_TEXT_LENGTH = 2000
_ExpectedValue: TypeAlias = str | bool | Decimal | Mapping[str, object]
_SUPPORTED_CRITERIA = {
    "brand",
    "model",
    "category",
    "product_name",
    "storage_capacity",
    "ram",
    "color",
    "size",
    "weight",
    "pack_quantity",
    "availability",
    "is_organic",
    "description_contains",
    "minimum_rating",
}


class ComparisonError(ValueError):
    pass


class ComparisonProviderError(ComparisonError):
    pass


class UnknownProductReferenceError(ComparisonError):
    pass


class ComparisonService:
    def __init__(self, provider_registry: ProviderRegistry) -> None:
        self._provider_registry = provider_registry

    def compare(
        self,
        request: ComparisonRequest | Mapping[str, object],
    ) -> ComparisonResult:
        if not isinstance(request, ComparisonRequest):
            try:
                request = ComparisonRequest.model_validate(request)
            except ValueError as exc:
                raise ComparisonError("The comparison request is invalid.") from exc

        candidates = self._resolve_candidates(request.candidates)
        assessments = tuple(
            self._assess(candidate, request.must_have, request.budget)
            for candidate in candidates
        )
        recommendation = self._recommend(
            assessments, bool(request.must_have or request.budget)
        )
        return ComparisonResult(
            assessments=assessments,
            price_summaries=_summarize_prices(
                assessment.candidate for assessment in assessments
            ),
            recommendation=recommendation,
        )

    def _resolve_candidates(
        self,
        references: tuple[ProductReference, ...],
    ) -> tuple[ComparisonCandidate, ...]:
        references_by_provider: dict[str, list[ProductReference]] = {}
        for reference in references:
            references_by_provider.setdefault(reference.provider_key, []).append(
                reference
            )

        resolved_by_reference: dict[
            tuple[str, str], ResolvedProviderProduct
        ] = {}
        for provider_key, provider_references in references_by_provider.items():
            provider_ids = tuple(
                reference.provider_product_id for reference in provider_references
            )
            try:
                provider = self._provider_registry.get(provider_key)
                products = provider.resolve_products(provider_ids)
            except ProviderUnavailableError as exc:
                raise ComparisonProviderError(
                    "A requested product provider is unavailable."
                ) from exc
            except ProviderError as exc:
                raise ComparisonProviderError(
                    "Product evidence could not be loaded."
                ) from exc

            for product in products:
                if not isinstance(product, ResolvedProviderProduct):
                    raise ComparisonProviderError(
                        "A provider returned invalid product evidence."
                    )
                offer = product.offer
                reference_key = (offer.provider_key, offer.provider_product_id)
                if (
                    offer.provider_key != provider_key
                    or offer.provider_product_id not in provider_ids
                    or reference_key in resolved_by_reference
                ):
                    raise ComparisonProviderError(
                        "A provider returned mismatched product evidence."
                    )
                resolved_by_reference[reference_key] = product

        missing = [
            reference
            for reference in references
            if (reference.provider_key, reference.provider_product_id)
            not in resolved_by_reference
        ]
        if missing:
            raise UnknownProductReferenceError(
                "One or more product references could not be resolved."
            )

        return tuple(
            self._build_candidate(
                reference,
                resolved_by_reference[
                    (reference.provider_key, reference.provider_product_id)
                ],
            )
            for reference in references
        )

    @staticmethod
    def _build_candidate(
        reference: ProductReference,
        product: ResolvedProviderProduct,
    ) -> ComparisonCandidate:
        offer = product.offer
        product_attributes = (
            product.product_attributes
            if product.product_attributes is not None
            else offer.product_attributes
        )
        if product_attributes is not None and product_attributes != offer.product_attributes:
            offer = replace(offer, product_attributes=product_attributes)
        facts: dict[str, EvidenceValue] = {
            "product_name": _evidence(
                offer.product_name, f"{offer.provider_key}.product_name"
            ),
            "brand": _evidence(
                offer.brand,
                f"{offer.provider_key}.brand",
                missing_reason="provider_did_not_supply",
            ),
            "model": _evidence(
                offer.model,
                f"{offer.provider_key}.model",
                missing_reason="provider_did_not_supply",
            ),
            "category": _bounded_text_evidence(
                product.category,
                f"{offer.provider_key}.products.category",
                missing_reason="not_available",
            ),
            "description": _bounded_text_evidence(
                product.description,
                f"{offer.provider_key}.products.description",
                missing_reason="not_available",
            ),
            "is_organic": _evidence(
                product.is_organic,
                f"{offer.provider_key}.products.is_organic",
                missing_reason="not_available",
            ),
            "price": (
                EvidenceValue(
                    value=PriceEvidence(offer.price_minor, offer.currency),
                    source=f"{offer.provider_key}.products.price_and_currency",
                    status="known",
                )
                if offer.price_minor is not None and offer.currency is not None
                else EvidenceValue(
                    value=None,
                    source=f"{offer.provider_key}.products.price_and_currency",
                    status="unknown",
                    reason="price_or_currency_unverified",
                )
            ),
            "availability": (
                _evidence(
                    offer.availability,
                    f"{offer.provider_key}.availability",
                )
                if offer.availability != "unknown"
                else EvidenceValue(
                    value=None,
                    source=f"{offer.provider_key}.availability",
                    status="unknown",
                    reason="provider_did_not_supply",
                )
            ),
            "product_url": _evidence(
                offer.product_url,
                f"{offer.provider_key}.product_url",
                missing_reason="provider_did_not_supply",
            ),
            "image_url": _evidence(
                offer.image_url,
                f"{offer.provider_key}.image_url",
                missing_reason="provider_did_not_supply",
            ),
            "average_rating": _rating_evidence(
                product.average_rating,
                product.review_count,
                f"{offer.provider_key}.reviews.rating",
            ),
            "review_count": _evidence(
                product.review_count,
                f"{offer.provider_key}.reviews.review_count",
                missing_reason="not_available",
            ),
            "specifications": EvidenceValue(
                value=None,
                source=f"{offer.provider_key}.specifications",
                status="unknown",
                reason="provider_did_not_supply",
            ),
        }
        for attribute in product_attributes or ():
            ComparisonService._add_attribute_evidence(facts, attribute)
        return ComparisonCandidate(reference=reference, offer=offer, evidence=facts)

    @staticmethod
    def _add_attribute_evidence(
        facts: dict[str, EvidenceValue],
        attribute: ProductAttribute,
    ) -> None:
        existing = facts.get(attribute.name)
        if attribute.provenance == "unknown":
            if existing is None:
                facts[attribute.name] = EvidenceValue(
                    value=None,
                    source="unknown",
                    status="unknown",
                    reason="attribute_not_available",
                )
            return

        source = attribute.provenance
        if attribute.extraction_method == "product_title":
            if existing is not None and existing.status == "known":
                return
            source = f"{source}.product_title"
            facts[attribute.name] = EvidenceValue(
                value=None,
                source=source,
                status="unknown",
                reason="title_extracted_not_verified",
            )
            return

        value = attribute.value
        if value is None:
            return
        if attribute.unit is not None:
            value = f"{value} {attribute.unit}"
        facts[attribute.name] = EvidenceValue(
            value=value,
            source=source,
            status="known",
        )

    @staticmethod
    def _assess(
        candidate: ComparisonCandidate,
        must_have: tuple[MustHaveCriterion, ...],
        budget: ComparisonBudget | None,
    ) -> CandidateAssessment:
        evaluations = [
            _evaluate_criterion(candidate, criterion) for criterion in must_have
        ]
        if budget is not None:
            evaluations.append(_evaluate_budget(candidate, budget))

        if not evaluations:
            eligibility = "undetermined"
        elif any(item.status == "not_met" for item in evaluations):
            eligibility = "ineligible"
        elif any(item.status == "unknown" for item in evaluations):
            eligibility = "undetermined"
        else:
            eligibility = "eligible"
        return CandidateAssessment(
            candidate=candidate,
            evaluations=tuple(evaluations),
            eligibility=eligibility,
        )

    @staticmethod
    def _recommend(
        assessments: tuple[CandidateAssessment, ...],
        has_requirements: bool,
    ) -> Recommendation:
        eligible = [
            assessment
            for assessment in assessments
            if assessment.eligibility == "eligible"
        ]
        undetermined = [
            assessment
            for assessment in assessments
            if assessment.eligibility == "undetermined"
        ]
        if not has_requirements:
            return Recommendation(
                status="no_clear_winner",
                candidate=None,
                basis=("No budget or must-have requirements were provided.",),
            )
        if len(eligible) == 1 and not undetermined:
            winner = eligible[0]
            basis = tuple(
                evaluation.attribute
                for evaluation in winner.evaluations
                if evaluation.status == "met"
            )
            return Recommendation(
                status="recommended",
                candidate=winner.candidate.reference,
                basis=basis,
            )
        if undetermined:
            return Recommendation(
                status="insufficient_evidence",
                candidate=None,
                basis=("Required product evidence is incomplete.",),
            )
        return Recommendation(
            status="no_clear_winner",
            candidate=None,
            basis=("Available evidence does not establish a unique match.",),
        )


def _evidence(
    value: str | bool | int | float | None,
    source: str,
    *,
    missing_reason: str = "not_available",
) -> EvidenceValue:
    return EvidenceValue(
        value=value,
        source=source,
        status="known" if value is not None else "unknown",
        reason=None if value is not None else missing_reason,
    )


def _bounded_text_evidence(
    value: str | None,
    source: str,
    *,
    missing_reason: str,
) -> EvidenceValue:
    if value is None or not value.strip():
        return EvidenceValue(
            value=None,
            source=source,
            status="unknown",
            reason=missing_reason,
        )
    truncated = len(value) > MAX_EVIDENCE_TEXT_LENGTH
    return EvidenceValue(
        value=value[:MAX_EVIDENCE_TEXT_LENGTH],
        source=source,
        status="known",
        truncated=truncated,
    )


def _rating_evidence(
    average_rating: float | None,
    review_count: int | None,
    source: str,
) -> EvidenceValue:
    if (
        review_count is None
        or review_count <= 0
        or average_rating is None
        or not math.isfinite(average_rating)
        or not 0 <= average_rating <= 5
    ):
        reason = "no_reviews" if review_count == 0 else "rating_not_available"
        return EvidenceValue(
            value=None,
            source=source,
            status="unknown",
            reason=reason,
        )
    return EvidenceValue(value=average_rating, source=source, status="known")


def _summarize_prices(
    candidates: Iterable[ComparisonCandidate],
) -> tuple[ComparisonPriceSummary, ...]:
    prices_by_currency: dict[str, list[tuple[ProductReference, PriceEvidence]]] = (
        defaultdict(list)
    )
    for candidate in candidates:
        evidence = candidate.evidence["price"]
        if isinstance(evidence.value, PriceEvidence):
            prices_by_currency[evidence.value.currency].append(
                (candidate.reference, evidence.value)
            )

    summaries = []
    for currency, entries in sorted(prices_by_currency.items()):
        amounts = [price.amount_minor for _, price in entries]
        minimum = min(amounts)
        maximum = max(amounts)
        summaries.append(
            ComparisonPriceSummary(
                currency=currency,
                minimum_price_minor=minimum,
                maximum_price_minor=maximum,
                offer_count=len(entries),
                candidate_references=tuple(reference for reference, _ in entries),
                minimum_price_references=tuple(
                    reference
                    for reference, price in entries
                    if price.amount_minor == minimum
                ),
                maximum_price_references=tuple(
                    reference
                    for reference, price in entries
                    if price.amount_minor == maximum
                ),
            )
        )
    return tuple(summaries)


def _evaluate_criterion(
    candidate: ComparisonCandidate,
    criterion: MustHaveCriterion,
) -> CriterionEvaluation:
    attribute = criterion.attribute
    evidence = candidate.evidence.get(attribute)
    expected = criterion.expected_value
    if attribute not in _SUPPORTED_CRITERIA:
        if (
            evidence is None
            or evidence.status != "known"
            or evidence.source
            not in {"verified_provider", "local_catalog", "demo_sample"}
        ):
            return _unknown_evaluation(
                attribute,
                expected,
                EvidenceValue(
                    value=None,
                    source=f"{candidate.offer.provider_key}.attribute.{attribute}",
                    status="unknown",
                    reason="attribute_not_supported",
                ),
                "attribute_not_supported",
            )

    if attribute == "description_contains":
        evidence = candidate.evidence["description"]
        if not isinstance(expected, str):
            return _unknown_evaluation(attribute, expected, evidence, "invalid_value")
        if evidence.value is None:
            return _unknown_evaluation(attribute, expected, evidence, evidence.reason)
        matched = expected.casefold() in str(evidence.value).casefold()
        return CriterionEvaluation(
            attribute=attribute,
            expected_value=expected,
            status="met" if matched else "not_met",
            evidence=evidence,
        )

    if attribute == "minimum_rating":
        evidence = candidate.evidence["average_rating"]
        if not isinstance(expected, Decimal) or not 0 <= expected <= 5:
            return _unknown_evaluation(attribute, expected, evidence, "invalid_value")
        if not isinstance(evidence.value, (int, float)):
            return _unknown_evaluation(attribute, expected, evidence, evidence.reason)
        return CriterionEvaluation(
            attribute=attribute,
            expected_value=expected,
            status=(
                "met"
                if Decimal(str(evidence.value)) >= expected
                else "not_met"
            ),
            evidence=evidence,
        )

    if evidence is None or evidence.value is None:
        return _unknown_evaluation(
            attribute,
            expected,
            evidence
            or EvidenceValue(
                value=None,
                source=f"provider.attribute.{attribute}",
                status="unknown",
                reason="attribute_not_available",
            ),
            "attribute_not_available" if evidence is None else evidence.reason,
        )

    if isinstance(evidence.value, str) and isinstance(expected, str):
        matched = " ".join(evidence.value.casefold().split()) == " ".join(
            expected.casefold().split()
        )
    else:
        matched = evidence.value == expected
    return CriterionEvaluation(
        attribute=attribute,
        expected_value=expected,
        status="met" if matched else "not_met",
        evidence=evidence,
    )


def _evaluate_budget(
    candidate: ComparisonCandidate,
    budget: ComparisonBudget,
) -> CriterionEvaluation:
    price_evidence = candidate.evidence["price"]
    expected = {
        "amount": str(budget.amount),
        "currency": budget.currency,
    }
    if not isinstance(price_evidence.value, PriceEvidence):
        return _unknown_evaluation(
            "max_price", expected, price_evidence, price_evidence.reason
        )
    price = price_evidence.value
    if price.currency != budget.currency:
        return _unknown_evaluation(
            "max_price", expected, price_evidence, "currency_mismatch"
        )

    exponent = currency_minor_unit_exponent(budget.currency)
    candidate_amount = Decimal(price.amount_minor) / Decimal(10**exponent)
    return CriterionEvaluation(
        attribute="max_price",
        expected_value=expected,
        status="met" if candidate_amount <= budget.amount else "not_met",
        evidence=price_evidence,
    )


def _unknown_evaluation(
    attribute: str,
    expected: _ExpectedValue,
    evidence: EvidenceValue,
    reason: str | None,
) -> CriterionEvaluation:
    return CriterionEvaluation(
        attribute=attribute,
        expected_value=expected,
        status="unknown",
        evidence=evidence,
        reason=reason or "evidence_not_available",
    )
