from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from backend.comparison.api_models import (
    ComparisonAPIResponse,
    ComparisonCandidateResponse,
    build_comparison_api_response,
)
from backend.comparison.models import (
    ComparisonBudget,
    ComparisonRequest,
    MustHaveCriterion,
    ProductReference,
)
from backend.comparison.service import (
    ComparisonError,
    ComparisonProviderError,
    ComparisonService,
    UnknownProductReferenceError,
)
from backend.currency import currency_minor_unit_exponent, format_major_amount
from backend.discovery import DiscoveryEngine
from backend.providers.base import ProviderError, ResolvedProviderProduct
from backend.providers.registry import ProviderRegistry

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


MAX_CHAT_DISCOVERY_RESULTS = 10
_CLARIFICATION = (
    "Which two or more products would you like me to compare? "
    "Please name them or choose from the products I find."
)
_STATED_STORAGE_PATTERN = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(GB|TB)\b",
    re.IGNORECASE,
)
_FIFTH_GENERATION_PATTERN = re.compile(r"\b5g\b", re.IGNORECASE)


class DiscoverySearchToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(min_length=1, max_length=160)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("A product search query is required.")
        return normalized


class ComparisonChatToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ProductReference] = Field(default_factory=list, max_length=5)
    budget: ComparisonBudget | None = None
    must_have: list[MustHaveCriterion] = Field(default_factory=list, max_length=8)


@dataclass
class ComparisonChatContext:
    user_messages: tuple[str, ...] = ()
    selected_products: tuple[ResolvedProviderProduct, ...] = ()
    discovered_references: set[tuple[str, str]] = field(default_factory=set)
    comparison_result: ComparisonAPIResponse | None = None
    clarification: str | None = None
    unavailable_message: str | None = None

    def __post_init__(self) -> None:
        self.discovered_references.update(
            (
                product.offer.provider_key,
                product.offer.provider_product_id,
            )
            for product in self.selected_products
        )


def create_comparison_chat_tools(
    *,
    discovery_engine: DiscoveryEngine | None,
    registry: ProviderRegistry,
    context: ComparisonChatContext,
) -> tuple[BaseTool, BaseTool, BaseTool]:
    from langchain.tools import tool

    @tool(args_schema=DiscoverySearchToolRequest)
    def search_discovery_products(query: str) -> str:
        """Find current offers and return their exact provider product references."""
        context.unavailable_message = None
        if discovery_engine is None:
            context.unavailable_message = (
                "Product discovery is temporarily unavailable. Please try again."
            )
            return json.dumps({
                "status": "unavailable",
                "message": context.unavailable_message,
            })

        try:
            result = discovery_engine.search(query)
        except (ProviderError, ValueError):
            context.unavailable_message = (
                "Product discovery is temporarily unavailable. Please try again."
            )
            return json.dumps({
                "status": "unavailable",
                "message": context.unavailable_message,
            })

        selected_offers = result.offers[:MAX_CHAT_DISCOVERY_RESULTS]
        offers = []
        for offer in selected_offers:
            reference = (offer.provider_key, offer.provider_product_id)
            context.discovered_references.add(reference)
            price = None
            if offer.price_minor is not None and offer.currency is not None:
                exponent = currency_minor_unit_exponent(offer.currency)
                price = format_major_amount(
                    Decimal(offer.price_minor) / Decimal(10**exponent),
                    offer.currency,
                )
            offers.append({
                "reference": {
                    "provider_key": offer.provider_key,
                    "provider_product_id": offer.provider_product_id,
                },
                "offer_id": offer.offer_id,
                "product_name": offer.product_name,
                "brand": offer.brand,
                "model": offer.model,
                "price": price,
                "currency": offer.currency,
                "availability": offer.availability,
            })

        succeeded = any(
            provider.status == "success" for provider in result.providers
        )
        partial = succeeded and any(
            provider.status != "success" for provider in result.providers
        )
        return json.dumps({
            "status": "success" if succeeded else "unavailable",
            "partial": partial,
            "providers": [provider.as_dict() for provider in result.providers],
            "offers": offers,
            "truncated": len(result.offers) > len(selected_offers),
        })

    @tool
    def get_selected_products() -> str:
        """Return products selected for comparison and resolved for this request."""
        selected = []
        for product in context.selected_products:
            offer = product.offer
            price = None
            if offer.price_minor is not None and offer.currency is not None:
                exponent = currency_minor_unit_exponent(offer.currency)
                price = format_major_amount(
                    Decimal(offer.price_minor) / (10 ** exponent),
                    offer.currency,
                )
            selected.append({
                "reference": {
                    "provider_key": offer.provider_key,
                    "provider_product_id": offer.provider_product_id,
                },
                "product_name": offer.product_name,
                "brand": offer.brand,
                "model": offer.model,
                "price": price,
                "currency": offer.currency,
                "availability": offer.availability,
                "category": product.category,
                "description": product.description,
                "is_organic": product.is_organic,
                "average_rating": product.average_rating,
                "review_count": product.review_count,
            })
        return json.dumps({
            "status": "success" if selected else "no_selection",
            "products": selected,
        })

    @tool(args_schema=ComparisonChatToolRequest)
    def compare_products(
        candidates: list[dict[str, object]] | None = None,
        budget: dict[str, object] | None = None,
        must_have: list[dict[str, object]] | None = None,
    ) -> str:
        """Compare discovered provider product references using verified catalog evidence."""
        context.comparison_result = None
        context.clarification = None
        context.unavailable_message = None
        supplied_candidates = candidates or []
        if len(supplied_candidates) < 2:
            context.clarification = _CLARIFICATION
            return json.dumps({
                "status": "clarification_required",
                "message": context.clarification,
            })

        try:
            request = ComparisonRequest.model_validate({
                "candidates": supplied_candidates,
                "budget": budget,
                "must_have": must_have or [],
            })
        except ValueError:
            context.clarification = _CLARIFICATION
            return json.dumps({
                "status": "clarification_required",
                "message": context.clarification,
            })

        if request.budget is not None and not _budget_was_explicitly_stated(
            request.budget,
            context.user_messages,
        ):
            context.clarification = (
                "Please state the maximum amount with its ISO currency code, "
                "for example, 20 USD."
            )
            return json.dumps({
                "status": "clarification_required",
                "message": context.clarification,
            })

        requested_references = {
            (reference.provider_key, reference.provider_product_id)
            for reference in request.candidates
        }
        if not requested_references.issubset(context.discovered_references):
            context.clarification = _CLARIFICATION
            return json.dumps({
                "status": "clarification_required",
                "message": context.clarification,
            })

        try:
            result = ComparisonService(registry).compare(request)
        except UnknownProductReferenceError:
            context.clarification = _CLARIFICATION
            return json.dumps({
                "status": "clarification_required",
                "message": context.clarification,
            })
        except ComparisonProviderError:
            context.unavailable_message = (
                "Verified product information is temporarily unavailable. "
                "Please try again."
            )
            return json.dumps({
                "status": "unavailable",
                "message": context.unavailable_message,
            })
        except ComparisonError:
            context.clarification = _CLARIFICATION
            return json.dumps({
                "status": "clarification_required",
                "message": context.clarification,
            })

        context.comparison_result = build_comparison_api_response(result)
        return context.comparison_result.model_dump_json()

    return search_discovery_products, compare_products, get_selected_products


def _budget_was_explicitly_stated(
    budget: ComparisonBudget,
    user_messages: tuple[str, ...],
) -> bool:
    currency_pattern = re.compile(
        rf"(?<![A-Za-z]){re.escape(budget.currency)}(?![A-Za-z])",
        re.IGNORECASE,
    )
    amount_pattern = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
    for message in user_messages:
        for currency_match in currency_pattern.finditer(message):
            for amount_match in amount_pattern.finditer(message):
                if (
                    Decimal(amount_match.group()) == budget.amount
                    and abs(currency_match.start() - amount_match.end()) <= 24
                ):
                    return True
    return False


def _stated_storage(product_name: str) -> str | None:
    match = _STATED_STORAGE_PATTERN.search(product_name)
    if match is None:
        return None
    amount = format(Decimal(match.group(1)).normalize(), "f")
    return f"{amount} {match.group(2).upper()}"


def _format_value_guidance(
    result: ComparisonAPIResponse,
) -> list[str]:
    candidates_by_currency: dict[
        tuple[str, bool],
        list[tuple[ComparisonCandidateResponse, int]],
    ] = {}
    for candidate in result.candidates:
        offer = candidate.offer
        if offer.price_minor is None or offer.currency is None:
            continue
        is_demo = candidate.reference.provider_key == "demo_catalog"
        candidates_by_currency.setdefault(
            (offer.currency, is_demo), []
        ).append((candidate, offer.price_minor))

    guidance = []
    for (currency, is_demo), entries in sorted(candidates_by_currency.items()):
        if len(entries) < 2:
            continue
        prices = [price for _, price in entries]
        minimum = min(prices)
        maximum = max(prices)
        if minimum == maximum:
            guidance.append(
                f"- {currency}: the compared offers have the same "
                f"{'sample ' if is_demo else ''}listed price; there is no "
                "price-based advantage."
            )
            continue

        lowest_candidates = [
            candidate
            for candidate, price in entries
            if price == minimum
        ]
        exponent = currency_minor_unit_exponent(currency)
        difference = format_major_amount(
            Decimal(maximum - minimum) / Decimal(10**exponent),
            currency,
        )
        price_kind = "sample price" if is_demo else "listed price"
        if len(lowest_candidates) > 1:
            names = ", ".join(
                candidate.offer.product_name for candidate in lowest_candidates
            )
            guidance.append(
                f"- {currency}: {names} tie for the lowest {price_kind}; "
                "there is no unique price-based choice."
            )
            continue

        lowest = lowest_candidates[0]
        storage_claims = [
            _stated_storage(candidate.offer.product_name)
            for candidate, _ in entries
        ]
        categories = [
            candidate.evidence.get("category")
            for candidate, _ in entries
        ]
        same_category = (
            all(
                evidence is not None
                and evidence.status == "known"
                for evidence in categories
            )
            and len({
                str(evidence.value).casefold()
                for evidence in categories
                if evidence is not None
            }) == 1
        )
        same_title_storage = (
            storage_claims[0] is not None
            and len(set(storage_claims)) == 1
        )
        if same_category and same_title_storage:
            storage = storage_claims[0]
            guidance.append(
                f"- {currency}: both product names state {storage}; that "
                "title claim is not independently verified. "
                f"{lowest.offer.product_name} has the lower {price_kind} "
                f"by {difference}. If the stated storage is accurate, this "
                "is better listed-price value for the same stated storage, "
                "not proof of better overall quality."
            )
        else:
            guidance.append(
                f"- {currency}: {lowest.offer.product_name} has the lower "
                f"{price_kind} by {difference}. If minimizing upfront cost "
                "is your priority, it is the more affordable option. The "
                "available evidence does not establish better product value."
            )
    return guidance


def format_comparison_chat_reply(
    result: ComparisonAPIResponse,
) -> str:
    has_demo_product = any(
        candidate.reference.provider_key == "demo_catalog"
        for candidate in result.candidates
    )
    candidates_by_reference = {
        (
            candidate.reference.provider_key,
            candidate.reference.provider_product_id,
        ): candidate
        for candidate in result.candidates
    }
    lines = [
        "Comparison based on available provider information:"
        if has_demo_product
        else "Comparison based on verified catalog information:"
    ]
    if has_demo_product:
        lines.append(
            "Demo Catalog entries are sample data - not live marketplace prices "
            "or verified availability."
        )

    for candidate in result.candidates:
        offer = candidate.offer
        reference = (
            candidate.reference.provider_key,
            candidate.reference.provider_product_id,
        )
        lines.append(
            f"- {offer.product_name} "
            f"(reference: {reference[0]}/{reference[1]})"
        )
        is_demo_product = reference[0] == "demo_catalog"
        if offer.price_minor is None or offer.currency is None:
            lines.append("  Price: unknown (verified price or currency unavailable).")
        else:
            exponent = currency_minor_unit_exponent(offer.currency)
            lines.append(
                ("  Sample price: " if is_demo_product else "  Price: ")
                + format_major_amount(
                    Decimal(offer.price_minor) / Decimal(10**exponent),
                    offer.currency,
                )
            )

        known_facts = []
        for attribute in ("brand", "model", "category", "is_organic", "availability"):
            evidence = candidate.evidence.get(attribute)
            if evidence is not None and evidence.status == "known":
                value = evidence.value
                if isinstance(value, bool):
                    value = "yes" if value else "no"
                known_facts.append(f"{attribute.replace('_', ' ')}: {value}")
        rating = candidate.evidence.get("average_rating")
        if rating is not None and rating.status == "known":
            reviews = candidate.evidence.get("review_count")
            count = (
                f" ({reviews.value} reviews)"
                if reviews is not None and reviews.status == "known"
                else ""
            )
            known_facts.append(f"rating: {rating.value}{count}")
        if known_facts:
            lines.append(
                ("  Sample fields: " if is_demo_product else "  Verified facts: ")
                + "; ".join(known_facts)
                + "."
            )
        if rating is not None and rating.status == "unknown":
            rating_reason = (
                "no reviews"
                if rating.reason == "no_reviews"
                else "rating evidence unavailable"
            )
            lines.append(f"  Rating: unknown ({rating_reason}).")

        unknown_facts = [
            attribute.replace("_", " ")
            for attribute in candidate.unknown_attributes
            if attribute in {
                "brand",
                "model",
                "category",
                "description",
                "is_organic",
                "availability",
                "specifications",
                "price",
            }
        ]
        if unknown_facts:
            lines.append("  Unknown: " + ", ".join(unknown_facts) + ".")
        stated_storage = _stated_storage(offer.product_name)
        if stated_storage is not None:
            lines.append(
                f"  Product name states {stated_storage}; this is not an "
                "independently verified specification."
            )
        if _FIFTH_GENERATION_PATTERN.search(offer.product_name):
            lines.append(
                '  The "5G" wording in the product name is not independently '
                "verified as a specification."
            )

        evaluations = [
            *candidate.must_have_evaluations,
            *([candidate.budget_evaluation] if candidate.budget_evaluation else []),
        ]
        for evaluation in evaluations:
            expected = json.dumps(evaluation.expected_value, ensure_ascii=False)
            detail = f"{evaluation.attribute} = {expected}: {evaluation.status}"
            if evaluation.reason == "currency_mismatch":
                detail += " (currencies differ; budget check is unknown)"
            elif evaluation.reason == "no_reviews":
                detail += " (no reviews are available)"
            elif evaluation.reason == "attribute_not_supported":
                detail += " (this attribute is not supported)"
            elif evaluation.reason == "provider_did_not_supply":
                detail += " (the provider did not supply this evidence)"
            lines.append("  Requirement: " + detail + ".")

    if result.price_summaries:
        lines.append("Currency-separated price ranges:")
        for summary in result.price_summaries:
            exponent = currency_minor_unit_exponent(summary.currency)
            minimum = format_major_amount(
                Decimal(summary.minimum_price_minor) / Decimal(10**exponent),
                summary.currency,
            )
            maximum = format_major_amount(
                Decimal(summary.maximum_price_minor) / Decimal(10**exponent),
                summary.currency,
            )
            lines.append(f"- {summary.currency}: {minimum} to {maximum}.")
    else:
        lines.append(
            "No numeric price summary is available for offers with verified "
            "price and currency."
        )

    value_guidance = _format_value_guidance(result)
    if value_guidance:
        lines.append(
            "Conditional value guidance (prices are not compared across currencies):"
        )
        lines.extend(value_guidance)

    if any(
        "specifications" in candidate.unknown_attributes
        for candidate in result.candidates
    ):
        categories = [
            candidate.evidence.get("category")
            for candidate in result.candidates
        ]
        is_smartphone_comparison = any(
            evidence is not None
            and evidence.status == "known"
            and "smartphone" in str(evidence.value).casefold()
            for evidence in categories
        )
        if is_smartphone_comparison:
            lines.append(
                "Important missing smartphone details: camera, processor, RAM, "
                "battery, display, and software specifications were not supplied."
            )
        else:
            lines.append(
                "Important product specifications were not supplied, so "
                "feature-level value cannot be verified."
            )

    recommendation = result.recommendation
    if recommendation.status == "recommended":
        if recommendation.candidate is None:
            lines.append(
                "Deterministic result: the recommended reference could not be "
                "matched to a resolved candidate, so no recommendation is shown."
            )
            lines.extend(result.limitations)
            return "\n".join(lines)
        selected = candidates_by_reference.get((
            recommendation.candidate.provider_key,
            recommendation.candidate.provider_product_id,
        ))
        if selected is not None:
            basis = ", ".join(recommendation.basis) or "verified requirements"
            lines.append(
                "Best for the supplied requirements (based on available "
                "provider evidence): "
                f"{selected.offer.product_name} is the only candidate that met "
                f"the supplied requirements ({basis})."
            )
        else:
            lines.append(
                "Deterministic result: the recommended reference could not be "
                "matched to a resolved candidate, so no recommendation is shown."
            )
    elif recommendation.status == "insufficient_evidence":
        lines.append(
            "Best overall: insufficient verified evidence to recommend one "
            "product. Use the conditional price guidance above only for the "
            "stated affordability trade-off."
        )
    else:
        lines.append(
            "Best overall: the available verified information is not sufficient "
            "to establish an overall winner. The price guidance above is conditional "
            "and does not establish better quality or full-feature value."
        )

    lines.extend(result.limitations)
    return "\n".join(lines)
