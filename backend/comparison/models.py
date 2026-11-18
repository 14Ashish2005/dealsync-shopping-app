from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from backend.currency import parse_major_amount, to_minor_units, validate_currency_code
from backend.providers.base import NormalizedOffer


class ProductReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_key: StrictStr = Field(min_length=1, max_length=64)
    provider_product_id: StrictStr = Field(min_length=1, max_length=128)


class ComparisonBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: Decimal
    currency: StrictStr

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> Decimal:
        return parse_major_amount(value)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        return validate_currency_code(value)

    @model_validator(mode="after")
    def validate_minor_units(self) -> ComparisonBudget:
        to_minor_units(self.amount, self.currency)
        return self


class MustHaveCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute: StrictStr = Field(min_length=1, max_length=64)
    expected_value: StrictBool | StrictStr | Decimal

    @field_validator("attribute")
    @classmethod
    def normalize_attribute(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("Criterion attribute must not be blank.")
        return normalized

    @field_validator("expected_value")
    @classmethod
    def validate_expected_value(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip() or len(value) > 120:
                raise ValueError("Criterion text must contain 1 to 120 characters.")
        elif isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("Criterion number must be finite.")
        return value

    @model_validator(mode="after")
    def validate_attribute_value(self) -> MustHaveCriterion:
        if self.attribute == "is_organic" and not isinstance(
            self.expected_value, bool
        ):
            raise ValueError("The organic criterion requires a boolean value.")
        if self.attribute == "minimum_rating":
            if (
                not isinstance(self.expected_value, Decimal)
                or not self.expected_value.is_finite()
                or not 0 <= self.expected_value <= 5
            ):
                raise ValueError("The minimum rating must be between 0 and 5.")
        elif self.attribute != "is_organic" and not isinstance(
            self.expected_value, str
        ):
            raise ValueError("This criterion requires a text value.")
        return self


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: tuple[ProductReference, ...] = Field(min_length=2, max_length=5)
    budget: ComparisonBudget | None = None
    must_have: tuple[MustHaveCriterion, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def reject_duplicate_candidates(self) -> ComparisonRequest:
        references = [
            (item.provider_key, item.provider_product_id)
            for item in self.candidates
        ]
        if len(references) != len(set(references)):
            raise ValueError("Comparison candidates must be unique.")
        return self


@dataclass(frozen=True)
class PriceEvidence:
    amount_minor: int
    currency: str


EvidenceStatus = Literal["known", "unknown"]
CriterionStatus = Literal["met", "not_met", "unknown"]
EligibilityStatus = Literal["eligible", "ineligible", "undetermined"]
RecommendationStatus = Literal[
    "recommended", "no_clear_winner", "insufficient_evidence"
]


@dataclass(frozen=True)
class EvidenceValue:
    value: str | bool | int | float | PriceEvidence | None
    source: str
    status: EvidenceStatus
    reason: str | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if (self.status == "known") != (self.value is not None):
            raise ValueError("Evidence status must match whether a value is present.")

    def as_dict(self) -> dict[str, object]:
        value: object = self.value
        if isinstance(value, PriceEvidence):
            value = {
                "amount_minor": value.amount_minor,
                "currency": value.currency,
            }
        return {
            "value": value,
            "source": self.source,
            "status": self.status,
            "reason": self.reason,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class ComparisonCandidate:
    reference: ProductReference
    offer: NormalizedOffer
    evidence: dict[str, EvidenceValue]


@dataclass(frozen=True)
class CriterionEvaluation:
    attribute: str
    expected_value: str | bool | Decimal | Mapping[str, object]
    status: CriterionStatus
    evidence: EvidenceValue
    reason: str | None = None


@dataclass(frozen=True)
class CandidateAssessment:
    candidate: ComparisonCandidate
    evaluations: tuple[CriterionEvaluation, ...]
    eligibility: EligibilityStatus


@dataclass(frozen=True)
class ComparisonPriceSummary:
    currency: str
    minimum_price_minor: int
    maximum_price_minor: int
    offer_count: int
    candidate_references: tuple[ProductReference, ...]
    minimum_price_references: tuple[ProductReference, ...]
    maximum_price_references: tuple[ProductReference, ...]


@dataclass(frozen=True)
class Recommendation:
    status: RecommendationStatus
    candidate: ProductReference | None
    basis: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonResult:
    assessments: tuple[CandidateAssessment, ...]
    price_summaries: tuple[ComparisonPriceSummary, ...]
    recommendation: Recommendation

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": [
                {
                    "reference": assessment.candidate.reference.model_dump(),
                    "offer": assessment.candidate.offer.as_dict(),
                    "evidence": {
                        key: value.as_dict()
                        for key, value in assessment.candidate.evidence.items()
                    },
                    "evaluations": [
                        {
                            "attribute": evaluation.attribute,
                            "expected_value": _serialize_expected(
                                evaluation.expected_value
                            ),
                            "status": evaluation.status,
                            "evidence": evaluation.evidence.as_dict(),
                            "reason": evaluation.reason,
                        }
                        for evaluation in assessment.evaluations
                    ],
                    "eligibility": assessment.eligibility,
                }
                for assessment in self.assessments
            ],
            "price_summaries": [
                {
                    "currency": summary.currency,
                    "minimum_price_minor": summary.minimum_price_minor,
                    "maximum_price_minor": summary.maximum_price_minor,
                    "offer_count": summary.offer_count,
                    "candidate_references": [
                        reference.model_dump()
                        for reference in summary.candidate_references
                    ],
                    "minimum_price_references": [
                        reference.model_dump()
                        for reference in summary.minimum_price_references
                    ],
                    "maximum_price_references": [
                        reference.model_dump()
                        for reference in summary.maximum_price_references
                    ],
                }
                for summary in self.price_summaries
            ],
            "recommendation": {
                "status": self.recommendation.status,
                "candidate": (
                    self.recommendation.candidate.model_dump()
                    if self.recommendation.candidate is not None
                    else None
                ),
                "basis": list(self.recommendation.basis),
            },
        }


def _serialize_expected(
    value: str | bool | Decimal | Mapping[str, object],
) -> str | bool | dict[str, object]:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return dict(value)
    return value
