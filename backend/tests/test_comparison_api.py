import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.providers import (
    LocalCatalogAdapter,
    NormalizedOffer,
    ProviderAdapter,
    ProviderError,
    ProviderRegistry,
)
from backend.providers.base import PriceInput, ResolvedProviderProduct
from backend.tests._support import (
    import_main_with_temporary_auth_db,
    prepare_test_schemas,
)


class FailingResolverProvider(ProviderAdapter):
    @property
    def provider_key(self) -> str:
        return "failing_provider"

    @property
    def display_name(self) -> str:
        return "Failing provider"

    def search(
        self,
        query: str = "",
        *,
        max_price: PriceInput | None = None,
        currency: str | None = None,
        is_organic: bool | None = None,
    ) -> list[NormalizedOffer]:
        _ = query, max_price, currency, is_organic
        return []

    def normalize_offer(
        self,
        product: Mapping[str, object],
    ) -> NormalizedOffer:
        _ = product
        raise NotImplementedError

    def resolve_products(
        self,
        provider_product_ids: Sequence[str],
    ) -> tuple[ResolvedProviderProduct, ...]:
        _ = provider_product_ids
        raise ProviderError(self.provider_key, "private SQL credential marker")


class ComparisonApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = import_main_with_temporary_auth_db()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "store.sqlite"
        self.auth_path = Path(self.temp_dir.name) / "auth.sqlite"
        self._create_database()

        self.main = self.__class__.main
        self.original_store_path = self.main.STORE_DB_PATH
        self.original_auth_path = self.main.AUTH_DB_PATH
        self.original_registry = self.main.PROVIDER_REGISTRY
        setattr(self.main, "STORE_DB_PATH", self.store_path)
        setattr(self.main, "AUTH_DB_PATH", self.auth_path)
        registry = ProviderRegistry([LocalCatalogAdapter(self.store_path)])
        setattr(self.main, "PROVIDER_REGISTRY", registry)
        self.registry = registry
        self.main.app.dependency_overrides[
            self.main.get_provider_registry
        ] = lambda: self.registry
        prepare_test_schemas(self.store_path, self.auth_path)

        self.client = TestClient(self.main.app, raise_server_exceptions=False)
        with patch.object(
            self.main,
            "create_vision_analyzer",
            side_effect=ValueError("vision is not needed in this test"),
        ):
            self.client.__enter__()
        self.session_token = self.main.create_session(
            "comparison-test-owner",
            self.main.app.state.database_engines.authentication,
        )
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            self.session_token,
        )

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.main.app.dependency_overrides.pop(
            self.main.get_provider_registry,
            None,
        )
        setattr(self.main, "STORE_DB_PATH", self.original_store_path)
        setattr(self.main, "AUTH_DB_PATH", self.original_auth_path)
        setattr(self.main, "PROVIDER_REGISTRY", self.original_registry)
        self.temp_dir.cleanup()

    def _create_database(self) -> None:
        with closing(sqlite3.connect(self.store_path)) as connection:
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
                    (1, "Organic Honey", "honey", 10.00, "USD", "Raw honey", 1),
                    (2, "Flower Honey", "honey", 12.00, "INR", None, 0),
                    (3, "Legacy Honey", "honey", 5.00, None, None, None),
                ],
            )
            connection.execute(
                "INSERT INTO reviews (product_id, rating) VALUES (?, ?)",
                (1, 4.5),
            )
            connection.commit()

    def _reference(self, product_id: str) -> dict[str, str]:
        return {
            "provider_key": "local_catalog",
            "provider_product_id": product_id,
        }

    def _payload(
        self,
        product_ids: tuple[str, ...] = ("1", "2"),
        **fields: object,
    ) -> dict[str, object]:
        return {
            "candidates": [self._reference(product_id) for product_id in product_ids],
            **fields,
        }

    def _post(
        self,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ):
        request_headers = {"Origin": "http://localhost:5174"}
        if headers:
            request_headers.update(headers)
        return self.client.post(
            "/api/offers/compare",
            json=payload,
            headers=request_headers,
        )

    def test_successful_comparison_returns_typed_evidence_and_currency_groups(self):
        response = self._post(
            self._payload(
                must_have=[
                    {"attribute": "is_organic", "expected_value": True}
                ]
            )
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["candidates"]), 2)
        self.assertEqual(
            body["candidates"][0]["offer"]["product_name"], "Organic Honey"
        )
        self.assertEqual(
            body["candidates"][0]["evidence"]["average_rating"]["value"], 4.5
        )
        self.assertEqual(
            body["candidates"][0]["must_have_evaluations"][0]["status"], "met"
        )
        self.assertEqual(
            body["candidates"][1]["must_have_evaluations"][0]["status"], "not_met"
        )
        self.assertEqual(body["recommendation"]["status"], "recommended")
        self.assertEqual(
            {summary["currency"] for summary in body["price_summaries"]},
            {"USD", "INR"},
        )
        self.assertIn("reasons", body)
        self.assertIn("limitations", body)

    def test_authentication_and_origin_protection(self):
        self.client.cookies.clear()
        unauthenticated = self._post(self._payload())
        self.assertEqual(unauthenticated.status_code, 401)

        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            self.session_token,
        )
        forbidden = self._post(
            self._payload(),
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_candidate_count_and_duplicate_references_are_rejected(self):
        too_few = self._post(self._payload(("1",)))
        too_many = self._post(
            self._payload(("1", "2", "3", "4", "5", "6"))
        )
        duplicate = self._post(self._payload(("1", "1")))

        self.assertEqual(too_few.status_code, 422)
        self.assertEqual(too_many.status_code, 422)
        self.assertEqual(duplicate.status_code, 422)

    def test_unknown_provider_and_product_references_are_rejected(self):
        unknown_provider = self._post(
            {
                "candidates": [
                    {
                        "provider_key": "not_registered",
                        "provider_product_id": "1",
                    },
                    {
                        "provider_key": "not_registered",
                        "provider_product_id": "2",
                    },
                ]
            }
        )
        unknown_product = self._post(self._payload(("1", "999")))

        self.assertEqual(unknown_provider.status_code, 404)
        self.assertEqual(unknown_product.status_code, 404)

    def test_client_supplied_product_evidence_and_preferences_are_rejected(self):
        spoofed = self._payload()
        spoofed["candidates"] = [
            {
                **self._reference("1"),
                "price_minor": 1,
                "description": "client supplied",
                "brand": "spoofed",
                "availability": "in_stock",
                "specifications": {"weight": "1 kg"},
                "rating": 5,
            },
            self._reference("2"),
        ]
        spoofed["preferred_criteria"] = [
            {"attribute": "category", "expected_value": "honey"}
        ]
        response = self._post(spoofed)

        self.assertEqual(response.status_code, 422)

    def test_missing_product_evidence_is_explicitly_unknown(self):
        response = self._post(self._payload(("1", "3")))

        self.assertEqual(response.status_code, 200)
        legacy = response.json()["candidates"][1]
        self.assertEqual(legacy["evidence"]["price"]["status"], "unknown")
        self.assertEqual(legacy["evidence"]["average_rating"]["status"], "unknown")
        self.assertIn("brand", legacy["unknown_attributes"])
        self.assertIn("specifications", legacy["unknown_attributes"])
        self.assertIn("product_attributes", legacy["offer"])
        structured_attributes = {
            attribute["name"]: attribute
            for attribute in legacy["offer"]["product_attributes"]
        }
        self.assertEqual(
            structured_attributes["storage_capacity"]["provenance"],
            "unknown",
        )

    def test_budget_currency_mismatch_is_unknown_and_summaries_stay_separate(self):
        response = self._post(
            self._payload(
                budget={"amount": "15.00", "currency": "USD"}
            )
        )

        self.assertEqual(response.status_code, 200)
        candidates = response.json()["candidates"]
        self.assertEqual(candidates[0]["budget_evaluation"]["status"], "met")
        self.assertEqual(candidates[1]["budget_evaluation"]["status"], "unknown")
        self.assertEqual(
            candidates[1]["budget_evaluation"]["reason"], "currency_mismatch"
        )
        self.assertEqual(
            {item["currency"] for item in response.json()["price_summaries"]},
            {"USD", "INR"},
        )

    def test_invalid_budget_is_rejected(self):
        missing_currency = self._post(
            self._payload(budget={"amount": "10.00"})
        )
        negative_amount = self._post(
            self._payload(budget={"amount": "-1", "currency": "USD"})
        )
        unsupported_currency = self._post(
            self._payload(budget={"amount": "10.00", "currency": "XYZ"})
        )

        self.assertEqual(missing_currency.status_code, 422)
        self.assertEqual(negative_amount.status_code, 422)
        self.assertEqual(unsupported_currency.status_code, 422)

    def test_unsupported_criteria_remain_unknown(self):
        response = self._post(
            self._payload(
                must_have=[
                    {
                        "attribute": "water_resistant",
                        "expected_value": "yes",
                    }
                ]
            )
        )

        self.assertEqual(response.status_code, 200)
        evaluation = response.json()["candidates"][0]["must_have_evaluations"][0]
        self.assertEqual(evaluation["status"], "unknown")
        self.assertEqual(evaluation["reason"], "attribute_not_supported")

    def test_no_clear_winner_is_returned_when_candidates_tie(self):
        response = self._post(
            self._payload(
                must_have=[
                    {"attribute": "category", "expected_value": "honey"}
                ]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["recommendation"]["status"], "no_clear_winner"
        )

    def test_provider_errors_are_sanitized(self):
        self.registry.register(FailingResolverProvider())
        response = self._post(
            {
                "candidates": [
                    {
                        "provider_key": "failing_provider",
                        "provider_product_id": "1",
                    },
                    {
                        "provider_key": "failing_provider",
                        "provider_product_id": "2",
                    },
                ]
            }
        )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private SQL credential marker", response.text)
        self.assertNotIn("Traceback", response.text)
        self.assertEqual(
            response.json()["detail"],
            "Product comparison is temporarily unavailable.",
        )

    def test_request_body_size_is_bounded(self):
        body = json.dumps({"padding": "x" * self.main.MAX_COMPARISON_REQUEST_BODY_BYTES})
        response = self.client.post(
            "/api/offers/compare",
            content=body,
            headers={
                "Origin": "http://localhost:5174",
                "Content-Type": "application/json",
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.json()["detail"],
            "Comparison request body is too large.",
        )


if __name__ == "__main__":
    unittest.main()
