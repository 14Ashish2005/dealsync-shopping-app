from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Literal
from unittest.mock import Mock, patch

from langchain_core.messages import HumanMessage
from backend.discovery.engine import DiscoveryEngine
from backend.providers.base import (
    NormalizedOffer,
    PriceInput,
    ProviderAdapter,
    ProviderError,
)
from backend.providers.local_catalog import LocalCatalogAdapter
from backend.providers.registry import ProviderRegistry
from backend.visual_search.analyzer import InvalidVisionOutputError, VisionAnalyzer
from backend.visual_search.discovery import (
    VisualDiscoveryService,
)
from backend.visual_search.groq_client import create_vision_analyzer
from backend.visual_search.image_processing import NormalizedImage
from backend.visual_search.models import VisualAnalysis


def valid_analysis(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "is_grocery_product": True,
        "is_organic": None,
        "product_category": "Food",
        "product_type": "Honey",
        "brand": None,
        "model": None,
        "primary_color": "gold",
        "secondary_colors": [],
        "shape_or_design": "glass jar",
        "distinctive_features": ["metal lid"],
        "visible_text": [],
        "search_query": "honey",
        "description": "A jar of honey.",
        "confidence": {
            "product_category": 0.9,
            "product_type": 0.9,
            "primary_color": 0.8,
            "shape_or_design": 0.9,
            "distinctive_features": 0.8,
            "search_query": 0.9,
        },
    }
    result.update(overrides)
    return result


class FakeRunnable:
    def __init__(self, result: object) -> None:
        self.result = result
        self.messages: list[HumanMessage] | None = None

    def invoke(self, input: list[HumanMessage]) -> object:
        self.messages = input
        return self.result


class FakeVisionModel:
    def __init__(self, runnable: FakeRunnable) -> None:
        self.runnable = runnable
        self.schema: type[VisualAnalysis] | None = None
        self.method: Literal["json_mode"] | None = None

    def with_structured_output(
        self,
        schema: type[VisualAnalysis],
        *,
        method: Literal["json_mode"],
    ) -> FakeRunnable:
        self.schema = schema
        self.method = method
        return self.runnable


class FailingProvider(ProviderAdapter):
    def __init__(self, key: str = "failing_provider") -> None:
        self._key = key

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
        currency: str | None = None,
        is_organic: bool | None = None,
    ) -> list[NormalizedOffer]:
        _ = query, max_price, currency, is_organic
        raise ProviderError(self.provider_key, "private provider detail")

    def normalize_offer(
        self,
        product: Mapping[str, object],
    ) -> NormalizedOffer:
        _ = product
        raise NotImplementedError


class VisualDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "catalog.sqlite"
        self.engines: list[DiscoveryEngine] = []
        self._create_database()
        self.image = NormalizedImage(
            data=b"normalized test image",
            media_type="image/jpeg",
            width=8,
            height=6,
        )

    def tearDown(self) -> None:
        for engine in self.engines:
            engine.shutdown(wait_for_tasks=False)
        self.temporary_directory.cleanup()

    def _create_database(self) -> None:
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
                    (1, "Honey glass jar", "Food", 8.0, "Golden honey", 0, "USD"),
                    (2, "Honey squeeze bottle", "Food", 250.0, "Honey", 0, "INR"),
                    (3, "Honey metal tin", "Food", 5.0, "Honey", 0, None),
                ],
            )
            connection.commit()

    def _build_service(
        self,
        response: object,
        *,
        providers: tuple[ProviderAdapter, ...] = (),
    ) -> tuple[VisualDiscoveryService, FakeRunnable]:
        runnable = FakeRunnable(response)
        analyzer = VisionAnalyzer(FakeVisionModel(runnable))
        registry = ProviderRegistry(
            [LocalCatalogAdapter(self.database_path), *providers]
        )
        engine = DiscoveryEngine(registry, max_workers=3, max_admitted=3)
        self.engines.append(engine)
        return VisualDiscoveryService(analyzer, engine), runnable

    def test_successful_analysis_discovers_and_ranks_by_catalog_text(self) -> None:
        response = valid_analysis(
            confidence={
                "product_type": 0.9,
                "shape_or_design": 0.9,
            }
        )
        service, runnable = self._build_service(response)

        result = service.discover(self.image)

        self.assertEqual(result.search_query, "honey")
        messages = runnable.messages
        if messages is None:
            self.fail("The mocked vision model did not receive a message.")
        content = messages[0].content
        if (
            not isinstance(content, list)
            or len(content) < 2
            or not isinstance(content[1], dict)
        ):
            self.fail("The mocked vision message was not multimodal.")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(
            [item.offer.product_name for item in result.ranked_offers],
            ["Honey glass jar", "Honey squeeze bottle", "Honey metal tin"],
        )
        self.assertIn("shape_or_design", result.ranked_offers[0].matched_attributes)
        self.assertEqual(
            result.ranked_offers[0].match_basis,
            "catalog_text",
        )
        serialized = result.as_dict()
        offers = serialized["offers"]
        if not isinstance(offers, list) or not offers or not isinstance(offers[0], dict):
            self.fail("The serialized discovery result has no offers.")
        self.assertEqual(offers[0]["product_name"], "Honey glass jar")
        self.assertEqual(serialized["ranking_method"], "uncalibrated_catalog_text")
        groups = serialized["groups"]
        if not isinstance(groups, list):
            self.fail("The serialized discovery result has no product groups.")
        self.assertEqual(len(groups), 3)
        price_summaries = serialized["price_summaries"]
        if not isinstance(price_summaries, list):
            self.fail("The serialized discovery result has no price summaries.")
        self.assertEqual(
            {
                summary["currency"]
                for summary in price_summaries
                if isinstance(summary, dict)
            },
            {"INR", "USD"},
        )

    def test_missing_search_query_returns_empty_result_without_discovery(self) -> None:
        service, _ = self._build_service(
            {
                "is_grocery_product": False,
                "search_query": None,
                "confidence": {},
            }
        )

        result = service.discover(self.image)

        self.assertEqual(result.search_query, "")
        self.assertEqual(result.discovery.providers, ())
        self.assertEqual(result.discovery.offers, ())
        self.assertEqual(result.ranked_offers, ())

        blank_query_service, _ = self._build_service(
            valid_analysis(search_query="   ")
        )
        with self.assertRaises(InvalidVisionOutputError):
            blank_query_service.discover(self.image)

    def test_brand_and_model_are_used_only_when_confident_and_in_candidate_text(self):
        response = valid_analysis(
            product_type="Tea",
            brand="Acme",
            model="Tea 7",
            search_query="tea",
            confidence={
                "product_type": 0.9,
                "brand": 0.59,
                "model": 0.8,
            },
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (4, "Acme Tea 7", "Tea", 4.0, "Tea", 0, "USD"),
            )
            connection.commit()
        service, _ = self._build_service(response)

        result = service.discover(self.image)

        self.assertIsNone(result.analysis.brand)
        self.assertIsNone(result.analysis.confidence.brand)
        matching = next(
            item
            for item in result.ranked_offers
            if item.offer.product_name == "Acme Tea 7"
        )
        self.assertIn("model", matching.matched_attributes)
        self.assertNotIn("brand", matching.matched_attributes)

    def test_missing_catalog_brand_and_model_remain_unknown(self) -> None:
        response = valid_analysis(
            brand="Unlisted Brand",
            model="Unknown Model",
            confidence={
                "product_type": 0.9,
                "brand": 0.95,
                "model": 0.95,
            },
        )
        service, _ = self._build_service(response)

        result = service.discover(self.image)

        honey_jar = next(
            item
            for item in result.ranked_offers
            if item.offer.product_name == "Honey glass jar"
        )
        self.assertIsNone(honey_jar.offer.brand)
        self.assertIsNone(honey_jar.offer.model)
        self.assertGreater(honey_jar.relevance_score, 0)
        self.assertNotIn("brand", honey_jar.matched_attributes)
        self.assertNotIn("model", honey_jar.matched_attributes)

    def test_visual_organic_classification_does_not_replace_user_filter(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (4, "Organic honey", "Food", 9.0, "Organic honey", 1, "USD"),
            )
            connection.commit()

        service, _ = self._build_service(
            valid_analysis(
                is_organic=True,
                confidence={
                    "product_type": 0.9,
                    "is_organic": 0.99,
                },
            )
        )

        unfiltered = service.discover(self.image)
        explicitly_non_organic = service.discover(
            self.image,
            is_organic=False,
        )

        unfiltered_names = {
            offer.product_name for offer in unfiltered.discovery.offers
        }
        filtered_names = {
            offer.product_name
            for offer in explicitly_non_organic.discovery.offers
        }
        self.assertIn("Organic honey", unfiltered_names)
        self.assertNotIn("Organic honey", filtered_names)
        self.assertEqual(len(unfiltered_names), 4)
        self.assertEqual(len(filtered_names), 3)

    def test_partial_provider_results_are_preserved(self) -> None:
        service, _ = self._build_service(
            valid_analysis(),
            providers=(FailingProvider(),),
        )

        result = service.discover(self.image)

        self.assertEqual(len(result.discovery.offers), 3)
        self.assertEqual(
            [outcome.status for outcome in result.discovery.providers],
            ["success", "error"],
        )
        self.assertTrue(result.partial)
        self.assertTrue(result.as_dict()["partial"])
        self.assertNotIn(
            "private provider detail",
            str(result.as_dict()["providers"]),
        )

    def test_provider_failure_is_reported_without_discarding_engine_outcome(self):
        service, _ = self._build_service(
            valid_analysis(),
            providers=(FailingProvider(),),
        )

        result = service.discover(
            self.image,
            provider_key="failing_provider",
        )

        self.assertEqual(result.discovery.offers, ())
        self.assertEqual(result.discovery.providers[0].status, "error")
        self.assertFalse(result.partial)
        self.assertNotIn(
            "private provider detail",
            str(result.as_dict()["providers"]),
        )

    def test_currency_filters_are_explicit_and_never_cross_currency(self) -> None:
        service, _ = self._build_service(
            valid_analysis(search_query="honey")
        )

        usd_result = service.discover(
            self.image,
            max_price=10,
            currency="USD",
        )
        self.assertEqual(
            [(offer.currency, offer.price_minor) for offer in usd_result.discovery.offers],
            [("USD", 800)],
        )
        self.assertEqual(
            [summary.currency for summary in usd_result.discovery.price_summaries],
            ["USD"],
        )

        inr_result = service.discover(
            self.image,
            max_price=300,
            currency="INR",
        )
        self.assertEqual(
            [(offer.currency, offer.price_minor) for offer in inr_result.discovery.offers],
            [("INR", 25000)],
        )
        self.assertEqual(
            [summary.currency for summary in inr_result.discovery.price_summaries],
            ["INR"],
        )

        unfiltered = service.discover(self.image)
        self.assertEqual(
            {offer.currency for offer in unfiltered.discovery.offers},
            {"INR", "USD", None},
        )

    def test_max_price_without_currency_is_rejected(self) -> None:
        service, _ = self._build_service(valid_analysis())

        with self.assertRaisesRegex(ValueError, "currency"):
            service.discover(self.image, max_price=10)


class GroqClientFactoryTests(unittest.TestCase):
    def test_factory_uses_existing_model_and_timeout_without_api_call(self) -> None:
        fake_groq_module = types.ModuleType("langchain_groq")
        chat_groq = Mock(
            return_value=FakeVisionModel(FakeRunnable(valid_analysis()))
        )
        setattr(fake_groq_module, "ChatGroq", chat_groq)
        with (
            patch("backend.visual_search.groq_client.load_dotenv"),
            patch.dict(os.environ, {"GROQ_API_KEY": "test-only-key"}),
            patch.dict(sys.modules, {"langchain_groq": fake_groq_module}),
        ):
            analyzer = create_vision_analyzer()

        self.assertIsInstance(analyzer, VisionAnalyzer)
        chat_groq.assert_called_once_with(
            model="qwen/qwen3.8-27b",
            temperature=0,
            timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
