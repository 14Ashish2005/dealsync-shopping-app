import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from backend.comparison.models import (
    ComparisonBudget,
    ComparisonRequest,
    MustHaveCriterion,
    ProductReference,
)
from backend.product_attributes import ProductAttribute
from backend.comparison.service import (
    ComparisonError,
    ComparisonService,
    UnknownProductReferenceError,
)
from backend.providers.base import InvalidProductDataError
from backend.providers.local_catalog import LocalCatalogAdapter
from backend.providers.registry import ProviderRegistry


class ComparisonServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "comparison.sqlite"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    currency TEXT,
                    description TEXT,
                    is_organic INTEGER
                );
                CREATE TABLE reviews (
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL,
                    rating REAL NOT NULL
                );
                """
            )
            connection.executemany(
                """
                INSERT INTO products
                    (id, name, category, price, currency, description, is_organic)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        1,
                        "Organic Honey",
                        "honey",
                        14.99,
                        "USD",
                        "Raw honey from local farms",
                        1,
                    ),
                    (2, "Wildflower Honey", "honey", 12.50, "INR", None, 0),
                    (3, "Unverified Price", "honey", 8.00, None, None, None),
                    (4, "No Reviews", None, 20.00, "USD", "Jarred honey", 0),
                ],
            )
            connection.executemany(
                "INSERT INTO reviews (product_id, rating) VALUES (?, ?)",
                [(1, 4.0), (1, 5.0)],
            )
            connection.commit()

        self.adapter = LocalCatalogAdapter(self.database_path)
        self.registry = ProviderRegistry([self.adapter])
        self.service = ComparisonService(self.registry)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _request(
        self,
        ids=("1", "2"),
        *,
        budget=None,
        must_have=(),
    ):
        return ComparisonRequest(
            candidates=tuple(
                ProductReference(
                    provider_key=self.adapter.provider_key,
                    provider_product_id=product_id,
                )
                for product_id in ids
            ),
            budget=budget,
            must_have=must_have,
        )

    def test_resolves_products_from_exact_server_side_references(self):
        result = self.service.compare(self._request())

        self.assertEqual(
            [item.candidate.offer.product_name for item in result.assessments],
            ["Organic Honey", "Wildflower Honey"],
        )
        self.assertEqual(
            result.assessments[0].candidate.offer.provider_product_id, "1"
        )
        self.assertEqual(
            result.assessments[0].candidate.evidence["average_rating"].value, 4.5
        )

    def test_unknown_product_reference_is_rejected(self):
        with self.assertRaises(UnknownProductReferenceError):
            self.service.compare(self._request(ids=("1", "999")))

    def test_duplicate_and_oversized_candidate_lists_are_rejected(self):
        reference = ProductReference(
            provider_key=self.adapter.provider_key,
            provider_product_id="1",
        )
        with self.assertRaises(ValidationError):
            ComparisonRequest(candidates=(reference, reference))
        with self.assertRaises(ValidationError):
            ComparisonRequest(
                candidates=tuple(
                    ProductReference(
                        provider_key=self.adapter.provider_key,
                        provider_product_id=str(product_id),
                    )
                    for product_id in range(1, 7)
                )
            )

    def test_missing_attributes_and_specifications_remain_unknown(self):
        result = self.service.compare(self._request(ids=("2", "4")))
        evidence = result.assessments[0].candidate.evidence

        self.assertEqual(evidence["description"].status, "unknown")
        self.assertEqual(evidence["brand"].status, "unknown")
        self.assertEqual(evidence["specifications"].status, "unknown")
        self.assertEqual(evidence["price"].status, "known")
        self.assertEqual(evidence["storage_capacity"].status, "unknown")
        attributes = {
            item.name: item
            for item in result.assessments[0].candidate.offer.product_attributes or ()
        }
        self.assertIsNone(attributes["storage_capacity"].value)
        self.assertEqual(attributes["storage_capacity"].provenance, "unknown")

    def test_comparison_exposes_provenance_without_treating_title_as_verified(self):
        resolved = self.adapter.resolve_products(("1", "2"))
        resolved_by_id = {
            item.offer.provider_product_id: item
            for item in resolved
        }
        resolved_by_id["1"] = replace(
            resolved_by_id["1"],
            product_attributes=(
                ProductAttribute(
                    "storage_capacity",
                    128,
                    "verified_provider",
                    unit="GB",
                ),
            ),
        )
        resolved_by_id["2"] = replace(
            resolved_by_id["2"],
            product_attributes=(
                ProductAttribute(
                    "storage_capacity",
                    64,
                    "local_catalog",
                    extraction_method="product_title",
                    unit="GB",
                ),
            ),
        )
        criterion = MustHaveCriterion(
            attribute="storage_capacity",
            expected_value="128 GB",
        )

        with patch.object(
            self.adapter,
            "resolve_products",
            side_effect=lambda ids: tuple(resolved_by_id[item] for item in ids),
        ):
            result = self.service.compare(
                self._request(ids=("1", "2"), must_have=(criterion,))
            )

        first = result.assessments[0]
        second = result.assessments[1]
        self.assertEqual(first.candidate.evidence["storage_capacity"].status, "known")
        self.assertEqual(
            first.candidate.evidence["storage_capacity"].source,
            "verified_provider",
        )
        self.assertEqual(first.evaluations[0].status, "met")
        self.assertEqual(second.candidate.evidence["storage_capacity"].status, "unknown")
        self.assertEqual(
            second.candidate.evidence["storage_capacity"].reason,
            "title_extracted_not_verified",
        )
        self.assertEqual(second.evaluations[0].status, "unknown")
        self.assertEqual(result.recommendation.status, "insufficient_evidence")

    def test_zero_review_count_is_not_a_zero_star_rating(self):
        result = self.service.compare(self._request(ids=("4", "1")))
        evidence = result.assessments[0].candidate.evidence

        self.assertEqual(evidence["review_count"].value, 0)
        self.assertEqual(evidence["average_rating"].status, "unknown")
        self.assertIsNone(evidence["average_rating"].value)
        self.assertEqual(evidence["average_rating"].reason, "no_reviews")

    def test_prices_are_summarized_separately_by_currency(self):
        result = self.service.compare(self._request(ids=("1", "2")))

        self.assertEqual(
            {summary.currency for summary in result.price_summaries},
            {"INR", "USD"},
        )
        self.assertEqual(
            {summary.offer_count for summary in result.price_summaries}, {1}
        )

    def test_price_summary_preserves_candidate_provenance(self):
        result = self.service.compare(self._request(ids=("1", "4")))
        summary = result.price_summaries[0]

        self.assertEqual(summary.currency, "USD")
        self.assertEqual(summary.minimum_price_minor, 1499)
        self.assertEqual(summary.maximum_price_minor, 2000)
        self.assertEqual(
            [
                reference.provider_product_id
                for reference in summary.minimum_price_references
            ],
            ["1"],
        )
        self.assertEqual(
            [
                reference.provider_product_id
                for reference in summary.maximum_price_references
            ],
            ["4"],
        )

    def test_budget_does_not_compare_different_currencies(self):
        budget = ComparisonBudget(amount=Decimal("15.00"), currency="USD")
        result = self.service.compare(
            self._request(ids=("1", "2"), budget=budget)
        )
        evaluations = [
            assessment.evaluations[0] for assessment in result.assessments
        ]

        self.assertEqual(evaluations[0].status, "met")
        self.assertEqual(evaluations[1].status, "unknown")
        self.assertEqual(evaluations[1].reason, "currency_mismatch")

    def test_budget_does_not_treat_unknown_currency_as_a_match(self):
        budget = ComparisonBudget(amount=Decimal("15.00"), currency="USD")
        result = self.service.compare(
            self._request(ids=("1", "3"), budget=budget)
        )

        self.assertEqual(result.assessments[0].evaluations[0].status, "met")
        self.assertEqual(result.assessments[1].evaluations[0].status, "unknown")
        self.assertEqual(
            result.recommendation.status, "insufficient_evidence"
        )

    def test_unknown_currency_is_excluded_from_numeric_price_summary(self):
        result = self.service.compare(self._request(ids=("1", "3")))

        self.assertEqual(len(result.price_summaries), 1)
        self.assertEqual(result.price_summaries[0].currency, "USD")
        self.assertIsNone(result.assessments[1].candidate.offer.price_minor)
        self.assertIsNone(result.assessments[1].candidate.offer.currency)

    def test_legacy_schema_without_currency_keeps_prices_unknown(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    description TEXT,
                    is_organic INTEGER
                );
                INSERT INTO products (id, name, price)
                    VALUES (1, 'Legacy item one', 10.00);
                INSERT INTO products (id, name, price)
                    VALUES (2, 'Legacy item two', 20.00);
                """
            )
            connection.commit()

        legacy_adapter = LocalCatalogAdapter(legacy_path)
        legacy_service = ComparisonService(ProviderRegistry([legacy_adapter]))
        request = ComparisonRequest(
            candidates=(
                ProductReference(
                    provider_key=legacy_adapter.provider_key,
                    provider_product_id="1",
                ),
                ProductReference(
                    provider_key=legacy_adapter.provider_key,
                    provider_product_id="2",
                ),
            )
        )
        result = legacy_service.compare(request)

        self.assertEqual(result.price_summaries, ())
        self.assertTrue(
            all(
                assessment.candidate.offer.price_minor is None
                and assessment.candidate.offer.currency is None
                for assessment in result.assessments
            )
        )

    def test_budget_requires_explicit_supported_currency_and_exact_minor_units(self):
        with self.assertRaises(ValidationError):
            ComparisonBudget.model_validate({"amount": "10.00"})
        with self.assertRaises(ValidationError):
            ComparisonBudget(amount=Decimal("10.00"), currency="XYZ")
        with self.assertRaises(ValidationError):
            ComparisonBudget(amount=Decimal("10.01"), currency="JPY")

    def test_must_have_criteria_are_met_not_met_or_unknown(self):
        criteria = (
            MustHaveCriterion(attribute="is_organic", expected_value=True),
            MustHaveCriterion(attribute="category", expected_value="honey"),
            MustHaveCriterion(attribute="water_resistant", expected_value="yes"),
        )
        result = self.service.compare(
            self._request(ids=("1", "2"), must_have=criteria)
        )

        first_statuses = [
            item.status for item in result.assessments[0].evaluations
        ]
        second_statuses = [
            item.status for item in result.assessments[1].evaluations
        ]
        self.assertEqual(first_statuses, ["met", "met", "unknown"])
        self.assertEqual(second_statuses, ["not_met", "met", "unknown"])

    def test_unsupported_attributes_are_unknown_not_miscompared(self):
        criterion = MustHaveCriterion(attribute="price", expected_value="USD")
        result = self.service.compare(
            self._request(ids=("1", "2"), must_have=(criterion,))
        )

        self.assertEqual(result.assessments[0].evaluations[0].status, "unknown")

    def test_minimum_rating_requires_review_evidence(self):
        criterion = MustHaveCriterion(
            attribute="minimum_rating", expected_value=Decimal("4.0")
        )
        result = self.service.compare(
            self._request(ids=("1", "4"), must_have=(criterion,))
        )

        self.assertEqual(result.assessments[0].evaluations[0].status, "met")
        self.assertEqual(result.assessments[1].evaluations[0].status, "unknown")

    def test_unique_candidate_meeting_requirements_is_recommended(self):
        criterion = MustHaveCriterion(attribute="is_organic", expected_value=True)
        result = self.service.compare(
            self._request(ids=("1", "2"), must_have=(criterion,))
        )

        self.assertEqual(result.recommendation.status, "recommended")
        recommendation = result.recommendation.candidate
        if recommendation is None:
            self.fail("A unique candidate should have been recommended.")
        self.assertEqual(recommendation.provider_product_id, "1")

    def test_no_clear_winner_when_candidates_equally_meet_requirements(self):
        criterion = MustHaveCriterion(attribute="category", expected_value="honey")
        result = self.service.compare(
            self._request(ids=("1", "2"), must_have=(criterion,))
        )

        self.assertEqual(result.recommendation.status, "no_clear_winner")
        self.assertIsNone(result.recommendation.candidate)

    def test_no_requirements_produces_no_clear_winner(self):
        result = self.service.compare(self._request())

        self.assertEqual(result.recommendation.status, "no_clear_winner")
        self.assertIsNone(result.recommendation.candidate)

    def test_client_cannot_supply_product_facts(self):
        with self.assertRaises(ValidationError):
            ProductReference.model_validate(
                {
                    "provider_key": self.adapter.provider_key,
                    "provider_product_id": "1",
                    "price_minor": 1,
                    "specifications": {"capacity": "fabricated"},
                }
            )
        with self.assertRaises(ComparisonError):
            self.service.compare(
                {
                    "candidates": [
                        {
                            "provider_key": self.adapter.provider_key,
                            "provider_product_id": "1",
                            "description": "client supplied",
                        },
                        {
                            "provider_key": self.adapter.provider_key,
                            "provider_product_id": "2",
                        },
                    ],
                    "client_price": 0,
                }
            )

    def test_comparison_is_read_only(self):
        before = hashlib.sha256(self.database_path.read_bytes()).digest()

        self.service.compare(self._request(ids=("1", "2")))

        after = hashlib.sha256(self.database_path.read_bytes()).digest()
        self.assertEqual(after, before)

    def test_local_reference_must_fit_sqlite_integer_identity(self):
        with self.assertRaises(InvalidProductDataError):
            self.adapter.resolve_products(("999999999999999999999999999999",))

    def test_criteria_and_budget_inputs_are_bounded(self):
        with self.assertRaises(ValidationError):
            MustHaveCriterion(
                attribute="description_contains",
                expected_value="x" * 121,
            )
        with self.assertRaises(ValidationError):
            ComparisonRequest(
                candidates=(
                    ProductReference(
                        provider_key=self.adapter.provider_key,
                        provider_product_id="1",
                    ),
                    ProductReference(
                        provider_key=self.adapter.provider_key,
                        provider_product_id="2",
                    ),
                ),
                must_have=tuple(
                    MustHaveCriterion(attribute="category", expected_value="honey")
                    for _ in range(9)
                ),
            )


if __name__ == "__main__":
    unittest.main()
