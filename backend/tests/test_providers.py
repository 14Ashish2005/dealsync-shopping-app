import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.providers import (
    DEFAULT_PROVIDER_REGISTRY,
    DemoProviderAdapter,
    InvalidProductDataError,
    LocalCatalogAdapter,
    ProviderError,
    ProviderRegistry,
    ProviderUnavailableError,
    create_default_registry,
)


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "catalog.sqlite"
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
                    (2, "Rolled Oats", "grains", 5.49, "Whole grain oats", 0, "USD"),
                ],
            )
            connection.commit()
        self.adapter = LocalCatalogAdapter(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _insert_product(
        self,
        product_id: int,
        name: str,
        category: str | None,
        price: float | None,
        currency: str | None,
    ) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (product_id, name, category, price, "Provider filter test", currency),
            )
            connection.commit()

    def test_local_search_filters_catalog_without_writing(self):
        before = self.database_path.read_bytes()

        offers = self.adapter.search(
            "honey",
            max_price=15,
            currency="INR",
            is_organic=True,
        )

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].product_name, "Organic Raw Honey")
        self.assertEqual(self.database_path.read_bytes(), before)

    def test_offer_normalization_matches_product_offers_schema(self):
        offer = self.adapter.normalize_offer(
            {
                "id": 2,
                "name": "Rolled Oats",
                "price": 5.49,
                "currency": "USD",
            }
        )

        self.assertEqual(
            set(offer.as_dict()),
            {
                "offer_id",
                "provider_key",
                "provider_offer_key",
                "provider_product_id",
                "canonical_product_id",
                "product_name",
                "brand",
                "model",
                "price_minor",
                "currency",
                "product_url",
                "image_url",
                "availability",
                "last_checked_at",
                "expires_at",
                "created_at",
                "updated_at",
                "product_attributes",
            },
        )
        self.assertEqual(offer.provider_offer_key, "local-product:2")
        self.assertEqual(offer.provider_product_id, "2")
        self.assertEqual(offer.currency, "USD")
        self.assertIsNone(offer.canonical_product_id)
        self.assertIsNone(offer.image_url)
        self.assertEqual(offer.availability, "unknown")

    def test_local_attributes_preserve_catalog_and_title_provenance(self):
        offer = self.adapter.normalize_offer(
            {
                "id": 2,
                "name": "Acme phone (128 GB) 8 GB RAM",
                "price": 5.49,
                "currency": "USD",
                "brand": "Acme",
                "model": "M1",
                "category": "Phones",
            }
        )
        attributes = {item.name: item for item in offer.product_attributes or ()}

        self.assertEqual(attributes["brand"].provenance, "local_catalog")
        self.assertEqual(attributes["model"].value, "M1")
        self.assertEqual(attributes["category"].value, "Phones")
        self.assertEqual(
            attributes["storage_capacity"].extraction_method,
            "product_title",
        )
        self.assertEqual(attributes["storage_capacity"].value, 128)
        self.assertEqual(attributes["ram"].value, 8)
        self.assertEqual(attributes["ram"].unit, "GB")
        self.assertEqual(attributes["color"].provenance, "unknown")

    def test_local_resolution_returns_structured_category_attributes(self):
        resolved = self.adapter.resolve_products(("1",))[0]
        attributes = {
            item.name: item
            for item in resolved.product_attributes or ()
        }

        self.assertEqual(attributes["category"].value, "honey")
        self.assertEqual(attributes["category"].provenance, "local_catalog")
        self.assertEqual(
            resolved.offer.product_attributes,
            resolved.product_attributes,
        )

    def test_prices_are_converted_to_integer_paise_and_ids_are_stable(self):
        product = {
            "id": 1,
            "name": "Organic Raw Honey",
            "price": 14.99,
            "currency": "INR",
        }

        first = self.adapter.normalize_offer(product)
        second = self.adapter.normalize_offer(product)

        self.assertEqual(first.price_minor, 1499)
        self.assertEqual(first.offer_id, second.offer_id)
        self.assertEqual(first.provider_offer_key, second.provider_offer_key)

    def test_unknown_currency_is_not_interpreted_as_inr(self):
        offer = self.adapter.normalize_offer(
            {"id": 2, "name": "Rolled Oats", "price": 5.49, "currency": None}
        )

        self.assertIsNone(offer.price_minor)
        self.assertIsNone(offer.currency)

    def test_price_filter_does_not_compare_unlike_currencies(self):
        offers = self.adapter.search(max_price=10, currency="INR")

        self.assertEqual(offers, [])

    def test_price_filter_requires_an_explicit_currency(self):
        with self.assertRaisesRegex(ValueError, "currency code is required"):
            self.adapter.search(max_price=10)
        with self.assertRaisesRegex(ValueError, "currency code is required"):
            self.adapter.search(min_price=10)

    def test_local_price_bounds_are_inclusive_and_currency_safe(self):
        self._insert_product(3, "Boundary low", "Boundary", 10.00, "INR")
        self._insert_product(4, "Boundary high", "Boundary", 15.00, "INR")

        minimum_only = self.adapter.search(
            min_price=15,
            currency="INR",
            category="boundary",
        )
        maximum_only = self.adapter.search(
            max_price=10,
            currency="INR",
            category="Boundary",
        )
        both_bounds = self.adapter.search(
            min_price=10,
            max_price=15,
            currency="INR",
            category="Boundary",
        )
        mismatched_currency = self.adapter.search(
            min_price=0,
            currency="INR",
        )

        self.assertEqual(
            {offer.provider_product_id for offer in minimum_only},
            {"4"},
        )
        self.assertEqual(
            {offer.provider_product_id for offer in maximum_only},
            {"3"},
        )
        self.assertEqual(
            {offer.provider_product_id for offer in both_bounds},
            {"3", "4"},
        )
        self.assertTrue(
            all(offer.currency == "INR" for offer in mismatched_currency)
        )
        self.assertNotIn(
            "2",
            {offer.provider_product_id for offer in mismatched_currency},
        )

    def test_price_filters_exclude_unknown_prices_but_no_filter_preserves_them(self):
        self._insert_product(3, "Unknown currency", "Unknown tests", 5.00, None)
        self._insert_product(4, "Unknown price", "Unknown tests", None, None)

        unfiltered = self.adapter.search(category="Unknown tests")
        filtered = self.adapter.search(
            min_price=0,
            currency="INR",
            category="Unknown tests",
        )

        self.assertEqual(
            {offer.provider_product_id for offer in unfiltered},
            {"3", "4"},
        )
        self.assertTrue(all(offer.price_minor is None for offer in unfiltered))
        self.assertEqual(filtered, [])

    def test_category_filters_are_exact_case_insensitive_and_use_only_explicit_aliases(self):
        self._insert_product(3, "Generic headphones", "Headphones", 12.00, "USD")
        self._insert_product(4, "Wireless headphones", "Wireless headphones", 13.00, "USD")
        self._insert_product(5, "Uncategorized headphones", None, 14.00, "USD")
        self._insert_product(6, "Smartphone", "Smartphones", 100.00, "USD")

        generic = self.adapter.search(category="headphones")
        wireless = self.adapter.search(category="WIRELESS HEADPHONES")
        explicit_alias = self.adapter.search(category="smart phone")
        unrelated = self.adapter.search(category="phone")

        self.assertEqual(
            {offer.provider_product_id for offer in generic},
            {"3"},
        )
        self.assertEqual(
            {offer.provider_product_id for offer in wireless},
            {"4"},
        )
        self.assertEqual(
            {offer.provider_product_id for offer in explicit_alias},
            {"6"},
        )
        self.assertEqual(unrelated, [])

    def test_category_alias_terms_do_not_preempt_category_filtering(self):
        self._insert_product(3, "Acme Mobile Device", "smart phone", 12.00, "INR")

        offers = self.adapter.search(
            "smartphones",
            category="smartphones",
        )
        products = self.adapter.search_catalog_products(
            "smartphones",
            category="smartphones",
        )

        self.assertEqual(
            {offer.provider_product_id for offer in offers},
            {"3"},
        )
        self.assertEqual([product["id"] for product in products], [3])

    def test_local_catalog_product_search_uses_the_same_filters(self):
        self._insert_product(3, "Boundary low", "Boundary", 10.00, "INR")
        self._insert_product(4, "Boundary high", "Boundary", 15.00, "INR")

        products = self.adapter.search_catalog_products(
            min_price=15,
            max_price=15,
            currency="INR",
            category="boundary",
        )

        self.assertEqual([product["id"] for product in products], [4])

    def test_contradictory_price_ranges_are_rejected_by_both_providers(self):
        adapters = (self.adapter, DemoProviderAdapter())
        for adapter in adapters:
            with self.subTest(provider=adapter.provider_key):
                with self.assertRaisesRegex(
                    ValueError,
                    "Minimum price cannot exceed maximum price",
                ):
                    adapter.search(
                        min_price=20,
                        max_price=10,
                        currency="INR",
                    )

    def test_empty_search_returns_no_offers(self):
        self.assertEqual(self.adapter.search("not-in-catalog"), [])

    def test_invalid_product_data_raises_a_provider_data_error(self):
        with self.assertRaises(InvalidProductDataError):
            self.adapter.normalize_offer(
                {"id": 1, "name": "Invalid product", "price": "not-a-price"}
            )

    def test_unavailable_catalog_raises_a_specific_provider_error(self):
        unavailable = LocalCatalogAdapter(
            Path(self.temp_dir.name) / "missing.sqlite"
        )

        with self.assertRaises(ProviderUnavailableError):
            unavailable.search()

    def test_catalog_database_errors_are_wrapped_consistently(self):
        with patch(
            "backend.providers.local_catalog.sqlite3.connect",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            with self.assertRaises(ProviderError) as raised:
                self.adapter.search()

        self.assertEqual(raised.exception.provider_key, self.adapter.provider_key)

    def test_duplicate_provider_keys_are_rejected(self):
        registry = ProviderRegistry()
        registry.register(self.adapter)

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(LocalCatalogAdapter(self.database_path))

    def test_unknown_provider_lookup_reports_unavailability(self):
        registry = create_default_registry(self.adapter)

        with self.assertRaises(ProviderUnavailableError):
            registry.get("unregistered_provider")

    def test_default_registry_contains_local_catalog_then_demo_provider(self):
        self.assertEqual(
            DEFAULT_PROVIDER_REGISTRY.provider_keys,
            ("local_catalog", "demo_catalog"),
        )
        self.assertIsInstance(
            DEFAULT_PROVIDER_REGISTRY.get("local_catalog"),
            LocalCatalogAdapter,
        )
        self.assertIsInstance(
            DEFAULT_PROVIDER_REGISTRY.get("demo_catalog"),
            DemoProviderAdapter,
        )

    def test_demo_provider_returns_stable_labelled_offers_without_checkout(self):
        adapter = DemoProviderAdapter()

        offers = adapter.search()
        repeated_offers = adapter.search()

        self.assertEqual(adapter.provider_key, "demo_catalog")
        self.assertEqual(
            adapter.display_name,
            "DealSync Demo Catalog (Sample Data)",
        )
        self.assertFalse(adapter.supports_external_cart)
        self.assertEqual(len(offers), 23)
        self.assertEqual(
            [offer.offer_id for offer in offers],
            [offer.offer_id for offer in repeated_offers],
        )
        self.assertEqual(len({offer.offer_id for offer in offers}), len(offers))
        self.assertEqual(
            [offer.provider_product_id for offer in offers],
            [offer.provider_product_id for offer in repeated_offers],
        )
        self.assertEqual(
            len({offer.provider_product_id for offer in offers}),
            len(offers),
        )
        for offer in offers:
            self.assertEqual(offer.provider_key, "demo_catalog")
            self.assertTrue(offer.product_name.startswith("[DEMO SAMPLE] "))
            self.assertIsInstance(offer.price_minor, int)
            self.assertGreater(offer.price_minor, 0)
            self.assertIn(offer.currency, {"INR", "USD", "EUR", "GBP", "CAD"})
            self.assertIsNone(offer.product_url)
            self.assertEqual(offer.availability, "unknown")
            self.assertIsNone(offer.canonical_product_id)

    def test_demo_search_covers_expanded_categories_and_searchable_fields(self):
        adapter = DemoProviderAdapter()
        searches = {
            "wireless headphones": "demo-006",
            "smartphone": "demo-007",
            "laptop": "demo-009",
            "smartwatch": "demo-011",
            "Computer accessories": "demo-012",
            "privacy shutter": "demo-013",
        }

        for query, expected_product_id in searches.items():
            with self.subTest(query=query):
                offers = adapter.search(query)
                self.assertIn(
                    expected_product_id,
                    {offer.provider_product_id for offer in offers},
                )
                self.assertTrue(
                    all(
                        offer.product_name.startswith("[DEMO SAMPLE] ")
                        for offer in offers
                    )
                )

    def test_demo_smartphone_samples_match_search_and_explicit_inr_budget(self):
        adapter = DemoProviderAdapter()
        new_product_ids = {"demo-021", "demo-022", "demo-023"}

        unfiltered_offers = adapter.search("smartphones")
        unfiltered_ids = {
            offer.provider_product_id for offer in unfiltered_offers
        }
        self.assertTrue(new_product_ids.issubset(unfiltered_ids))

        budgeted_offers = adapter.search(
            "smartphones",
            max_price=20000,
            currency="INR",
        )
        budgeted_ids = {
            offer.provider_product_id for offer in budgeted_offers
        }
        self.assertEqual(budgeted_ids, new_product_ids)
        self.assertNotIn("demo-008", budgeted_ids)
        self.assertEqual(len(budgeted_offers), len(new_product_ids))
        for offer in budgeted_offers:
            self.assertEqual(offer.currency, "INR")
            self.assertLessEqual(offer.price_minor, 2_000_000)
            self.assertTrue(offer.product_name.startswith("[DEMO SAMPLE] "))
            self.assertIsNone(offer.product_url)
            self.assertEqual(offer.availability, "unknown")

    def test_demo_search_uses_query_organic_and_explicit_currency_filters(self):
        adapter = DemoProviderAdapter()

        lamp_offers = adapter.search("desk lamp")
        organic_offers = adapter.search(is_organic=True)
        usd_offers = adapter.search(max_price=20, currency="USD")

        self.assertEqual(len(lamp_offers), 1)
        self.assertIn("desk lamp", lamp_offers[0].product_name.casefold())
        self.assertTrue(
            all(
                offer.product_name.startswith("[DEMO SAMPLE] ")
                for offer in organic_offers
            )
        )
        self.assertTrue(all(offer.currency == "USD" for offer in usd_offers))
        self.assertTrue(all(offer.price_minor <= 2000 for offer in usd_offers))
        with self.assertRaisesRegex(ValueError, "currency code is required"):
            adapter.search(max_price=20)

    def test_demo_price_and_category_filters_match_local_semantics(self):
        adapter = DemoProviderAdapter()

        minimum_only = adapter.search(
            min_price=16_999,
            currency="INR",
            category="smart phone",
        )
        maximum_only = adapter.search(
            max_price=12_999,
            currency="INR",
            category="SMARTPHONES",
        )
        both_bounds = adapter.search(
            min_price=12_999,
            max_price=16_999,
            currency="INR",
            category="smartphones",
        )
        wrong_currency = adapter.search(
            min_price=0,
            currency="INR",
            category="smartphones",
        )
        distinct_category = adapter.search(category="headphones")
        category_only = adapter.search(category="smartphones")
        alias_query = adapter.search(
            "smart phone",
            category="smartphones",
        )

        self.assertIn("demo-022", {offer.provider_product_id for offer in minimum_only})
        self.assertEqual(
            {offer.provider_product_id for offer in maximum_only},
            {"demo-021"},
        )
        self.assertEqual(
            {offer.provider_product_id for offer in both_bounds},
            {"demo-021", "demo-022"},
        )
        self.assertTrue(all(offer.currency == "INR" for offer in wrong_currency))
        self.assertFalse(
            any(offer.provider_product_id == "demo-006" for offer in distinct_category)
        )
        self.assertEqual(
            {offer.provider_product_id for offer in alias_query},
            {offer.provider_product_id for offer in category_only},
        )
        self.assertTrue(
            all(
                offer.product_name.startswith("[DEMO SAMPLE] ")
                for offer in (*minimum_only, *maximum_only, *both_bounds)
            )
        )

    def test_demo_product_resolution_uses_exact_ids(self):
        adapter = DemoProviderAdapter()
        offer = adapter.search("desk lamp")[0]

        resolved = adapter.resolve_products(
            (offer.provider_product_id, "demo-unknown")
        )

        self.assertEqual(len(resolved), 1)
        self.assertEqual(
            resolved[0].offer.provider_product_id,
            offer.provider_product_id,
        )
        self.assertEqual(resolved[0].offer.provider_key, "demo_catalog")
        with self.assertRaises(ProviderUnavailableError):
            adapter.resolve_offer(offer.provider_offer_key)

    def test_demo_attributes_keep_sample_and_title_extraction_provenance(self):
        adapter = DemoProviderAdapter()
        offer = next(
            item
            for item in adapter.search("smartphone")
            if item.provider_product_id == "demo-021"
        )
        attributes = {item.name: item for item in offer.product_attributes or ()}

        self.assertEqual(attributes["category"].provenance, "demo_sample")
        self.assertEqual(attributes["storage_capacity"].value, 64)
        self.assertEqual(attributes["storage_capacity"].provenance, "demo_sample")
        self.assertEqual(
            attributes["storage_capacity"].extraction_method,
            "product_title",
        )
        self.assertEqual(attributes["ram"].provenance, "unknown")
        resolved = adapter.resolve_products(("demo-021",))[0]
        self.assertEqual(resolved.product_attributes, offer.product_attributes)

    def test_demo_normalization_rejects_unlabelled_or_invalid_currency_data(self):
        adapter = DemoProviderAdapter()
        product = {
            "id": "demo-999",
            "name": "Unlabelled product",
            "price": 10,
            "currency": "USD",
        }

        with self.assertRaises(InvalidProductDataError):
            adapter.normalize_offer(product)

        product["name"] = "[DEMO SAMPLE] Invalid currency product"
        product["currency"] = "XXX"
        with self.assertRaises(InvalidProductDataError):
            adapter.normalize_offer(product)


if __name__ == "__main__":
    unittest.main()
