import importlib
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from backend.tests.test_cart_api import CartApiFixture, main


class ShoppingAgentCartTests(CartApiFixture):
    def _load_shopping_agent(self):
        from backend.visual_search import groq_client

        with (
            patch("dotenv.load_dotenv"),
            patch.object(groq_client, "load_dotenv"),
            patch.dict(os.environ, {"GROQ_API_KEY": "test-key-for-unit-tests"}),
        ):
            shopping_agent = importlib.import_module("shopping_agent")

        original_db_path = shopping_agent.DB_PATH
        shopping_agent.DB_PATH = str(self.store_path)
        self.addCleanup(setattr, shopping_agent, "DB_PATH", original_db_path)
        return shopping_agent

    def test_authenticated_ai_add_tool_requires_exact_catalog_name(self):
        def capture_agent(**configuration):
            return SimpleNamespace(tools=configuration["tools"])

        with patch("langchain.agents.create_agent", side_effect=capture_agent):
            agent = main._get_authenticated_cart_chat_agent(
                "test-owner-a",
                "Add Organic Raw Honey to my cart",
            )

        add_tool = next(
            tool for tool in agent.tools if tool.name == "add_product_to_cart"
        )
        self.assertNotIn("product_id", add_tool.args)

        rejected = add_tool.invoke({
            "product_name": "Wildflower Honey",
            "quantity": 1,
        })
        self.assertIn("No item was added", rejected)

        added = add_tool.invoke({
            "product_name": "Organic Raw Honey",
            "quantity": 2,
        })
        self.assertIn("Added 2 × Organic Raw Honey", added)
        cart = self.client.get(
            "/api/cart",
            cookies=self._cookies(self.owner_a),
        ).json()
        self.assertEqual(cart["items"][0]["product_id"], 1)
        self.assertEqual(cart["items"][0]["quantity"], 2)

    def test_existing_streamlit_cart_context_remains_request_local(self):
        shopping_agent = self._load_shopping_agent()
        cart_a = {}
        cart_b = {}
        with shopping_agent.use_cart(cart_a):
            self.assertIs(shopping_agent._get_active_cart(), cart_a)
            with shopping_agent.use_cart(cart_b):
                self.assertIs(shopping_agent._get_active_cart(), cart_b)
            self.assertIs(shopping_agent._get_active_cart(), cart_a)

        with self.assertRaises(RuntimeError):
            shopping_agent._get_active_cart()

        barrier = Barrier(2)

        def read_bound_cart(product_id):
            session_cart = {product_id: product_id}
            with shopping_agent.use_cart(session_cart):
                barrier.wait(timeout=5)
                return shopping_agent._get_active_cart() is session_cart

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(read_bound_cart, (1, 2)))
        self.assertEqual(results, [True, True])

        with shopping_agent.use_cart(cart_a):
            result = shopping_agent.add_to_cart.invoke({
                "product_id": 1,
                "quantity": 2,
            })
        self.assertIn("Added 2x 'Organic Raw Honey'", result)
        self.assertEqual(cart_a, {1: 2})

    def test_checkout_snapshots_known_and_unknown_currency_without_changing_amount(self):
        shopping_agent = self._load_shopping_agent()
        with (
            shopping_agent.use_order_owner("test-owner-a"),
            shopping_agent.use_cart({1: 1}),
        ):
            known_result = shopping_agent.checkout_cart.invoke({})
        self.assertIn("USD 14.99", known_result)

        with (
            shopping_agent.use_order_owner("test-owner-a"),
            shopping_agent.use_cart({3: 1}),
        ):
            unknown_result = shopping_agent.checkout_cart.invoke({})
        self.assertIn("currency is unverified", unknown_result)

        with closing(sqlite3.connect(self.store_path)) as connection:
            orders = connection.execute(
                "SELECT price, currency FROM orders ORDER BY id"
            ).fetchall()
        self.assertEqual(orders, [(14.99, "USD"), (5.49, None)])


if __name__ == "__main__":
    unittest.main()
