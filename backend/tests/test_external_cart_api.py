import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence, cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.cart.service import ExternalOfferCartService
from backend.cart.models import OfferAvailability
from backend.migrations import v002_external_cart_quotes
from backend.providers import (
    LocalCatalogAdapter,
    NormalizedOffer,
    ProviderAdapter,
    ProviderRegistry,
)
from backend.providers.base import (
    PriceInput,
    ProviderCapacityError,
    ProviderOfferNotFoundError,
    ProviderResolutionTimeoutError,
    ProviderUnavailableError,
    ResolvedProviderProduct,
)
from backend.tests._support import (
    import_main_with_temporary_auth_db,
    prepare_test_schemas,
)
from backend.tests.test_external_cart import (
    _create_v001_database,
    _insert_offer,
    _timestamp_ago,
)


_ALLOWED_ORIGIN = "http://localhost:5174"


def _utc_timestamp(*, minutes_ago: int = 0) -> str:
    value = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalized_offer(
    *,
    offer_id: str,
    provider_offer_key: str,
    provider_product_id: str,
    product_name: str = "Provider resolved item",
    price_minor: int | None = 2499,
    currency: str | None = "USD",
    product_url: str | None = "https://shop.example/items/offer",
    availability: str = "in_stock",
    verified_at: str | None = None,
    provider_key: str = "test_market",
) -> NormalizedOffer:
    timestamp = verified_at or _utc_timestamp()
    return NormalizedOffer(
        offer_id=offer_id,
        provider_key=provider_key,
        provider_offer_key=provider_offer_key,
        provider_product_id=provider_product_id,
        canonical_product_id=None,
        product_name=product_name,
        brand=None,
        model=None,
        price_minor=price_minor,
        currency=currency,
        product_url=product_url,
        image_url=None,
        availability=cast(OfferAvailability, availability),
        last_checked_at=timestamp,
        expires_at=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


class FakeExactOfferProvider(ProviderAdapter):
    def __init__(self) -> None:
        self.offers: dict[str, NormalizedOffer | Exception] = {}
        self.resolve_calls: list[str] = []

    @property
    def provider_key(self) -> str:
        return "test_market"

    @property
    def display_name(self) -> str:
        return "Temporary test provider"

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
        raise ProviderUnavailableError(self.provider_key, "Not used in this test.")

    def resolve_offer(self, provider_offer_key: str) -> NormalizedOffer:
        self.resolve_calls.append(provider_offer_key)
        result = self.offers.get(provider_offer_key)
        if result is None:
            raise ProviderOfferNotFoundError(
                self.provider_key,
                "The exact test offer was not found.",
            )
        if isinstance(result, Exception):
            raise result
        return result


class ExternalCartApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = import_main_with_temporary_auth_db()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.store_path = self.temp_path / "store.sqlite"
        self.auth_path = self.temp_path / "auth.sqlite"
        _create_v001_database(self.store_path)
        v002_external_cart_quotes.migrate(self.store_path)

        self.main = self.__class__.main
        self.original_store_path = self.main.STORE_DB_PATH
        self.original_auth_path = self.main.AUTH_DB_PATH
        self.original_registry = self.main.PROVIDER_REGISTRY
        self.original_dependency_overrides = dict(
            self.main.app.dependency_overrides
        )
        self.main.STORE_DB_PATH = self.store_path
        self.main.AUTH_DB_PATH = self.auth_path

        self.provider = FakeExactOfferProvider()
        self.registry = ProviderRegistry(
            [
                LocalCatalogAdapter(self.store_path),
                self.provider,
            ]
        )
        self.main.PROVIDER_REGISTRY = self.registry
        prepare_test_schemas(self.store_path, self.auth_path)

        self.client = TestClient(self.main.app, raise_server_exceptions=False)
        with patch.object(
            self.main,
            "create_vision_analyzer",
            side_effect=ValueError("not used by this test"),
        ):
            self.client.__enter__()

        authentication_engine = self.main.app.state.database_engines.authentication
        self.owner_a = self.main.create_session(
            "external-cart-owner-a",
            authentication_engine,
        )
        self.owner_b = self.main.create_session(
            "external-cart-owner-b",
            authentication_engine,
        )

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.main.app.dependency_overrides.clear()
        self.main.app.dependency_overrides.update(
            self.original_dependency_overrides
        )
        self.main.STORE_DB_PATH = self.original_store_path
        self.main.AUTH_DB_PATH = self.original_auth_path
        self.main.PROVIDER_REGISTRY = self.original_registry
        self.temp_dir.cleanup()

    def _add_offer(
        self,
        *,
        key: str = "market-offer-1",
        product_id: str = "market-product-1",
        price_minor: int | None = 2499,
        currency: str | None = "USD",
        availability: str = "in_stock",
        checked_at: str | None = None,
        provider_offer_key: str | None = None,
    ) -> tuple[str, str]:
        offer_key = provider_offer_key or key
        offer_id = _insert_offer(
            self.store_path,
            provider_offer_key=offer_key,
            provider_product_id=product_id,
            price_minor=price_minor,
            currency=currency,
            availability=availability,
            checked_at=checked_at,
        )
        self.provider.offers[offer_key] = _normalized_offer(
            offer_id=offer_id,
            provider_offer_key=offer_key,
            provider_product_id=product_id,
            price_minor=price_minor,
            currency=currency,
            availability=availability,
            verified_at=checked_at,
        )
        return offer_id, offer_key

    def _request(
        self,
        method: str,
        path: str,
        *,
        owner: str | None = None,
        json_body: object = None,
        headers: dict[str, str] | None = None,
        origin: str | None = _ALLOWED_ORIGIN,
    ):
        request_headers = dict(headers or {})
        if origin is not None:
            request_headers.setdefault("Origin", origin)
        self.client.cookies.clear()
        if owner is not None:
            self.client.cookies.set(self.main.SESSION_COOKIE_NAME, owner)
        return self.client.request(
            method,
            path,
            json=json_body,
            headers=request_headers,
        )

    def _add(
        self,
        offer_id: str,
        *,
        owner: str | None = None,
        quantity: int = 1,
        headers: dict[str, str] | None = None,
        origin: str | None = _ALLOWED_ORIGIN,
    ):
        return self._request(
            "POST",
            "/api/cart/external/items",
            owner=owner or self.owner_a,
            json_body={"offer_id": offer_id, "quantity": quantity},
            headers=headers,
            origin=origin,
        )

    def _cart_item(self, response, index: int = 0) -> dict[str, object]:
        return response.json()["items"][index]

    def test_all_external_cart_routes_require_authentication(self):
        offer_id, _ = self._add_offer()
        requests = (
            ("GET", "/api/cart/external", None),
            ("POST", "/api/cart/external/items", {
                "offer_id": offer_id, "quantity": 1,
            }),
            ("PATCH", f"/api/cart/external/items/{offer_id}", {"quantity": 2}),
            ("DELETE", f"/api/cart/external/items/{offer_id}", None),
            ("DELETE", "/api/cart/external", None),
            ("POST", f"/api/cart/external/items/{offer_id}/refresh", None),
            (
                "POST",
                f"/api/cart/external/items/{offer_id}/confirm-price",
                {"confirm": True},
            ),
        )
        responses = [
            self._request(method, path, json_body=body)
            for method, path, body in requests
        ]
        self.assertEqual([response.status_code for response in responses], [401] * 7)

    def test_get_requires_auth_but_does_not_require_origin(self):
        response = self._request(
            "GET",
            "/api/cart/external",
            owner=self.owner_a,
            origin=None,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "currency_totals": []})

    def test_owner_isolation_blocks_read_and_all_item_mutations(self):
        offer_id, _ = self._add_offer()
        added = self._add(offer_id, quantity=2)
        self.assertEqual(added.status_code, 200)

        self.assertEqual(
            self._request("GET", "/api/cart/external", owner=self.owner_b).json()[
                "items"
            ],
            [],
        )
        denied = (
            self._request(
                "PATCH",
                f"/api/cart/external/items/{offer_id}",
                owner=self.owner_b,
                json_body={"quantity": 3},
            ),
            self._request(
                "DELETE",
                f"/api/cart/external/items/{offer_id}",
                owner=self.owner_b,
            ),
            self._request(
                "POST",
                f"/api/cart/external/items/{offer_id}/refresh",
                owner=self.owner_b,
            ),
            self._request(
                "POST",
                f"/api/cart/external/items/{offer_id}/confirm-price",
                owner=self.owner_b,
                json_body={"confirm": True},
            ),
        )
        self.assertEqual([response.status_code for response in denied], [404] * 4)
        self.assertEqual(
            self._cart_item(
                self._request("GET", "/api/cart/external", owner=self.owner_a)
            )["quantity"],
            2,
        )

        self._request("DELETE", "/api/cart/external", owner=self.owner_b)
        self.assertEqual(
            len(
                self._request(
                    "GET",
                    "/api/cart/external",
                    owner=self.owner_a,
                ).json()["items"]
            ),
            1,
        )

    def test_mutations_require_allowed_origin_or_referer(self):
        offer_id, _ = self._add_offer()
        missing = self._add(offer_id, headers={}, origin=None)
        disallowed = self._add(
            offer_id,
            headers={},
            origin="https://attacker.example",
        )
        invalid_referer = self._request(
            "DELETE",
            "/api/cart/external",
            owner=self.owner_a,
            headers={"Referer": "not a URL"},
            origin=None,
        )
        self.assertEqual(
            [missing.status_code, disallowed.status_code, invalid_referer.status_code],
            [403, 403, 403],
        )

        allowed_referer = self._add(
            offer_id,
            headers={"Referer": f"{_ALLOWED_ORIGIN}/cart"},
            origin=None,
        )
        self.assertEqual(allowed_referer.status_code, 200)

    def test_valid_add_uses_only_provider_resolved_quote_data(self):
        offer_id, offer_key = self._add_offer()
        resolved_at = _utc_timestamp()
        self.provider.offers[offer_key] = _normalized_offer(
            offer_id=offer_id,
            provider_offer_key=offer_key,
            provider_product_id="market-product-1",
            product_name="Trusted provider title",
            price_minor=3100,
            currency="EUR",
            product_url="https://shop.example/trusted-listing",
            availability="preorder",
            verified_at=resolved_at,
        )

        response = self._add(offer_id, quantity=2)

        self.assertEqual(response.status_code, 200)
        item = self._cart_item(response)
        self.assertEqual(item["product_name"], "Trusted provider title")
        self.assertEqual(item["price_minor"], 3100)
        self.assertEqual(item["currency"], "EUR")
        self.assertEqual(item["product_url"], "https://shop.example/trusted-listing")
        self.assertEqual(item["availability"], "preorder")
        self.assertEqual(item["verified_at"], resolved_at)
        self.assertEqual(item["provider_key"], "test_market")
        self.assertEqual(item["provider_offer_key"], offer_key)
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(self.provider.resolve_calls, [offer_key])

    def test_unexpected_client_quote_fields_are_rejected(self):
        offer_id, _ = self._add_offer()
        untrusted_fields = {
            "price_minor": 1,
            "currency": "INR",
            "product_name": "Client title",
            "product_url": "https://attacker.example/item",
            "availability": "in_stock",
            "provider_key": "client_provider",
            "provider_offer_key": "client_offer",
            "provider_product_id": "client_product",
            "verified_at": _utc_timestamp(),
        }
        for field, value in untrusted_fields.items():
            with self.subTest(field=field):
                response = self._request(
                    "POST",
                    "/api/cart/external/items",
                    owner=self.owner_a,
                    json_body={
                        "offer_id": offer_id,
                        "quantity": 1,
                        field: value,
                    },
                )
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.provider.resolve_calls, [])

    def test_valid_quantity_update_remove_and_clear(self):
        first_id, _ = self._add_offer(key="first")
        second_id, _ = self._add_offer(
            key="second",
            product_id="market-product-2",
        )
        self.assertEqual(self._add(first_id).status_code, 200)
        self.assertEqual(self._add(second_id, quantity=2).status_code, 200)

        updated = self._request(
            "PATCH",
            f"/api/cart/external/items/{first_id}",
            owner=self.owner_a,
            json_body={"quantity": 4},
        )
        self.assertEqual(updated.status_code, 200)
        updated_first_items = [
            item for item in updated.json()["items"] if item["offer_id"] == first_id
        ]
        self.assertEqual(len(updated_first_items), 1)
        self.assertEqual(updated_first_items[0]["quantity"], 4)

        removed = self._request(
            "DELETE",
            f"/api/cart/external/items/{first_id}",
            owner=self.owner_a,
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(
            [item["offer_id"] for item in removed.json()["items"]],
            [second_id],
        )

        cleared = self._request(
            "DELETE",
            "/api/cart/external",
            owner=self.owner_a,
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json(), {"items": [], "currency_totals": []})

    def test_unexpected_fields_and_invalid_quantities_are_rejected(self):
        offer_id, _ = self._add_offer()
        self.assertEqual(
            self._request(
                "POST",
                "/api/cart/external/items",
                owner=self.owner_a,
                json_body={"offer_id": offer_id, "quantity": 1, "extra": True},
            ).status_code,
            422,
        )
        self.assertEqual(self._add(offer_id).status_code, 200)

        for quantity in (0, -1, True, 1.5, "2"):
            with self.subTest(quantity=quantity):
                response = self._request(
                    "PATCH",
                    f"/api/cart/external/items/{offer_id}",
                    owner=self.owner_a,
                    json_body={"quantity": quantity},
                )
                self.assertEqual(response.status_code, 422)

        self.assertEqual(
            self._request(
                "PATCH",
                f"/api/cart/external/items/{offer_id}",
                owner=self.owner_a,
                json_body={"quantity": 2, "price_minor": 1},
            ).status_code,
            422,
        )
        self.assertEqual(
            self._request(
                "POST",
                f"/api/cart/external/items/{offer_id}/confirm-price",
                owner=self.owner_a,
                json_body={"confirm": True, "price_minor": 1},
            ).status_code,
            422,
        )
        self.assertEqual(
            self._request(
                "POST",
                f"/api/cart/external/items/{offer_id}/confirm-price",
                owner=self.owner_a,
                json_body={"confirm": False},
            ).status_code,
            422,
        )

    def test_malformed_offer_ids_are_rejected(self):
        whitespace = self._request(
            "POST",
            "/api/cart/external/items",
            owner=self.owner_a,
            json_body={"offer_id": "   ", "quantity": 1},
        )
        path_value = self._request(
            "PATCH",
            "/api/cart/external/items/%20%20",
            owner=self.owner_a,
            json_body={"quantity": 1},
        )
        self.assertEqual(whitespace.status_code, 422)
        self.assertEqual(path_value.status_code, 422)

    def test_exact_offer_mismatches_and_unknown_offer_are_rejected_safely(self):
        bad_key_id, bad_key = self._add_offer(key="wrong-key")
        self.provider.offers[bad_key] = _normalized_offer(
            offer_id=bad_key_id,
            provider_offer_key="different-key",
            provider_product_id="market-product-1",
        )
        wrong_key_response = self._add(bad_key_id)
        self.assertEqual(wrong_key_response.status_code, 404)

        bad_product_id, bad_product_key = self._add_offer(
            key="wrong-product",
            product_id="market-product-2",
        )
        self.provider.offers[bad_product_key] = _normalized_offer(
            offer_id=bad_product_id,
            provider_offer_key=bad_product_key,
            provider_product_id="different-product",
        )
        wrong_product_response = self._add(bad_product_id)
        self.assertEqual(wrong_product_response.status_code, 409)

        bad_provider_id, bad_provider_key = self._add_offer(key="wrong-provider")
        self.provider.offers[bad_provider_key] = _normalized_offer(
            offer_id=bad_provider_id,
            provider_offer_key=bad_provider_key,
            provider_product_id="market-product-1",
            provider_key="other_provider",
        )
        wrong_provider_response = self._add(bad_provider_id)
        self.assertIn(wrong_provider_response.status_code, (404, 503))

        unknown = self._add(str(uuid.uuid4()))
        self.assertEqual(unknown.status_code, 404)
        self.assertNotIn(str(self.store_path), wrong_key_response.text)
        self.assertNotIn(str(self.store_path), wrong_product_response.text)

    def test_local_catalog_offer_is_not_eligible_for_external_purchase(self):
        offer_id = str(uuid.uuid4())
        timestamp = _utc_timestamp()
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute(
                """
                INSERT INTO product_offers (
                    offer_id, provider_key, provider_offer_key,
                    provider_product_id, canonical_product_id, product_name,
                    brand, model, price_minor, currency, product_url, image_url,
                    availability, last_checked_at, expires_at, created_at, updated_at
                ) VALUES (?, 'local_catalog', 'local-product:55', '55', NULL,
                          'Local product', NULL, NULL, 1000, 'USD', NULL, NULL,
                          'in_stock', ?, NULL, ?, ?)
                """,
                (offer_id, timestamp, timestamp, timestamp),
            )
            connection.commit()

        response = self._add(offer_id)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.provider.resolve_calls, [])
        self.assertIn("unavailable", response.json()["detail"].lower())

    def test_stale_unavailable_and_unknown_price_quotes_cannot_be_added(self):
        stale_id, _ = self._add_offer(
            key="stale",
            checked_at=_timestamp_ago(6),
        )
        unavailable_id, _ = self._add_offer(
            key="unavailable",
            product_id="market-product-2",
            availability="out_of_stock",
        )
        unknown_price_id, _ = self._add_offer(
            key="unknown-price",
            product_id="market-product-3",
            price_minor=None,
            currency=None,
        )

        responses = (
            self._add(stale_id),
            self._add(unavailable_id),
            self._add(unknown_price_id),
        )
        self.assertEqual([response.status_code for response in responses], [409] * 3)
        self.assertEqual(
            self._request("GET", "/api/cart/external", owner=self.owner_a).json()[
                "items"
            ],
            [],
        )

    def test_unknown_currency_is_rejected_before_it_can_become_a_cart_quote(self):
        with self.assertRaises(ValueError):
            _normalized_offer(
                offer_id=str(uuid.uuid4()),
                provider_offer_key="invalid-currency",
                provider_product_id="product",
                price_minor=1000,
                currency="ZZZ",
            )

        offer_id, _ = self._add_offer(
            key="missing-currency",
            product_id="market-product-4",
            price_minor=None,
            currency=None,
        )
        response = self._add(offer_id)
        self.assertEqual(response.status_code, 409)

    def test_refresh_unchanged_quote_stays_current_without_incrementing_quantity(self):
        offer_id, offer_key = self._add_offer()
        added = self._add(offer_id, quantity=3)
        self.assertEqual(added.status_code, 200)
        new_verified_at = _utc_timestamp()
        self.provider.offers[offer_key] = _normalized_offer(
            offer_id=offer_id,
            provider_offer_key=offer_key,
            provider_product_id="market-product-1",
            verified_at=new_verified_at,
        )

        refreshed = self._request(
            "POST",
            f"/api/cart/external/items/{offer_id}/refresh",
            owner=self.owner_a,
        )

        self.assertEqual(refreshed.status_code, 200)
        item = self._cart_item(refreshed)
        self.assertEqual(item["quantity"], 3)
        self.assertEqual(item["price_minor"], 2499)
        self.assertEqual(item["quote_status"], "verified_current")
        self.assertEqual(item["verified_at"], new_verified_at)

    def test_changed_price_is_staged_and_explicit_confirmation_uses_provider_time(self):
        offer_id, offer_key = self._add_offer()
        self.assertEqual(self._add(offer_id, quantity=3).status_code, 200)
        changed_verified_at = _utc_timestamp()
        self.provider.offers[offer_key] = _normalized_offer(
            offer_id=offer_id,
            provider_offer_key=offer_key,
            provider_product_id="market-product-1",
            price_minor=3299,
            verified_at=changed_verified_at,
        )

        refreshed = self._request(
            "POST",
            f"/api/cart/external/items/{offer_id}/refresh",
            owner=self.owner_a,
        )
        self.assertEqual(refreshed.status_code, 200)
        staged = self._cart_item(refreshed)
        self.assertEqual(staged["quote_status"], "price_change_pending")
        self.assertEqual(staged["price_minor"], 2499)
        self.assertEqual(staged["pending_price_minor"], 3299)
        self.assertEqual(staged["quantity"], 3)

        repeated_refresh = self._request(
            "POST",
            f"/api/cart/external/items/{offer_id}/refresh",
            owner=self.owner_a,
        )
        self.assertEqual(repeated_refresh.status_code, 409)
        saved = self._request(
            "GET",
            "/api/cart/external",
            owner=self.owner_a,
        )
        self.assertEqual(self._cart_item(saved)["price_minor"], 2499)

        confirmed = self._request(
            "POST",
            f"/api/cart/external/items/{offer_id}/confirm-price",
            owner=self.owner_a,
            json_body={"confirm": True},
        )
        self.assertEqual(confirmed.status_code, 200)
        selected = self._cart_item(confirmed)
        self.assertEqual(selected["price_minor"], 3299)
        self.assertEqual(selected["verified_at"], changed_verified_at)
        self.assertEqual(selected["quote_status"], "verified_current")
        self.assertEqual(selected["quantity"], 3)

    def test_expired_pending_quote_confirmation_remains_stale(self):
        offer_id, offer_key = self._add_offer()
        self.assertEqual(self._add(offer_id).status_code, 200)
        new_verified_at = _utc_timestamp()
        self.provider.offers[offer_key] = _normalized_offer(
            offer_id=offer_id,
            provider_offer_key=offer_key,
            provider_product_id="market-product-1",
            price_minor=3999,
            verified_at=new_verified_at,
        )
        staged = self._request(
            "POST",
            f"/api/cart/external/items/{offer_id}/refresh",
            owner=self.owner_a,
        )
        self.assertEqual(staged.status_code, 200)

        expired_at = _timestamp_ago(6)
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute(
                """
                UPDATE cart_offer_items
                SET pending_verified_at = ?
                WHERE owner_id = ? AND offer_id = ?
                """,
                (expired_at, self._owner_id(self.owner_a), offer_id),
            )
            connection.execute(
                "UPDATE product_offers SET last_checked_at = ? WHERE offer_id = ?",
                (expired_at, offer_id),
            )
            connection.commit()

        confirmed = self._request(
            "POST",
            f"/api/cart/external/items/{offer_id}/confirm-price",
            owner=self.owner_a,
            json_body={"confirm": True},
        )

        self.assertEqual(confirmed.status_code, 200)
        selected = self._cart_item(confirmed)
        self.assertEqual(selected["price_minor"], 3999)
        self.assertEqual(selected["verified_at"], expired_at)
        self.assertEqual(selected["quote_status"], "stale_unverified")

    def _owner_id(self, session_token: str) -> str:
        owner_id = self.main.get_session_owner(
            session_token,
            self.main.app.state.database_engines.authentication,
        )
        if owner_id is None:
            raise AssertionError("The test session has no owner.")
        return owner_id

    def test_provider_failures_are_sanitized(self):
        failures = (
            (
                "unavailable",
                ProviderUnavailableError("test_market", "private provider secret"),
                503,
            ),
            (
                "timeout",
                ProviderResolutionTimeoutError("test_market", "private timeout detail"),
                504,
            ),
            (
                "capacity",
                ProviderCapacityError("test_market", "private capacity detail"),
                503,
            ),
        )
        for key, error, expected_status in failures:
            with self.subTest(error=key):
                offer_id, offer_key = self._add_offer(
                    key=f"failure-{key}",
                    product_id=f"failure-product-{key}",
                )
                self.provider.offers[offer_key] = error
                response = self._add(offer_id)
                self.assertEqual(response.status_code, expected_status)
                self.assertNotIn("private", response.text)
                self.assertNotIn(str(self.store_path), response.text)

    def test_missing_cart_schema_returns_sanitized_service_unavailable(self):
        empty_database = self.temp_path / "missing-cart-schema.sqlite"
        sqlite3.connect(empty_database).close()
        self.main.app.dependency_overrides[
            self.main.get_external_cart_service
        ] = lambda: ExternalOfferCartService(empty_database)
        try:
            response = self._request(
                "GET",
                "/api/cart/external",
                owner=self.owner_a,
                origin=None,
            )
        finally:
            self.main.app.dependency_overrides.pop(
                self.main.get_external_cart_service,
                None,
            )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn(str(empty_database), response.text)
        self.assertNotIn("CREATE TABLE", response.text)

    def test_existing_local_cart_read_contract_remains_separate(self):
        owner_id = self._owner_id(self.owner_a)
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute(
                "UPDATE products SET currency = 'USD' WHERE id = 1"
            )
            connection.execute(
                """
                INSERT INTO cart_items (owner_id, product_id, quantity)
                VALUES (?, 1, 2)
                """,
                (owner_id,),
            )
            connection.commit()

        local_cart = self._request(
            "GET",
            "/api/cart",
            owner=self.owner_a,
            origin=None,
        )
        external_cart = self._request(
            "GET",
            "/api/cart/external",
            owner=self.owner_a,
            origin=None,
        )

        self.assertEqual(local_cart.status_code, 200)
        self.assertEqual(
            set(local_cart.json()),
            {"items", "subtotal", "currency", "item_count"},
        )
        self.assertEqual(local_cart.json()["items"][0]["quantity"], 2)
        self.assertEqual(local_cart.json()["currency"], "USD")
        self.assertEqual(external_cart.status_code, 200)
        self.assertEqual(external_cart.json()["items"], [])


if __name__ == "__main__":
    unittest.main()
