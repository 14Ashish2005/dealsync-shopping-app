import sqlite3
import threading
import tempfile
import time
import unittest
from collections.abc import Callable, Mapping
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.discovery import DiscoveryEngine
from backend.providers import (
    DemoProviderAdapter,
    LocalCatalogAdapter,
    NormalizedOffer,
    ProviderAdapter,
    ProviderError,
    ProviderRegistry,
)
from backend.providers.base import PriceInput
from backend.tests._support import (
    import_main_with_temporary_auth_db,
    prepare_test_schemas,
)


class StubProvider(ProviderAdapter):
    def __init__(
        self,
        key: str,
        search_function: Callable[..., list[NormalizedOffer]],
        *,
        supports_external_cart: bool = False,
    ) -> None:
        self._key = key
        self._search_function = search_function
        self._supports_external_cart = supports_external_cart

    @property
    def supports_external_cart(self) -> bool:
        return self._supports_external_cart

    @property
    def provider_key(self) -> str:
        return self._key

    @property
    def display_name(self) -> str:
        return self._key

    def search(
        self,
        query: str = "",
        *,
        max_price: PriceInput | None = None,
        min_price: PriceInput | None = None,
        currency: str | None = None,
        is_organic: bool | None = None,
        category: str | None = None,
    ) -> list[NormalizedOffer]:
        return self._search_function(
            query,
            max_price=max_price,
            currency=currency,
            is_organic=is_organic,
        )

    def normalize_offer(
        self,
        product: Mapping[str, object],
    ) -> NormalizedOffer:
        raise NotImplementedError


class OffersApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_temporary_auth_db()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "store.sqlite"
        self.auth_path = Path(self.temp_dir.name) / "auth.sqlite"
        self._create_database()

        self.original_store_path = self.main.STORE_DB_PATH
        self.original_auth_path = self.main.AUTH_DB_PATH
        self.original_registry = self.main.PROVIDER_REGISTRY
        setattr(self.main, "STORE_DB_PATH", self.database_path)
        setattr(self.main, "AUTH_DB_PATH", self.auth_path)

        self.registry = ProviderRegistry([LocalCatalogAdapter(self.database_path)])
        self.main.PROVIDER_REGISTRY = self.registry
        self.main.app.dependency_overrides[
            self.main.get_provider_registry
        ] = lambda: self.registry
        prepare_test_schemas(self.database_path, self.auth_path)

        self.vision_patch = patch.object(
            self.main,
            "create_vision_analyzer",
            side_effect=ValueError("vision is not used by this test"),
        )
        self.vision_patch.start()
        self.client = TestClient(self.main.app)
        self.client.__enter__()

    def tearDown(self):
        if self.client is not None:
            self.client.__exit__(None, None, None)
        self.vision_patch.stop()
        self.main.app.dependency_overrides.pop(
            self.main.get_provider_registry,
            None,
        )
        setattr(self.main, "STORE_DB_PATH", self.original_store_path)
        setattr(self.main, "AUTH_DB_PATH", self.original_auth_path)
        self.main.PROVIDER_REGISTRY = self.original_registry
        self.temp_dir.cleanup()

    def _create_database(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    description TEXT,
                    is_organic INTEGER DEFAULT 0,
                    currency TEXT
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        1,
                        "Organic Raw Honey",
                        "honey",
                        14.99,
                        "Unfiltered local honey",
                        1,
                        "INR",
                    ),
                    (
                        2,
                        "Wildflower Honey",
                        "honey",
                        12.99,
                        "Natural wildflower honey",
                        0,
                        "INR",
                    ),
                    (3, "Rolled Oats", "grains", 5.49, "Whole grain oats", 0, "INR"),
                    (4, "Mixed Nuts", "nuts", 3.00, "Mixed nuts", 0, None),
                ],
            )
            connection.commit()

    def test_provider_list_includes_registered_local_catalog(self):
        response = self.client.get("/api/providers")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "providers": [
                    {
                        "provider_key": "local_catalog",
                        "display_name": "Local catalog",
                        "supports_external_cart": False,
                    }
                ]
            },
        )

    def test_demo_discovery_is_not_persisted_or_eligible_for_external_cart(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript("""
                CREATE TABLE product_providers (
                    provider_key TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL
                );
                CREATE TABLE product_offers (
                    offer_id TEXT PRIMARY KEY,
                    provider_key TEXT NOT NULL,
                    provider_offer_key TEXT NOT NULL,
                    provider_product_id TEXT NOT NULL
                );
            """)
            connection.commit()

        demo_provider = DemoProviderAdapter()
        self.registry.register(demo_provider)

        provider_response = self.client.get("/api/providers")
        self.assertEqual(provider_response.status_code, 200)
        demo_listing = next(
            provider
            for provider in provider_response.json()["providers"]
            if provider["provider_key"] == "demo_catalog"
        )
        self.assertEqual(
            demo_listing["display_name"],
            "DealSync Demo Catalog (Sample Data)",
        )
        self.assertFalse(demo_listing["supports_external_cart"])

        response = self.client.get(
            "/api/offers/search",
            params={"provider_key": "demo_catalog"},
        )
        self.assertEqual(response.status_code, 200)
        offers = response.json()["offers"]
        self.assertGreaterEqual(len(offers), 4)
        self.assertTrue(
            all(
                offer["product_name"].startswith("[DEMO SAMPLE] ")
                for offer in offers
            )
        )
        self.assertTrue(
            all(
                isinstance(offer["price_minor"], int)
                and offer["price_minor"] > 0
                and offer["currency"] in {"INR", "USD", "EUR", "GBP", "CAD"}
                and offer["product_url"] is None
                and offer["availability"] == "unknown"
                for offer in offers
            )
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM product_providers"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM product_offers"
                ).fetchone()[0],
                0,
            )

        owner_id = self.main.create_session(
            "demo-discovery-owner",
            self.main.app.state.database_engines.authentication,
        )
        self.client.cookies.set(self.main.SESSION_COOKIE_NAME, owner_id)
        cart_response = self.client.post(
            "/api/cart/external/items",
            json={"offer_id": offers[0]["offer_id"], "quantity": 1},
            headers={"Origin": "http://localhost:5174"},
        )
        self.assertEqual(cart_response.status_code, 404)

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM product_providers"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM product_offers"
                ).fetchone()[0],
                0,
            )

    def test_provider_list_reports_explicit_external_cart_opt_in(self):
        eligible_provider = StubProvider(
            "eligible_test_provider",
            lambda *_args, **_kwargs: [],
            supports_external_cart=True,
        )
        self.registry = ProviderRegistry(
            [LocalCatalogAdapter(self.database_path), eligible_provider]
        )
        self.main.PROVIDER_REGISTRY = self.registry

        response = self.client.get("/api/providers")

        self.assertEqual(response.status_code, 200)
        providers = {
            provider["provider_key"]: provider
            for provider in response.json()["providers"]
        }
        self.assertFalse(providers["local_catalog"]["supports_external_cart"])
        self.assertTrue(
            providers["eligible_test_provider"]["supports_external_cart"]
        )

    def test_search_returns_normalized_local_catalog_offers(self):
        response = self.client.get("/api/offers/search")

        self.assertEqual(response.status_code, 200)
        offers = response.json()["offers"]
        self.assertEqual(len(offers), 4)
        honey = next(offer for offer in offers if offer["provider_product_id"] == "1")
        self.assertEqual(honey["provider_key"], "local_catalog")
        self.assertEqual(honey["product_name"], "Organic Raw Honey")
        self.assertEqual(honey["price_minor"], 1499)
        self.assertEqual(honey["currency"], "INR")
        unknown = next(
            offer for offer in offers if offer["provider_product_id"] == "4"
        )
        self.assertIsNone(unknown["price_minor"])
        self.assertIsNone(unknown["currency"])
        self.assertEqual(
            response.json()["providers"],
            [
                {
                    "provider_key": "local_catalog",
                    "status": "success",
                    "offer_count": 4,
                    "error_code": None,
                    "error_message": None,
                }
            ],
        )
        self.assertFalse(response.json()["partial"])

    def test_search_query_filters_products(self):
        response = self.client.get(
            "/api/offers/search",
            params={"query": "honey"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {offer["product_name"] for offer in response.json()["offers"]},
            {"Organic Raw Honey", "Wildflower Honey"},
        )
        first_offer = response.json()["offers"][0]
        self.assertIn("product_attributes", first_offer)
        self.assertEqual(
            next(
                attribute
                for attribute in first_offer["product_attributes"]
                if attribute["name"] == "category"
            )["provenance"],
            "local_catalog",
        )

    def test_smart_search_applies_explicit_inr_budget(self):
        response = self.client.post(
            "/api/search/smart",
            json={"query": "Find honey under ₹15"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["original_query"], "Find honey under ₹15")
        self.assertEqual(body["normalized_search_query"], "honey")
        self.assertEqual(body["budget_amount"], 15)
        self.assertEqual(body["budget_currency"], "INR")
        self.assertFalse(body["clarification_required"])
        self.assertEqual(
            {offer["product_name"] for offer in body["offers"]},
            {"Organic Raw Honey", "Wildflower Honey"},
        )
        self.assertTrue(
            all(
                offer["currency"] == "INR" and offer["price_minor"] <= 1500
                for offer in body["offers"]
            )
        )

    def test_smart_search_ranks_exact_name_without_price_advantage(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executemany(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (5, "Acme Phone Pro", "smartphones", 5, "", 0, "INR"),
                    (6, "Acme Phone", "smartphones", 1500, "", 0, "INR"),
                ],
            )
            connection.commit()

        response = self.client.post(
            "/api/search/smart",
            json={"query": "Find Acme Phone smartphones"},
        )

        self.assertEqual(response.status_code, 200)
        offers = response.json()["offers"]
        self.assertEqual(
            [offer["product_name"] for offer in offers],
            ["Acme Phone", "Acme Phone Pro"],
        )
        self.assertEqual(
            [offer["price_minor"] for offer in offers],
            [150000, 500],
        )

    def test_smart_search_matches_category_alias_independently_of_text(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (5, "Acme Mobile Device", "smart phone", 12000, "", 0, "INR"),
            )
            connection.commit()

        response = self.client.post(
            "/api/search/smart",
            json={"query": "Find smartphones"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["normalized_search_query"], "smartphones")
        self.assertEqual(body["product_category"], "Smartphones")
        self.assertEqual(
            [offer["provider_product_id"] for offer in body["offers"]],
            ["5"],
        )

    def test_smart_search_missing_budget_currency_requests_clarification(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine, "search", wraps=engine.search) as search:
            response = self.client.post(
                "/api/search/smart",
                json={"query": "laptops under 50000"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["clarification_required"])
        self.assertIsNone(body["budget_currency"])
        self.assertEqual(body["offers"], [])
        self.assertEqual(body["providers"], [])
        self.assertIn("currency", body["clarification_message"])
        search.assert_not_called()

    def test_smart_search_bare_dollar_requests_currency_clarification(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine, "search", wraps=engine.search) as search:
            response = self.client.post(
                "/api/search/smart",
                json={"query": "Find headphones under $100"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["clarification_required"])
        self.assertEqual(body["budget_amount"], 100)
        self.assertIsNone(body["budget_currency"])
        self.assertIn("Which currency", body["clarification_message"])
        self.assertEqual(body["offers"], [])
        search.assert_not_called()

    def test_smart_search_malformed_budget_does_not_invoke_provider_search(self):
        engine = self.main.app.state.discovery_engine
        malformed_queries = (
            "Find honey under 1.2.3 USD",
            "Find honey under 100abc USD",
            "Find honey under 10,00,000 INR",
            "Find honey under -100 INR",
        )

        for query in malformed_queries:
            with self.subTest(query=query):
                with patch.object(engine, "search", wraps=engine.search) as search:
                    response = self.client.post(
                        "/api/search/smart",
                        json={"query": query},
                    )

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertTrue(body["clarification_required"])
                self.assertEqual(body["offers"], [])
                self.assertEqual(body["providers"], [])
                search.assert_not_called()

    def test_smart_search_explicit_usd_uses_existing_maximum_filter(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine, "search", wraps=engine.search) as search:
            response = self.client.post(
                "/api/search/smart",
                json={"query": "Find headphones under USD 100"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["clarification_required"])
        search.assert_called_once_with(
            "headphones",
            min_price=None,
            max_price=Decimal("100"),
            currency="USD",
            category="Headphones",
        )

    def test_smart_search_applies_minimum_provider_filter(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine, "search", wraps=engine.search) as search:
            response = self.client.post(
                "/api/search/smart",
                json={"query": "Find honey above ₹10"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["minimum_budget_amount"], 10)
        self.assertEqual(body["budget_currency"], "INR")
        self.assertIsNone(body["budget_amount"])
        self.assertNotIn(
            "Minimum-price filtering is recognized but is not applied yet.",
            body["unsupported_requirements"],
        )
        search.assert_called_once_with(
            "honey",
            category=None,
            max_price=None,
            currency="INR",
            min_price=Decimal("10"),
        )

    def test_smart_search_range_applies_both_bounds(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine, "search", wraps=engine.search) as search:
            response = self.client.post(
                "/api/search/smart",
                json={
                    "query": "Find honey between ₹10 and ₹20",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["minimum_budget_amount"], 10)
        self.assertEqual(body["budget_amount"], 20)
        self.assertNotIn(
            "Minimum-price filtering is recognized but is not applied yet.",
            body["unsupported_requirements"],
        )
        search.assert_called_once_with(
            "honey",
            category=None,
            max_price=Decimal("20"),
            currency="INR",
            min_price=Decimal("10"),
        )

    def test_smart_search_contradictory_range_clarifies_without_searching(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine, "search", wraps=engine.search) as search:
            response = self.client.post(
                "/api/search/smart",
                json={
                    "query": "Find honey between ₹20 and ₹10",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["clarification_required"])
        self.assertEqual(body["minimum_budget_amount"], 20)
        self.assertEqual(body["budget_amount"], 10)
        self.assertIn("exceeds the maximum", body["clarification_message"])
        self.assertEqual(body["offers"], [])
        self.assertEqual(body["providers"], [])
        search.assert_not_called()

    def test_smart_search_reports_unsupported_preferences_without_filtering_by_them(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine, "search", wraps=engine.search) as search:
            response = self.client.post(
                "/api/search/smart",
                json={
                    "query": "Smartphone under ₹20,000 with good camera",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["normalized_search_query"], "smartphone")
        self.assertEqual(body["product_category"], "Smartphones")
        self.assertEqual(body["preferences"], ["camera quality"])
        self.assertEqual(
            body["unsupported_requirements"],
            ["camera quality"],
        )
        self.assertNotIn("verified_specifications", body)
        search.assert_called_once_with(
            "smartphone",
            min_price=None,
            max_price=Decimal("20000"),
            currency="INR",
            category="Smartphones",
        )

    def test_smart_search_without_budget_returns_matching_offers(self):
        response = self.client.post(
            "/api/search/smart",
            json={"query": "Find honey"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["budget_amount"])
        self.assertNotIn("minimum_budget_amount", body)
        self.assertIsNone(body["budget_currency"])
        self.assertFalse(body["clarification_required"])
        self.assertEqual(len(body["offers"]), 2)

    def test_smart_search_returns_success_for_empty_results(self):
        response = self.client.post(
            "/api/search/smart",
            json={"query": "unfindable product"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["offers"], [])
        self.assertEqual(
            response.json()["providers"],
            [
                {
                    "provider_key": "local_catalog",
                    "status": "success",
                    "offer_count": 0,
                    "error_code": None,
                    "error_message": None,
                }
            ],
        )

    def test_smart_search_preserves_partial_provider_failures(self):
        def fail(_query, **_kwargs):
            raise ProviderError("failing_provider", "sensitive internal details")

        self.registry.register(StubProvider("failing_provider", fail))
        response = self.client.post(
            "/api/search/smart",
            json={"query": "honey"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["partial"])
        self.assertEqual(len(body["offers"]), 2)
        self.assertEqual(
            {
                provider["provider_key"]: provider["status"]
                for provider in body["providers"]
            },
            {"local_catalog": "success", "failing_provider": "error"},
        )
        self.assertEqual(
            [offer["product_name"] for offer in body["offers"]],
            ["Organic Raw Honey", "Wildflower Honey"],
        )
        self.assertNotIn("sensitive", response.text)

    def test_smart_search_rejects_malformed_requests(self):
        invalid_requests = (
            {},
            {"query": 123},
            {"query": "   "},
            {"query": "honey", "provider_key": "local_catalog"},
        )
        for request_body in invalid_requests:
            with self.subTest(request_body=request_body):
                response = self.client.post(
                    "/api/search/smart",
                    json=request_body,
                )
                self.assertEqual(response.status_code, 422)

    def test_smart_search_preserves_demo_sample_offer_labels(self):
        self.registry.register(DemoProviderAdapter())
        response = self.client.post(
            "/api/search/smart",
            json={"query": "wireless headphones"},
        )

        self.assertEqual(response.status_code, 200)
        demo_offers = [
            offer
            for offer in response.json()["offers"]
            if offer["provider_key"] == "demo_catalog"
        ]
        self.assertTrue(demo_offers)
        self.assertTrue(
            all(
                offer["product_name"].startswith("[DEMO SAMPLE] ")
                and offer["availability"] == "unknown"
                and offer["product_url"] is None
                for offer in demo_offers
            )
        )

    def test_existing_offers_search_response_contract_is_unchanged(self):
        response = self.client.get(
            "/api/offers/search",
            params={"query": "honey"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body),
            {"offers", "providers", "groups", "price_summaries", "partial"},
        )
        self.assertEqual(
            {offer["product_name"] for offer in body["offers"]},
            {"Organic Raw Honey", "Wildflower Honey"},
        )
        self.assertEqual(
            [offer["product_name"] for offer in body["offers"]],
            ["Organic Raw Honey", "Wildflower Honey"],
        )

    def test_search_max_price_filters_in_inr(self):
        response = self.client.get(
            "/api/offers/search",
            params={"max_price": 10, "currency": "INR"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [offer["product_name"] for offer in response.json()["offers"]],
            ["Rolled Oats"],
        )

    def test_offer_search_applies_minimum_price_and_exact_category(self):
        response = self.client.get(
            "/api/offers/search",
            params={
                "min_price": "13.00",
                "max_price": "15.00",
                "currency": "INR",
                "category": "Honey",
            },
        )

        self.assertEqual(response.status_code, 200)
        offers = response.json()["offers"]
        self.assertEqual(
            [offer["provider_product_id"] for offer in offers],
            ["1"],
        )

    def test_invalid_offer_bounds_do_not_invoke_discovery(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine, "search", wraps=engine.search) as search:
            contradictory = self.client.get(
                "/api/offers/search",
                params={
                    "min_price": "20",
                    "max_price": "10",
                    "currency": "INR",
                },
            )
            malformed = self.client.get(
                "/api/offers/search",
                params={"min_price": "1.2.3", "currency": "INR"},
            )

        self.assertEqual(contradictory.status_code, 400)
        self.assertEqual(
            contradictory.json()["detail"],
            "min_price cannot exceed max_price.",
        )
        self.assertEqual(malformed.status_code, 422)
        search.assert_not_called()

    def test_unsupported_adapter_is_reported_without_searching_unfiltered(self):
        calls = []
        legacy = StubProvider(
            "legacy",
            lambda *_args, **_kwargs: calls.append(True) or [],
        )
        registry = ProviderRegistry([legacy])
        engine = DiscoveryEngine(registry)
        self.main.app.dependency_overrides[
            self.main.get_provider_registry
        ] = lambda: registry
        self.main.app.dependency_overrides[
            self.main.get_discovery_engine
        ] = lambda: engine
        try:
            response = self.client.get(
                "/api/offers/search",
                params={"category": "smartphones"},
            )
        finally:
            self.main.app.dependency_overrides.pop(
                self.main.get_discovery_engine,
                None,
            )
            engine.shutdown()

        self.assertEqual(response.status_code, 501)
        self.assertEqual(calls, [])
        self.assertEqual(
            response.json()["providers"][0]["error_code"],
            "unsupported_filter",
        )

    def test_search_max_price_requires_explicit_currency(self):
        response = self.client.get(
            "/api/offers/search",
            params={"max_price": 10},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "Specify a currency code to filter offers by price.",
        )

    def test_search_price_filter_never_relabels_unknown_or_other_currency(self):
        response = self.client.get(
            "/api/offers/search",
            params={"max_price": 10, "currency": "USD"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["offers"], [])

    def test_search_max_price_filters_usd_and_excludes_other_currencies(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (5, "USD Oats", "grains", 5.00, "USD-priced oats", 0, "USD"),
            )
            connection.commit()

        response = self.client.get(
            "/api/offers/search",
            params={"max_price": 10, "currency": "USD"},
        )

        self.assertEqual(response.status_code, 200)
        offers = response.json()["offers"]
        self.assertEqual(
            [offer["provider_product_id"] for offer in offers],
            ["5"],
        )
        self.assertEqual(offers[0]["product_name"], "USD Oats")
        self.assertEqual(offers[0]["price_minor"], 500)
        self.assertEqual(offers[0]["currency"], "USD")

    def test_invalid_offer_currency_is_rejected(self):
        response = self.client.get(
            "/api/offers/search",
            params={"currency": "XYZ"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "currency must be a supported ISO 4217 code.",
        )

    def test_search_organic_filter(self):
        response = self.client.get(
            "/api/offers/search",
            params={"is_organic": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [offer["product_name"] for offer in response.json()["offers"]],
            ["Organic Raw Honey"],
        )

    def test_unknown_provider_returns_not_found(self):
        response = self.client.get(
            "/api/offers/search",
            params={"provider_key": "unregistered_provider"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Provider not found.")

    def test_unavailable_catalog_returns_service_unavailable(self):
        local_provider = self.registry.get("local_catalog")
        local_provider.database_path = Path(self.temp_dir.name) / "missing.sqlite"

        response = self.client.get("/api/offers/search")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "Provider is temporarily unavailable.",
        )
        self.assertEqual(
            response.json()["providers"][0]["status"],
            "unavailable",
        )
        self.assertNotIn(str(self.temp_dir.name), response.text)

    def test_provider_search_errors_return_sanitized_bad_gateway(self):
        provider = self.registry.get("local_catalog")
        with patch.object(
            provider,
            "search",
            side_effect=ProviderError(
                provider.provider_key,
                "internal failure with sensitive detail",
            ),
        ):
            response = self.client.get("/api/offers/search")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Provider search failed.")
        self.assertEqual(response.json()["providers"][0]["status"], "error")
        self.assertNotIn("sensitive", response.text)

    def test_provider_failure_does_not_discard_successful_provider_offers(self):
        def fail(_query, **_kwargs):
            raise ProviderError("failing_provider", "sensitive internal details")

        self.registry.register(StubProvider("failing_provider", fail))

        response = self.client.get("/api/offers/search")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["offers"]), 4)
        self.assertTrue(response.json()["partial"])
        statuses = {
            provider["provider_key"]: provider["status"]
            for provider in response.json()["providers"]
        }
        self.assertEqual(
            statuses,
            {"local_catalog": "success", "failing_provider": "error"},
        )
        self.assertNotIn("sensitive", response.text)

    def test_provider_timeout_is_returned_with_partial_offers(self):
        started = threading.Event()
        release = threading.Event()

        def wait_for_release(_query, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return []

        self.registry.register(StubProvider("slow_provider", wait_for_release))
        engine = self.main.app.state.discovery_engine
        engine._provider_timeouts["slow_provider"] = 0.03
        try:
            response = self.client.get("/api/offers/search")

            self.assertTrue(started.is_set())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["offers"]), 4)
            self.assertTrue(response.json()["partial"])
            timeout_status = next(
                provider
                for provider in response.json()["providers"]
                if provider["provider_key"] == "slow_provider"
            )
            self.assertEqual(timeout_status["status"], "timeout")
            self.assertEqual(timeout_status["error_code"], "provider_timeout")
        finally:
            release.set()

    def test_provider_overload_is_returned_without_losing_status(self):
        engine = self.main.app.state.discovery_engine
        with patch.object(engine._admission, "acquire", return_value=False):
            response = self.client.get("/api/offers/search")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["providers"][0]["status"],
            "overloaded",
        )
        self.assertEqual(
            response.json()["providers"][0]["error_code"],
            "capacity_exceeded",
        )

    def test_not_started_provider_outcome_maps_to_service_unavailable(self):
        from backend.discovery.models import DiscoveryResult, ProviderOutcome

        engine = self.main.app.state.discovery_engine
        not_started = ProviderOutcome(
            provider_key="cancelled_provider",
            status="not_started",
            offer_count=0,
            error_code="task_cancelled",
            error_message="Provider search was cancelled before execution.",
        )
        no_success = DiscoveryResult(
            offers=(),
            providers=(not_started,),
            groups=(),
            price_summaries=(),
        )

        successful = engine.search()
        partial = DiscoveryResult(
            offers=successful.offers,
            providers=successful.providers + (not_started,),
            groups=successful.groups,
            price_summaries=successful.price_summaries,
        )

        with patch.object(engine, "search", side_effect=[no_success, partial]):
            unavailable_response = self.client.get("/api/offers/search")
            partial_response = self.client.get("/api/offers/search")

        self.assertEqual(unavailable_response.status_code, 503)
        self.assertEqual(
            unavailable_response.json()["providers"][0]["status"],
            "not_started",
        )
        self.assertEqual(
            unavailable_response.json()["providers"][0]["error_code"],
            "task_cancelled",
        )
        self.assertNotIn("sensitive", unavailable_response.text)

        self.assertEqual(partial_response.status_code, 200)
        self.assertEqual(
            len(partial_response.json()["offers"]),
            len(successful.offers),
        )
        self.assertTrue(partial_response.json()["partial"])
        self.assertEqual(
            next(
                provider["status"]
                for provider in partial_response.json()["providers"]
                if provider["provider_key"] == "cancelled_provider"
            ),
            "not_started",
        )
        self.assertNotIn("sensitive", partial_response.text)

    def test_requests_share_the_lifespan_discovery_engine(self):
        engine = self.main.app.state.discovery_engine

        first = self.client.get("/api/offers/search")
        second = self.client.get("/api/offers/search")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIs(self.main.app.state.discovery_engine, engine)

    def test_lifespan_shutdown_does_not_wait_for_a_stuck_provider(self):
        started = threading.Event()
        release = threading.Event()

        def wait_forever(_query, **_kwargs):
            started.set()
            release.wait(timeout=5)
            return []

        self.registry.register(StubProvider("stuck_provider", wait_forever))
        engine = self.main.app.state.discovery_engine
        engine._provider_timeouts["stuck_provider"] = 0.03
        try:
            response = self.client.get("/api/offers/search")

            self.assertTrue(started.is_set())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                next(
                    provider["status"]
                    for provider in response.json()["providers"]
                    if provider["provider_key"] == "stuck_provider"
                ),
                "timeout",
            )

            shutdown_started = time.monotonic()
            self.client.__exit__(None, None, None)
            self.client = None
            self.assertLess(time.monotonic() - shutdown_started, 0.5)
            self.assertEqual(
                engine.search(provider_key="stuck_provider")
                .providers[0]
                .status,
                "unavailable",
            )
        finally:
            release.set()

    def test_invalid_parameters_return_client_error(self):
        negative_price = self.client.get(
            "/api/offers/search",
            params={"max_price": -1},
        )
        invalid_boolean = self.client.get(
            "/api/offers/search",
            params={"is_organic": "not-a-boolean"},
        )

        self.assertEqual(negative_price.status_code, 400)
        self.assertEqual(invalid_boolean.status_code, 422)

    def test_legacy_product_price_filter_requires_explicit_currency(self):
        missing_currency = self.client.get(
            "/api/products",
            params={"max_price": 10},
        )
        invalid_currency = self.client.get(
            "/api/products",
            params={"max_price": 10, "currency": "XYZ"},
        )

        self.assertEqual(missing_currency.status_code, 400)
        self.assertEqual(invalid_currency.status_code, 400)

    def test_existing_product_and_category_responses_remain_compatible(self):
        products_response = self.client.get("/api/products")
        categories_response = self.client.get("/api/categories")

        self.assertEqual(products_response.status_code, 200)
        products = products_response.json()["products"]
        honey = next(product for product in products if product["id"] == 1)
        legacy = next(product for product in products if product["id"] == 4)
        self.assertEqual(honey["name"], "Organic Raw Honey")
        self.assertEqual(honey["price"], 14.99)
        self.assertEqual(honey["currency"], "INR")
        self.assertEqual(legacy["price"], 3.0)
        self.assertIsNone(legacy["currency"])
        self.assertIn("image_url", honey)
        self.assertIn("local_image_url", honey)
        self.assertIn("fallback_image", honey)

        self.assertEqual(categories_response.status_code, 200)
        self.assertEqual(
            categories_response.json(),
            {
                "categories": [
                    {"category": "grains", "product_count": 1},
                    {"category": "honey", "product_count": 2},
                    {"category": "nuts", "product_count": 1},
                ]
            },
        )

    def test_product_catalog_filters_use_the_selected_currency(self):
        response = self.client.get(
            "/api/products",
            params={
                "query": "honey",
                "max_price": 13,
                "currency": "INR",
            },
        )

        self.assertEqual(response.status_code, 200)
        products = response.json()["products"]
        self.assertEqual([product["id"] for product in products], [2])

        organic_response = self.client.get(
            "/api/products",
            params={"is_organic": "true"},
        )
        self.assertEqual(organic_response.status_code, 200)
        self.assertEqual(
            [product["id"] for product in organic_response.json()["products"]],
            [1],
        )

    def test_product_catalog_applies_minimum_price_and_category(self):
        response = self.client.get(
            "/api/products",
            params={
                "min_price": "13",
                "max_price": "15",
                "currency": "INR",
                "category": "HONEY",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [product["id"] for product in response.json()["products"]],
            [1],
        )

    def test_product_catalog_matches_category_alias_independently_of_text(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (5, "Acme Mobile Device", "smart phone", 12000, "", 0, "INR"),
            )
            connection.commit()

        response = self.client.get(
            "/api/products",
            params={"query": "smartphones", "category": "smartphones"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [product["id"] for product in response.json()["products"]],
            [5],
        )

    def test_contradictory_product_bounds_do_not_search_catalog(self):
        with patch.object(
            LocalCatalogAdapter,
            "search_catalog_products",
            autospec=True,
        ) as search_catalog:
            response = self.client.get(
                "/api/products",
                params={
                    "min_price": "20",
                    "max_price": "10",
                    "currency": "INR",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "min_price cannot exceed max_price.",
        )
        search_catalog.assert_not_called()

    def test_missing_catalog_is_not_created(self):
        missing_path = Path(self.temp_dir.name) / "missing.sqlite"
        original_path = self.main.STORE_DB_PATH
        self.main.STORE_DB_PATH = missing_path
        try:
            products_response = self.client.get("/api/products")
            categories_response = self.client.get("/api/categories")
        finally:
            self.main.STORE_DB_PATH = original_path

        self.assertEqual(products_response.status_code, 503)
        self.assertEqual(categories_response.status_code, 503)
        self.assertFalse(missing_path.exists())

    def test_offer_search_does_not_modify_the_temporary_database(self):
        before = self.database_path.read_bytes()

        response = self.client.get(
            "/api/offers/search",
            params={"query": "oats"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
