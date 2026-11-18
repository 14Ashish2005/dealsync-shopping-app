from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from backend.comparison.models import ComparisonResult, ProductReference
from backend.product_attributes import (
    AttributeExtractionMethod,
    AttributeProvenance,
    AttributeValue,
)


class ComparisonAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StructuredAttributeResponse(ComparisonAPIModel):
    name: str
    value: AttributeValue
    provenance: AttributeProvenance
    extraction_method: AttributeExtractionMethod | None
    unit: str | None


class ComparisonOfferResponse(ComparisonAPIModel):
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
    product_attributes: list[StructuredAttributeResponse] | None = None


class ComparisonEvidenceResponse(ComparisonAPIModel):
    value: Any
    source: str
    status: Literal["known", "unknown"]
    reason: str | None
    truncated: bool


class ComparisonEvaluationResponse(ComparisonAPIModel):
    attribute: str
    expected_value: Any
    status: Literal["met", "not_met", "unknown"]
    evidence: ComparisonEvidenceResponse
    reason: str | None


class ComparisonCandidateResponse(ComparisonAPIModel):
    reference: ProductReference
    offer: ComparisonOfferResponse
    evidence: dict[str, ComparisonEvidenceResponse]
    known_attributes: list[str]
    unknown_attributes: list[str]
    must_have_evaluations: list[ComparisonEvaluationResponse]
    budget_evaluation: ComparisonEvaluationResponse | None
    eligibility: Literal["eligible", "ineligible", "undetermined"]


class ComparisonPriceSummaryResponse(ComparisonAPIModel):
    currency: str
    minimum_price_minor: int
    maximum_price_minor: int
    offer_count: int
    candidate_references: list[ProductReference]
    minimum_price_references: list[ProductReference]
    maximum_price_references: list[ProductReference]


class ComparisonRecommendationResponse(ComparisonAPIModel):
    status: Literal["recommended", "no_clear_winner", "insufficient_evidence"]
    candidate: ProductReference | None
    basis: list[str]


class ComparisonAPIResponse(ComparisonAPIModel):
    candidates: list[ComparisonCandidateResponse]
    price_summaries: list[ComparisonPriceSummaryResponse]
    recommendation: ComparisonRecommendationResponse
    reasons: list[str]
    limitations: list[str]


def build_comparison_api_response(
    result: ComparisonResult,
) -> ComparisonAPIResponse:
    candidates: list[ComparisonCandidateResponse] = []
    for assessment in result.assessments:
        candidate = assessment.candidate
        evidence = {
            name: ComparisonEvidenceResponse.model_validate(item.as_dict())
            for name, item in candidate.evidence.items()
        }
        known_attributes = [
            name for name, item in evidence.items() if item.status == "known"
        ]
        unknown_attributes = [
            name for name, item in evidence.items() if item.status == "unknown"
        ]
        must_have_evaluations: list[ComparisonEvaluationResponse] = []
        budget_evaluation: ComparisonEvaluationResponse | None = None
        for evaluation in assessment.evaluations:
            expected_value: Any = evaluation.expected_value
            if isinstance(expected_value, Decimal):
                expected_value = str(expected_value)
            elif isinstance(expected_value, Mapping):
                expected_value = dict(expected_value)
            response_evaluation = ComparisonEvaluationResponse(
                attribute=evaluation.attribute,
                expected_value=expected_value,
                status=evaluation.status,
                evidence=ComparisonEvidenceResponse.model_validate(
                    evaluation.evidence.as_dict()
                ),
                reason=evaluation.reason,
            )
            if (
                evaluation.attribute == "max_price"
                and isinstance(evaluation.expected_value, Mapping)
            ):
                budget_evaluation = response_evaluation
                if evaluation.status == "unknown":
                    unknown_attributes.append("max_price")
            else:
                must_have_evaluations.append(response_evaluation)
                if (
                    evaluation.status == "unknown"
                    and evaluation.attribute not in unknown_attributes
                ):
                    unknown_attributes.append(evaluation.attribute)

        candidates.append(
            ComparisonCandidateResponse(
                reference=candidate.reference,
                offer=ComparisonOfferResponse.model_validate(
                    candidate.offer.as_dict()
                ),
                evidence=evidence,
                known_attributes=known_attributes,
                unknown_attributes=unknown_attributes,
                must_have_evaluations=must_have_evaluations,
                budget_evaluation=budget_evaluation,
                eligibility=assessment.eligibility,
            )
        )

    price_summaries = [
        ComparisonPriceSummaryResponse(
            currency=summary.currency,
            minimum_price_minor=summary.minimum_price_minor,
            maximum_price_minor=summary.maximum_price_minor,
            offer_count=summary.offer_count,
            candidate_references=list(summary.candidate_references),
            minimum_price_references=list(summary.minimum_price_references),
            maximum_price_references=list(summary.maximum_price_references),
        )
        for summary in result.price_summaries
    ]
    recommendation = ComparisonRecommendationResponse(
        status=result.recommendation.status,
        candidate=result.recommendation.candidate,
        basis=list(result.recommendation.basis),
    )
    if recommendation.status == "recommended":
        reasons = [
            "The recommended candidate uniquely met the supplied requirements."
        ]
    elif recommendation.status == "insufficient_evidence":
        reasons = [
            "A recommendation was withheld because required evidence is unknown or incomparable."
        ]
    elif result.recommendation.basis and result.recommendation.basis[0].startswith(
        "No budget or must-have"
    ):
        reasons = ["No budget or must-have requirements were supplied."]
    else:
        reasons = ["Available evidence did not establish a unique qualifying candidate."]

    return ComparisonAPIResponse(
        candidates=candidates,
        price_summaries=price_summaries,
        recommendation=recommendation,
        reasons=reasons,
        limitations=[
            "Unavailable or unsupported product attributes remain unknown.",
            "Prices are compared only within the same verified currency; no conversion is performed.",
            "Preferred criteria are not evaluated by this endpoint.",
        ],
    )
