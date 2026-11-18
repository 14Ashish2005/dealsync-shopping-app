import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.comparison.chat import (
    ComparisonChatContext,
    create_comparison_chat_tools,
    format_comparison_chat_reply,
)
from backend.comparison.api_models import build_comparison_api_response
from backend.comparison.models import ComparisonRequest, ProductReference
from backend.comparison.service import ComparisonService
from backend.discovery import DiscoveryEngine
from backend.providers import (
    DemoProviderAdapter,
    LocalCatalogAdapter,
    ProviderRegistry,
)
from backend.providers.base import (
    ProviderUnavailableError,
    ResolvedProviderProduct,
)
from backend.tests._support import (
    import_main_with_temporary_auth_db,
    prepare_test_schemas,
)


class ComparisonChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_temporary_auth_db()

    def setUp(self):
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
        self.registry = ProviderRegistry([
            LocalCatalogAdapter(self.store_path),
            DemoProviderAdapter(),
        ])
        setattr(self.main, "PROVIDER_REGISTRY", self.registry)
        prepare_test_schemas(self.store_path, self.auth_path)
        self.client = TestClient(self.main.app, raise_server_exceptions=False)
        with patch.object(
            self.main,
            "create_vision_analyzer",
            side_effect=ValueError("vision is not needed in this test"),
        ):
            self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        setattr(self.main, "STORE_DB_PATH", self.original_store_path)
        setattr(self.main, "AUTH_DB_PATH", self.original_auth_path)
        setattr(self.main, "PROVIDER_REGISTRY", self.original_registry)
        self.temp_dir.cleanup()

    def _create_database(self):
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
                    (3, "Legacy Honey", "honey", 8.00, None, None, None),
                ],
            )
            connection.execute(
                "INSERT INTO reviews (product_id, rating) VALUES (?, ?)",
                (1, 4.5),
            )
            connection.commit()

    def _chat_tools(self, user_messages=()):
        engine = DiscoveryEngine(self.registry, max_workers=1, max_admitted=2)
        self.addCleanup(engine.shutdown)
        context = ComparisonChatContext(user_messages=tuple(user_messages))
        search, compare, _ = create_comparison_chat_tools(
            discovery_engine=engine,
            registry=self.registry,
            context=context,
        )
        return context, search, compare

    def _fake_shopping_agent_module(self):
        module = ModuleType("shopping_agent")
        setattr(module, "llm", object())
        for name in (
            "search_products",
            "list_categories",
            "get_rating",
            "get_batch_ratings",
        ):
            setattr(module, name, SimpleNamespace(name=name))
        return module

    def _post_chat_with_fake_agent(
        self,
        payload,
        *,
        compare_selected=False,
    ):
        captured = {}

        def fake_create_agent(**configuration):
            tools = {
                tool.name: tool
                for tool in configuration["tools"]
                if hasattr(tool, "name")
            }
            captured["tool_names"] = set(tools)
            captured["system_prompt"] = configuration["system_prompt"]

            def invoke(request):
                captured["request"] = request
                selected = json.loads(
                    tools["get_selected_products"].invoke({})
                )
                captured["selected_context"] = selected
                if compare_selected and selected["products"]:
                    captured["comparison_tool_result"] = json.loads(
                        tools["compare_products"].invoke({
                            "candidates": [
                                product["reference"]
                                for product in selected["products"]
                            ],
                        })
                    )
                return {
                    "messages": [
                        SimpleNamespace(
                            type="ai",
                            content="The first product has a better camera.",
                        )
                    ]
                }

            return SimpleNamespace(invoke=invoke)

        fake_module = self._fake_shopping_agent_module()
        with (
            patch.dict(sys.modules, {"shopping_agent": fake_module}),
            patch("langchain.agents.create_agent", side_effect=fake_create_agent),
        ):
            response = self.client.post("/api/chat", json=payload)
        return response, captured

    def _cart_owner(self, owner_id):
        return self.main.create_session(
            owner_id,
            self.main.app.state.database_engines.authentication,
        )

    def _post_cart_preview(self, owner_token, selected_products, message):
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            owner_token,
        )
        return self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": message}],
                "selected_products": selected_products,
            },
        )

    def _get_cart(self, owner_token):
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            owner_token,
        )
        return self.client.get(
            "/api/cart",
        ).json()

    def _confirm_cart_preview(
        self,
        owner_token,
        selected_products,
        token,
        message="Confirm adding eligible products to my cart",
    ):
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            owner_token,
        )
        return self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": message}],
                "selected_products": selected_products,
                "cart_confirmation_token": token,
            },
        )

    def test_cart_preview_resolves_local_product_without_modifying_cart(self):
        owner_token = self._cart_owner("cart-preview-owner")
        response = self._post_cart_preview(
            owner_token,
            [{
                "provider_key": "local_catalog",
                "provider_product_id": "1",
            }],
            "Prepare a cart from my selected products.",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["cart_preview"]["eligible_count"], 1)
        self.assertEqual(body["cart_preview"]["ineligible_count"], 0)
        self.assertEqual(body["cart_preview"]["items"][0]["product_name"], "Organic Honey")
        self.assertTrue(body["cart_preview"]["items"][0]["eligible"])
        self.assertTrue(body["cart_confirmation_token"])
        self.assertEqual(self._get_cart(owner_token)["items"], [])

    def test_cart_preview_keeps_demo_products_ineligible(self):
        owner_token = self._cart_owner("demo-cart-preview-owner")
        response = self._post_cart_preview(
            owner_token,
            [{
                "provider_key": "demo_catalog",
                "provider_product_id": "demo-021",
            }],
            "Prepare a cart from my selected products.",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        item = body["cart_preview"]["items"][0]
        self.assertEqual(body["cart_preview"]["eligible_count"], 0)
        self.assertIsNone(body["cart_confirmation_token"])
        self.assertFalse(item["eligible"])
        self.assertTrue(item["is_demo_sample"])
        self.assertTrue(item["product_name"].startswith("[DEMO SAMPLE] "))
        self.assertIn("synthetic samples", item["eligibility_reason"])
        self.assertIn("not a live marketplace price", body["reply"])
        self.assertEqual(self._get_cart(owner_token)["items"], [])

    def test_mixed_cart_preview_confirmation_adds_only_local_products(self):
        owner_token = self._cart_owner("mixed-cart-preview-owner")
        selected_products = [
            {
                "provider_key": "local_catalog",
                "provider_product_id": "1",
            },
            {
                "provider_key": "demo_catalog",
                "provider_product_id": "demo-021",
            },
        ]
        preview_response = self._post_cart_preview(
            owner_token,
            selected_products,
            "Prepare a cart from my selected products.",
        )

        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.json()
        self.assertEqual(preview["cart_preview"]["eligible_count"], 1)
        self.assertEqual(preview["cart_preview"]["ineligible_count"], 1)
        self.assertEqual(self._get_cart(owner_token)["items"], [])

        confirmation = self._confirm_cart_preview(
            owner_token,
            selected_products,
            preview["cart_confirmation_token"],
        )

        self.assertEqual(confirmation.status_code, 200)
        result = confirmation.json()
        self.assertEqual(result["cart_action"]["status"], "added")
        self.assertIn("No purchase or order was made.", result["reply"])
        cart = self._get_cart(owner_token)
        self.assertEqual([item["product_id"] for item in cart["items"]], [1])
        self.assertEqual(cart["items"][0]["quantity"], 1)

    def test_repeated_confirmation_returns_outcome_without_duplicate_addition(self):
        owner_token = self._cart_owner("repeat-cart-confirmation-owner")
        selected_products = [{
            "provider_key": "local_catalog",
            "provider_product_id": "1",
        }]
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            owner_token,
        )
        seeded_cart = self.client.post(
            "/api/cart/items",
            json={"product_id": 1, "quantity": 2},
        )
        self.assertEqual(seeded_cart.status_code, 200)

        preview = self._post_cart_preview(
            owner_token,
            selected_products,
            "Prepare a cart from my selected products.",
        ).json()
        token = preview["cart_confirmation_token"]
        first = self._confirm_cart_preview(owner_token, selected_products, token)
        repeated = self._confirm_cart_preview(owner_token, selected_products, token)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["cart_action"]["status"], "added")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["cart_action"]["status"], "already_committed")
        self.assertTrue(repeated.json()["cart_action"]["replayed"])
        self.assertIn("No additional products were added", repeated.json()["reply"])
        cart = self._get_cart(owner_token)
        self.assertEqual(len(cart["items"]), 1)
        self.assertEqual(cart["items"][0]["quantity"], 3)

        altered = self._confirm_cart_preview(
            owner_token,
            [{
                "provider_key": "local_catalog",
                "provider_product_id": "2",
            }],
            token,
        )
        self.assertEqual(altered.status_code, 409)
        self.assertEqual(self._get_cart(owner_token)["items"][0]["quantity"], 3)

    def test_committed_confirmation_can_be_recovered_after_lost_response(self):
        owner_token = self._cart_owner("lost-cart-confirmation-response-owner")
        selected_products = [{
            "provider_key": "local_catalog",
            "provider_product_id": "1",
        }]
        preview = self._post_cart_preview(
            owner_token,
            selected_products,
            "Prepare a cart from my selected products.",
        ).json()
        confirm = self.main._confirm_selected_cart_preview
        response_lost = False

        def commit_then_lose_response(*args, **kwargs):
            nonlocal response_lost
            result = confirm(*args, **kwargs)
            if not response_lost:
                response_lost = True
                raise RuntimeError("Simulated response loss after commit.")
            return result

        with patch.object(
            self.main,
            "_confirm_selected_cart_preview",
            side_effect=commit_then_lose_response,
        ):
            lost_response = self._confirm_cart_preview(
                owner_token,
                selected_products,
                preview["cart_confirmation_token"],
            )
            self.assertEqual(lost_response.status_code, 500)
            self.assertEqual(self._get_cart(owner_token)["items"][0]["quantity"], 1)

            recovered = self._confirm_cart_preview(
                owner_token,
                selected_products,
                preview["cart_confirmation_token"],
            )

        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(
            recovered.json()["cart_action"]["status"],
            "already_committed",
        )
        self.assertEqual(self._get_cart(owner_token)["items"][0]["quantity"], 1)

    def test_failed_cart_transaction_rolls_back_without_outcome_record(self):
        owner_token = self._cart_owner("failed-cart-confirmation-owner")
        selected_products = [{
            "provider_key": "local_catalog",
            "provider_product_id": "1",
        }]
        preview = self._post_cart_preview(
            owner_token,
            selected_products,
            "Prepare a cart from my selected products.",
        ).json()

        with patch.object(
            self.main,
            "_cart_response",
            side_effect=RuntimeError("Simulated cart transaction failure."),
        ):
            failed = self._confirm_cart_preview(
                owner_token,
                selected_products,
                preview["cart_confirmation_token"],
            )

        self.assertEqual(failed.status_code, 500)
        self.assertEqual(self._get_cart(owner_token)["items"], [])
        with closing(sqlite3.connect(self.store_path)) as connection:
            outcome_count = connection.execute(
                "SELECT COUNT(*) FROM cart_confirmation_outcomes"
            ).fetchone()[0]
        self.assertEqual(outcome_count, 0)

        retry = self._confirm_cart_preview(
            owner_token,
            selected_products,
            preview["cart_confirmation_token"],
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["cart_action"]["status"], "added")
        cart = self._get_cart(owner_token)
        self.assertEqual(len(cart["items"]), 1)
        self.assertEqual(cart["items"][0]["quantity"], 1)

    def test_cart_preview_with_no_selection_does_not_create_confirmation(self):
        owner_token = self._cart_owner("empty-cart-preview-owner")
        response = self._post_cart_preview(
            owner_token,
            [],
            "Prepare a cart from my selected products.",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["cart_preview"]["items"], [])
        self.assertEqual(body["cart_preview"]["eligible_count"], 0)
        self.assertIsNone(body["cart_confirmation_token"])
        self.assertIn("No products are selected", body["reply"])
        self.assertEqual(self._get_cart(owner_token)["items"], [])

    def test_cart_confirmation_without_preview_token_does_not_modify_cart(self):
        owner_token = self._cart_owner("missing-token-cart-owner")
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            owner_token,
        )
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [{
                    "role": "user",
                    "content": "Confirm adding eligible products to my cart",
                }],
                "selected_products": [{
                    "provider_key": "local_catalog",
                    "provider_product_id": "1",
                }],
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self._get_cart(owner_token)["items"], [])

    def test_cart_confirmation_rejects_changed_selection_and_stale_product(self):
        owner_token = self._cart_owner("changed-cart-preview-owner")
        selected_product = [{
            "provider_key": "local_catalog",
            "provider_product_id": "1",
        }]

        preview = self._post_cart_preview(
            owner_token,
            selected_product,
            "Prepare a cart from my selected products.",
        ).json()
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            owner_token,
        )
        altered = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Confirm cart"}],
                "selected_products": [{
                    "provider_key": "local_catalog",
                    "provider_product_id": "2",
                }],
                "cart_confirmation_token": preview["cart_confirmation_token"],
            },
        )
        self.assertEqual(altered.status_code, 409)
        self.assertEqual(self._get_cart(owner_token)["items"], [])

        preview = self._post_cart_preview(
            owner_token,
            selected_product,
            "Prepare a cart from my selected products.",
        ).json()
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute(
                "UPDATE products SET price = ? WHERE id = ?",
                (11.00, 1),
            )
            connection.commit()
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            owner_token,
        )
        stale = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Confirm cart"}],
                "selected_products": selected_product,
                "cart_confirmation_token": preview["cart_confirmation_token"],
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self._get_cart(owner_token)["items"], [])

    def test_cart_preview_requires_authentication_and_is_owner_bound(self):
        selected_product = [{
            "provider_key": "local_catalog",
            "provider_product_id": "1",
        }]
        unauthenticated = self.client.post(
            "/api/chat",
            json={
                "messages": [{
                    "role": "user",
                    "content": "Prepare a cart from my selected products.",
                }],
                "selected_products": selected_product,
            },
        )
        self.assertEqual(unauthenticated.status_code, 401)

        owner_a = self._cart_owner("cart-preview-owner-a")
        owner_b = self._cart_owner("cart-preview-owner-b")
        preview = self._post_cart_preview(
            owner_a,
            selected_product,
            "Prepare a cart from my selected products.",
        ).json()
        self.client.cookies.clear()
        unauthenticated_confirmation = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Confirm cart"}],
                "selected_products": selected_product,
                "cart_confirmation_token": preview["cart_confirmation_token"],
            },
        )
        self.assertEqual(unauthenticated_confirmation.status_code, 401)

        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            owner_b,
        )
        confirmation = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Confirm cart"}],
                "selected_products": selected_product,
                "cart_confirmation_token": preview["cart_confirmation_token"],
            },
        )

        self.assertEqual(confirmation.status_code, 409)
        self.assertEqual(self._get_cart(owner_a)["items"], [])
        self.assertEqual(self._get_cart(owner_b)["items"], [])

    def test_cart_preview_preserves_provider_unavailability(self):
        owner_token = self._cart_owner("unavailable-cart-preview-owner")
        provider = self.registry.get("local_catalog")
        with patch.object(
            provider,
            "resolve_products",
            side_effect=ProviderUnavailableError(
                "local_catalog",
                "Catalog lookup unavailable.",
            ),
        ):
            response = self._post_cart_preview(
                owner_token,
                [{
                    "provider_key": "local_catalog",
                    "provider_product_id": "1",
                }],
                "Prepare a cart from my selected products.",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "Selected product information is temporarily unavailable.",
        )
        self.assertEqual(self._get_cart(owner_token)["items"], [])

    def test_api_resolves_selected_demo_products_for_ai_comparison_context(self):
        response, captured = self._post_chat_with_fake_agent(
            {
                "messages": [
                    {"role": "user", "content": "Compare my selected products."}
                ],
                "selected_products": [
                    {
                        "provider_key": "demo_catalog",
                        "provider_product_id": "demo-021",
                    },
                    {
                        "provider_key": "demo_catalog",
                        "provider_product_id": "demo-022",
                    },
                ],
            },
            compare_selected=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("get_selected_products", captured["tool_names"])
        self.assertEqual(
            [
                product["reference"]
                for product in captured["selected_context"]["products"]
            ],
            [
                {
                    "provider_key": "demo_catalog",
                    "provider_product_id": "demo-021",
                },
                {
                    "provider_key": "demo_catalog",
                    "provider_product_id": "demo-022",
                },
            ],
        )
        self.assertTrue(
            all(
                product["product_name"].startswith("[DEMO SAMPLE] ")
                for product in captured["selected_context"]["products"]
            )
        )
        self.assertNotIn(
            "camera",
            {
                key
                for product in captured["selected_context"]["products"]
                for key in product
            },
        )
        self.assertIn(
            "unknown_attributes",
            {
                key
                for candidate in captured["comparison_tool_result"]["candidates"]
                for key in candidate
            },
        )
        self.assertNotIn("better camera", response.json()["reply"].casefold())
        self.assertIn("lower sample price", response.json()["reply"].casefold())
        self.assertIn(
            "if minimizing upfront cost is your priority",
            response.json()["reply"].casefold(),
        )
        self.assertIn(
            "best overall: the available verified information is not sufficient",
            response.json()["reply"].casefold(),
        )
        self.assertIn("sample data", response.json()["reply"].casefold())
        self.assertIn("specifications", response.json()["reply"].casefold())
        self.assertNotIn(
            "verified catalog information",
            response.json()["reply"].casefold(),
        )
        self.assertIn("missing", captured["system_prompt"].casefold())
        self.assertIn("sample data", captured["system_prompt"].casefold())

    def _format_demo_comparison(self, products):
        adapter = DemoProviderAdapter()
        resolved_by_id = {}
        references = []
        for product_id, name, price in products:
            offer = adapter.normalize_offer({
                "id": product_id,
                "name": name,
                "price": price,
                "currency": "INR",
            })
            resolved_by_id[product_id] = ResolvedProviderProduct(
                offer=offer,
                category="Smartphones",
                description=None,
                is_organic=False,
                average_rating=None,
                review_count=None,
            )
            references.append(ProductReference(
                provider_key="demo_catalog",
                provider_product_id=product_id,
            ))

        def resolve_products(product_ids):
            return tuple(
                resolved_by_id[product_id]
                for product_id in product_ids
                if product_id in resolved_by_id
            )

        with patch.object(adapter, "resolve_products", side_effect=resolve_products):
            result = ComparisonService(
                ProviderRegistry([adapter])
            ).compare(ComparisonRequest(candidates=tuple(references)))
        return format_comparison_chat_reply(build_comparison_api_response(result))

    def test_same_title_stated_storage_produces_only_a_conditional_price_value_note(self):
        reply = self._format_demo_comparison([
            (
                "demo-901",
                "[DEMO SAMPLE] Android smartphone (128 GB)",
                "12999.00",
            ),
            (
                "demo-902",
                "[DEMO SAMPLE] 5G smartphone (128 GB)",
                "16999.00",
            ),
        ])
        normalized_reply = reply.casefold()

        self.assertIn("both product names state 128 gb", normalized_reply)
        self.assertIn("not independently verified", normalized_reply)
        self.assertIn("lower sample price", normalized_reply)
        self.assertIn("inr 4000.00", normalized_reply)
        self.assertIn("if the stated storage is accurate", normalized_reply)
        self.assertIn(
            "better listed-price value for the same stated storage",
            normalized_reply,
        )
        self.assertIn("best overall:", normalized_reply)
        self.assertIn("camera, processor, ram, battery, display", normalized_reply)
        self.assertIn('"5g" wording', normalized_reply)
        self.assertIn("sample data - not live marketplace prices", normalized_reply)

    def test_api_accepts_empty_selected_products_and_legacy_chat_payload(self):
        response, captured = self._post_chat_with_fake_agent(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "selected_products": [],
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["selected_context"], {
            "status": "no_selection",
            "products": [],
        })

        legacy_response, legacy_captured = self._post_chat_with_fake_agent({
            "messages": [{"role": "user", "content": "Hello"}],
        })
        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(
            legacy_captured["selected_context"]["status"],
            "no_selection",
        )

    def test_api_rejects_unknown_selected_provider(self):
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Compare these"}],
                "selected_products": [{
                    "provider_key": "not_registered",
                    "provider_product_id": "item-1",
                }],
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_api_rejects_unresolvable_selected_product(self):
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Compare these"}],
                "selected_products": [{
                    "provider_key": "demo_catalog",
                    "provider_product_id": "demo-unknown",
                }],
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_api_rejects_duplicate_selected_product_references(self):
        reference = {
            "provider_key": "demo_catalog",
            "provider_product_id": "demo-021",
        }
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Compare these"}],
                "selected_products": [reference, reference],
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_api_rejects_excessive_selected_product_count(self):
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "Compare these"}],
                "selected_products": [
                    {
                        "provider_key": "demo_catalog",
                        "provider_product_id": f"demo-{index:03d}",
                    }
                    for index in range(1, 7)
                ],
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_api_rejects_malformed_selected_product_reference(self):
        for product_id in (21, "demo-021<script>"):
            with self.subTest(product_id=product_id):
                response = self.client.post(
                    "/api/chat",
                    json={
                        "messages": [{
                            "role": "user",
                            "content": "Compare these",
                        }],
                        "selected_products": [{
                            "provider_key": "demo_catalog",
                            "provider_product_id": product_id,
                        }],
                    },
                )
                self.assertEqual(response.status_code, 422)

    def test_api_reports_selected_provider_unavailability(self):
        demo_provider = self.registry.get("demo_catalog")
        with patch.object(
            demo_provider,
            "resolve_products",
            side_effect=ProviderUnavailableError(
                "demo_catalog",
                "Demo product lookup unavailable.",
            ),
        ):
            response = self.client.post(
                "/api/chat",
                json={
                    "messages": [{
                        "role": "user",
                        "content": "Compare my selected products.",
                    }],
                    "selected_products": [
                        {
                            "provider_key": "demo_catalog",
                            "provider_product_id": "demo-021",
                        },
                        {
                            "provider_key": "demo_catalog",
                            "provider_product_id": "demo-022",
                        },
                    ],
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "Selected product information is temporarily unavailable.",
        )

    def test_comparison_tool_uses_exact_discovery_references(self):
        context, search, compare = self._chat_tools()
        discovery = json.loads(search.invoke({"query": "honey"}))
        references = [
            offer["reference"] for offer in discovery["offers"][:2]
        ]

        comparison = json.loads(compare.invoke({"candidates": references}))

        self.assertEqual(
            [candidate["reference"] for candidate in comparison["candidates"]],
            references,
        )
        self.assertEqual(
            context.discovered_references,
            {
                (reference["provider_key"], reference["provider_product_id"])
                for reference in (
                    offer["reference"] for offer in discovery["offers"]
                )
            },
        )

    def test_unsearched_references_are_rejected_and_clarification_is_returned(self):
        context, search, compare = self._chat_tools()
        self.assertEqual(search.name, "search_discovery_products")

        response = json.loads(compare.invoke({
            "candidates": [
                {"provider_key": "local_catalog", "provider_product_id": "1"},
                {"provider_key": "local_catalog", "provider_product_id": "2"},
            ],
        }))

        self.assertEqual(response["status"], "clarification_required")
        self.assertIsNotNone(context.clarification)
        self.assertIsNone(context.comparison_result)

    def test_insufficient_candidates_ask_for_clarification(self):
        context, search, compare = self._chat_tools()
        self.assertEqual(search.name, "search_discovery_products")

        response = json.loads(compare.invoke({
            "candidates": [
                {"provider_key": "local_catalog", "provider_product_id": "1"},
            ],
        }))

        self.assertEqual(response["status"], "clarification_required")
        self.assertIn("two or more products", response["message"])
        self.assertEqual(context.discovered_references, set())

    def test_comparison_tool_rejects_client_supplied_product_evidence(self):
        context, search, compare = self._chat_tools()
        self.assertEqual(search.name, "search_discovery_products")

        with self.assertRaises((ValueError, TypeError)):
            compare.invoke({
                "candidates": [
                    {
                        "provider_key": "local_catalog",
                        "provider_product_id": "1",
                        "price_minor": 1,
                    },
                    {
                        "provider_key": "local_catalog",
                        "provider_product_id": "2",
                    },
                ],
            })
        self.assertIsNone(context.comparison_result)

    def test_no_clear_winner_and_unknown_evidence_are_rendered_honestly(self):
        context, search, compare = self._chat_tools()
        discovery = json.loads(search.invoke({"query": "honey"}))
        references = [
            offer["reference"] for offer in discovery["offers"][:2]
        ]
        result = json.loads(compare.invoke({"candidates": references}))

        comparison_result = context.comparison_result
        self.assertIsNotNone(comparison_result)
        if comparison_result is None:
            self.fail("The comparison tool did not store its verified result.")
        reply = format_comparison_chat_reply(comparison_result)

        self.assertEqual(result["recommendation"]["status"], "no_clear_winner")
        self.assertIn("best overall", reply.casefold())
        self.assertIn(
            "not sufficient to establish an overall winner",
            reply.casefold(),
        )
        self.assertIn("unknown", reply)
        self.assertIn("Rating: unknown (no reviews)", reply)
        self.assertIn("Currency-separated price ranges", reply)
        self.assertIn("USD", reply)
        self.assertIn("INR", reply)
        self.assertNotIn("5 stars", reply)

    def test_currency_mismatch_remains_unknown_in_chat_result(self):
        context, search, compare = self._chat_tools(
            user_messages=("Compare these products with a maximum budget of 20 USD.",)
        )
        discovery = json.loads(search.invoke({"query": "honey"}))
        references = [
            offer["reference"] for offer in discovery["offers"][:2]
        ]

        compare.invoke({
            "candidates": references,
            "budget": {"amount": "20", "currency": "USD"},
        })

        comparison_result = context.comparison_result
        self.assertIsNotNone(comparison_result)
        if comparison_result is None:
            self.fail("The comparison tool did not store its verified result.")
        evaluations = [
            candidate.budget_evaluation
            for candidate in comparison_result.candidates
        ]
        self.assertIsNotNone(evaluations[1])
        if evaluations[1] is None:
            self.fail("The comparison result omitted the budget evaluation.")
        self.assertEqual(evaluations[1].status, "unknown")
        self.assertEqual(evaluations[1].reason, "currency_mismatch")
        self.assertIn(
            "currencies differ; budget check is unknown",
            format_comparison_chat_reply(comparison_result),
        )
        self.assertNotIn(
            "Conditional value guidance",
            format_comparison_chat_reply(comparison_result),
        )

    def test_budget_currency_cannot_be_inferred_from_a_symbol(self):
        context, search, compare = self._chat_tools(
            user_messages=("Compare these products under $20.",)
        )
        discovery = json.loads(search.invoke({"query": "honey"}))
        references = [
            offer["reference"] for offer in discovery["offers"][:2]
        ]

        response = json.loads(compare.invoke({
            "candidates": references,
            "budget": {"amount": "20", "currency": "USD"},
        }))

        self.assertEqual(response["status"], "clarification_required")
        self.assertIn("ISO currency code", response["message"])
        self.assertIsNone(context.comparison_result)

    def test_api_uses_deterministic_result_instead_of_ai_override(self):
        captured = {}
        test_case = self

        class FakeAgent:
            def __init__(self, tools):
                self.tools = {tool.name: tool for tool in tools if hasattr(tool, "name")}

            def invoke(self, request):
                test_case.assertEqual(
                    request["messages"][-1]["role"],
                    "user",
                )
                discovery = json.loads(
                    self.tools["search_discovery_products"].invoke(
                        {"query": "honey"}
                    )
                )
                references = [
                    offer["reference"] for offer in discovery["offers"][:2]
                ]
                captured["references"] = references
                self.tools["compare_products"].invoke({
                    "candidates": references,
                    "must_have": [
                        {"attribute": "is_organic", "expected_value": True},
                    ],
                })
                return {
                    "messages": [
                        SimpleNamespace(
                            type="ai",
                            content="Flower Honey is best and has a 5-star rating.",
                        )
                    ]
                }

        def fake_create_agent(**configuration):
            tool_names = {
                tool.name for tool in configuration["tools"] if hasattr(tool, "name")
            }
            captured["tool_names"] = tool_names
            return FakeAgent(configuration["tools"])

        fake_module = self._fake_shopping_agent_module()
        with (
            patch.dict(sys.modules, {"shopping_agent": fake_module}),
            patch("langchain.agents.create_agent", side_effect=fake_create_agent),
        ):
            response = self.client.post(
                "/api/chat",
                json={
                    "messages": [
                        {"role": "user", "content": "Compare these honey products"}
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            captured["tool_names"],
            {
                "search_products",
                "list_categories",
                "get_rating",
                "get_batch_ratings",
                "search_discovery_products",
                "compare_products",
                "get_selected_products",
            },
        )
        self.assertEqual(
            captured["references"],
            [
                {"provider_key": "local_catalog", "provider_product_id": "1"},
                {"provider_key": "local_catalog", "provider_product_id": "2"},
            ],
        )
        self.assertIn(
            "Organic Honey is the only candidate that met the supplied requirements",
            response.json()["reply"],
        )
        self.assertNotIn("Flower Honey is best", response.json()["reply"])
        self.assertNotIn("5-star rating", response.json()["reply"])

    def test_chat_asks_for_products_when_comparison_has_no_identifiable_candidates(self):
        fake_module = self._fake_shopping_agent_module()

        def fake_create_agent(**configuration):
            tool_names = {
                tool.name
                for tool in configuration["tools"]
                if hasattr(tool, "name")
            }
            self.assertIn("compare_products", tool_names)

            def invoke(request):
                self.assertTrue(request["messages"])
                return {
                    "messages": [
                        SimpleNamespace(
                            type="ai",
                            content="Product 1 is definitely the best.",
                        )
                    ]
                }

            return SimpleNamespace(
                invoke=invoke
            )

        with (
            patch.dict(sys.modules, {"shopping_agent": fake_module}),
            patch("langchain.agents.create_agent", side_effect=fake_create_agent),
        ):
            response = self.client.post(
                "/api/chat",
                json={
                    "messages": [
                        {"role": "user", "content": "Which one should I buy?"}
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        reply = response.json()["reply"]
        self.assertIn("Which two or more products", reply)
        self.assertNotIn("definitely the best", reply)

    def test_existing_read_only_chat_response_and_cart_tool_remain_available(self):
        fake_module = self._fake_shopping_agent_module()
        captured = {}

        def fake_create_agent(**configuration):
            captured["tools"] = configuration["tools"]
            captured["system_prompt"] = configuration["system_prompt"]

            def invoke(request):
                self.assertTrue(request["messages"])
                return {
                    "messages": [
                        SimpleNamespace(type="ai", content="Existing chat response.")
                    ]
                }

            return SimpleNamespace(
                invoke=invoke
            )

        with (
            patch.dict(sys.modules, {"shopping_agent": fake_module}),
            patch("langchain.agents.create_agent", side_effect=fake_create_agent),
        ):
            read_only_agent = self.main._get_read_only_chat_agent()
            cart_agent = self.main._get_authenticated_cart_chat_agent(
                "chat-test-owner",
                "Add Organic Honey to my cart",
                comparison_context=ComparisonChatContext(),
            )

        self.assertIsNotNone(read_only_agent)
        self.assertIsNotNone(cart_agent)
        tool_names = {
            tool.name for tool in captured["tools"] if hasattr(tool, "name")
        }
        self.assertIn("add_product_to_cart", tool_names)
        self.assertIn("compare_products", tool_names)
        self.assertIn("search_discovery_products", tool_names)
        self.assertIn("deterministic comparison result", captured["system_prompt"])


if __name__ == "__main__":
    unittest.main()
