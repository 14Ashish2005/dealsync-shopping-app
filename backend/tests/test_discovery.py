import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

from backend.discovery import DiscoveryEngine, group_offers
from backend.discovery.matcher import summarize_prices
from backend.product_attributes import ProductAttribute
from backend.providers.base import NormalizedOffer, PriceInput, ProviderAdapter
from backend.providers.registry import ProviderRegistry


def make_offer(
    provider_key="provider_a",
    product_id="1",
    *,
    offer_id=None,
    canonical_product_id=None,
    product_name="Organic Honey",
    brand=None,
    model=None,
    product_attributes=None,
    price_minor=1000,
    currency="USD",
):
    return NormalizedOffer(
        offer_id=offer_id or f"{provider_key}-{product_id}",
        provider_key=provider_key,
        provider_offer_key=f"offer:{product_id}",
        provider_product_id=product_id,
        canonical_product_id=canonical_product_id,
        product_name=product_name,
        brand=brand,
        model=model,
        price_minor=price_minor,
        currency=currency,
        product_url=None,
        image_url=None,
        availability="unknown",
        last_checked_at="2026-01-01T00:00:00Z",
        expires_at=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        product_attributes=product_attributes,
    )


class FakeProvider(ProviderAdapter):
    def __init__(self, key, search_function):
        self._key = key
        self._search_function = search_function

    @property
    def provider_key(self):
        return self._key

    @property
    def display_name(self):
        return self._key

    def search(
        self,
        query="",
        *,
        max_price: PriceInput | None = None,
        currency=None,
        is_organic=None,
    ):
        return self._search_function(query, max_price, currency, is_organic)

    def normalize_offer(self, product):
        return product


class FilterSupportingProvider(FakeProvider):
    def __init__(self, key, search_function):
        super().__init__(key, search_function)
        self.received_filters = None

    @property
    def search_filter_capabilities(self):
        return frozenset({"min_price", "max_price", "category"})

    def search(
        self,
        query="",
        *,
        max_price: PriceInput | None = None,
        min_price: PriceInput | None = None,
        currency=None,
        is_organic=None,
        category=None,
    ):
        self.received_filters = {
            "min_price": min_price,
            "max_price": max_price,
            "currency": currency,
            "category": category,
        }
        return super().search(
            query,
            max_price=max_price,
            currency=currency,
            is_organic=is_organic,
        )


class DiscoveryMatcherTests(unittest.TestCase):
    def test_only_exact_verified_identity_groups_across_providers(self):
        offers = [
            make_offer(
                "provider_a",
                "a-1",
                canonical_product_id="canonical-1",
                product_name="Organic Raw Honey",
            ),
            make_offer(
                "provider_b",
                "b-9",
                canonical_product_id="canonical-1",
                product_name=" organic   raw honey ",
            ),
        ]

        groups = group_offers(offers)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].canonical_product_id, "canonical-1")
        self.assertEqual(len(groups[0].offers), 2)

    def test_similar_titles_without_canonical_identity_remain_separate(self):
        groups = group_offers(
            [
                make_offer("local_catalog", "1", product_name="Organic Honey"),
                make_offer("local_catalog", "2", product_name="organic honey"),
            ]
        )

        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(group.offers) == 1 for group in groups))

    def test_group_order_follows_first_occurrence(self):
        offers = [
            make_offer("local_catalog", "1"),
            make_offer(
                "provider_a",
                "a-1",
                canonical_product_id="canonical-1",
            ),
            make_offer("local_catalog", "2"),
            make_offer(
                "provider_b",
                "b-9",
                canonical_product_id="canonical-1",
            ),
        ]

        groups = group_offers(offers)

        self.assertEqual(
            [group.offers[0].provider_product_id for group in groups],
            ["1", "a-1", "2"],
        )

    def test_same_provider_ids_and_variant_conflicts_do_not_merge(self):
        same_provider = group_offers(
            [
                make_offer(
                    "provider_a",
                    "1",
                    canonical_product_id="canonical-1",
                ),
                make_offer(
                    "provider_a",
                    "2",
                    canonical_product_id="canonical-1",
                ),
            ]
        )
        variant_conflict = group_offers(
            [
                make_offer(
                    "provider_a",
                    "1",
                    canonical_product_id="canonical-2",
                    product_name="Phone 128GB",
                    model="128GB",
                ),
                make_offer(
                    "provider_b",
                    "2",
                    canonical_product_id="canonical-2",
                    product_name="Phone 256GB",
                    model="256GB",
                ),
            ]
        )

        self.assertEqual(len(same_provider), 2)
        self.assertEqual(len(variant_conflict), 2)

    def test_structured_storage_brand_and_model_conflicts_do_not_merge(self):
        conflicts = (
            (
                "storage_capacity",
                ProductAttribute("storage_capacity", 64, "verified_provider", unit="GB"),
                ProductAttribute("storage_capacity", 128, "verified_provider", unit="GB"),
            ),
            (
                "brand",
                ProductAttribute("brand", "Acme", "verified_provider"),
                ProductAttribute("brand", "Other", "verified_provider"),
            ),
            (
                "model",
                ProductAttribute("model", "M1", "verified_provider"),
                ProductAttribute("model", "M2", "verified_provider"),
            ),
        )

        for name, first_attribute, second_attribute in conflicts:
            with self.subTest(attribute=name):
                groups = group_offers(
                    [
                        make_offer(
                            "provider_a",
                            "1",
                            canonical_product_id="canonical-phone",
                            product_name="Example phone",
                            product_attributes=(first_attribute,),
                        ),
                        make_offer(
                            "provider_b",
                            "2",
                            canonical_product_id="canonical-phone",
                            product_name="Example phone",
                            product_attributes=(second_attribute,),
                        ),
                    ]
                )

                self.assertEqual(len(groups), 2)
                self.assertTrue(all(len(group.offers) == 1 for group in groups))

    def test_unknown_variant_does_not_invent_a_conflict(self):
        groups = group_offers(
            [
                make_offer(
                    "provider_a",
                    "1",
                    canonical_product_id="canonical-phone",
                    product_name="Example phone",
                    product_attributes=(
                        ProductAttribute(
                            "storage_capacity",
                            64,
                            "verified_provider",
                            unit="GB",
                        ),
                    ),
                ),
                make_offer(
                    "provider_b",
                    "2",
                    canonical_product_id="canonical-phone",
                    product_name="Example phone",
                    product_attributes=(
                        ProductAttribute(
                            "storage_capacity",
                            None,
                            "unknown",
                            extraction_method=None,
                        ),
                    ),
                ),
            ]
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].offers), 2)

    def test_price_summaries_are_separated_by_currency_and_ignore_unknown(self):
        summaries = summarize_prices(
            [
                make_offer("a", "1", price_minor=1000, currency="USD"),
                make_offer("b", "2", price_minor=900, currency="USD"),
                make_offer("c", "3", price_minor=1200, currency="EUR"),
                make_offer("d", "4", price_minor=None, currency=None),
            ]
        )

        self.assertEqual(
            [
                (
                    summary.currency,
                    summary.minimum_price_minor,
                    summary.maximum_price_minor,
                    summary.offer_count,
                )
                for summary in summaries
            ],
            [("EUR", 1200, 1200, 1), ("USD", 900, 1000, 2)],
        )


class DiscoveryEngineTests(unittest.TestCase):
    def test_minimum_price_is_propagated_to_capable_provider(self):
        offer = make_offer("capable")
        provider = FilterSupportingProvider(
            "capable",
            lambda *_args: [offer],
        )

        with DiscoveryEngine(ProviderRegistry([provider])) as engine:
            result = engine.search(
                "phone",
                min_price=Decimal("10.50"),
                currency="INR",
            )

        self.assertEqual(result.providers[0].status, "success")
        self.assertEqual(
            provider.received_filters,
            {
                "min_price": Decimal("10.50"),
                "max_price": None,
                "currency": "INR",
                "category": None,
            },
        )

    def test_maximum_price_is_skipped_without_provider_capability(self):
        calls = []
        provider = FakeProvider(
            "legacy",
            lambda *_args: calls.append(True) or [make_offer("legacy")],
        )

        with DiscoveryEngine(ProviderRegistry([provider])) as engine:
            result = engine.search(
                max_price=Decimal("25"),
                currency="USD",
            )

        self.assertEqual(result.providers[0].status, "unsupported")
        self.assertEqual(result.providers[0].error_code, "unsupported_filter")
        self.assertEqual(calls, [])
        self.assertEqual(result.offers, ())

    def test_combined_bounds_and_category_are_propagated(self):
        offer = make_offer("capable")
        provider = FilterSupportingProvider(
            "capable",
            lambda *_args: [offer],
        )

        with DiscoveryEngine(ProviderRegistry([provider])) as engine:
            result = engine.search(
                "phone",
                min_price=Decimal("10"),
                max_price=Decimal("50"),
                currency="INR",
                category="Smart Phone",
            )

        self.assertEqual(result.providers[0].status, "success")
        self.assertEqual(
            provider.received_filters,
            {
                "min_price": Decimal("10"),
                "max_price": Decimal("50"),
                "currency": "INR",
                "category": "smartphones",
            },
        )

    def test_unsupported_provider_is_skipped_without_unfiltered_results(self):
        unsupported_calls = []
        unsupported = FakeProvider(
            "legacy",
            lambda *_args: unsupported_calls.append(True) or [make_offer("legacy")],
        )
        capable = FilterSupportingProvider(
            "capable",
            lambda *_args: [make_offer("capable")],
        )

        with DiscoveryEngine(
            ProviderRegistry([unsupported, capable]),
            max_workers=2,
        ) as engine:
            result = engine.search(
                min_price=Decimal("10"),
                currency="INR",
                category="smartphones",
            )

        outcomes = {outcome.provider_key: outcome for outcome in result.providers}
        self.assertEqual(outcomes["legacy"].status, "unsupported")
        self.assertEqual(outcomes["legacy"].error_code, "unsupported_filter")
        self.assertEqual(outcomes["capable"].status, "success")
        self.assertEqual(unsupported_calls, [])
        self.assertEqual([offer.provider_key for offer in result.offers], ["capable"])

    def test_invalid_bounds_are_rejected_before_provider_search(self):
        calls = []
        provider = FilterSupportingProvider(
            "capable",
            lambda *_args: calls.append(True) or [],
        )

        with DiscoveryEngine(ProviderRegistry([provider])) as engine:
            with self.assertRaisesRegex(ValueError, "Minimum price cannot exceed"):
                engine.search(
                    min_price=Decimal("20"),
                    max_price=Decimal("10"),
                    currency="INR",
                )
            with self.assertRaisesRegex(ValueError, "supported ISO 4217"):
                engine.search(min_price=Decimal("10"), currency="XYZ")

        self.assertEqual(calls, [])

    def test_providers_run_concurrently_and_results_follow_registry_order(self):
        both_started = threading.Barrier(2)
        first_offer = make_offer("first")
        second_offer = make_offer("second")

        def wait_for_peer(offer):
            def search(_query, _max_price, _currency, _is_organic):
                both_started.wait(timeout=1)
                return [offer]

            return search

        registry = ProviderRegistry(
            [
                FakeProvider("first", wait_for_peer(first_offer)),
                FakeProvider("second", wait_for_peer(second_offer)),
            ]
        )
        with DiscoveryEngine(registry, max_workers=2, max_admitted=2) as engine:
            result = engine.search()

        self.assertEqual([outcome.provider_key for outcome in result.providers], [
            "first",
            "second",
        ])
        self.assertEqual([offer.provider_key for offer in result.offers], [
            "first",
            "second",
        ])

    def test_provider_failure_is_partial_and_does_not_discard_success(self):
        successful_offer = make_offer("good")

        def fail(_query, _max_price, _currency, _is_organic):
            raise RuntimeError("sensitive provider detail")

        registry = ProviderRegistry(
            [
                FakeProvider("bad", fail),
                FakeProvider(
                    "good",
                    lambda *_args: [successful_offer],
                ),
            ]
        )
        with DiscoveryEngine(registry, max_workers=2) as engine:
            result = engine.search()

        self.assertEqual([offer.provider_key for offer in result.offers], ["good"])
        self.assertEqual(result.providers[0].status, "error")
        self.assertNotIn("sensitive", result.providers[0].error_message)

    def test_provider_timeout_is_independent_and_keeps_other_results(self):
        release_slow = threading.Event()
        slow_started = threading.Event()
        fast_offer = make_offer("fast")

        def slow_search(_query, _max_price, _currency, _is_organic):
            slow_started.set()
            release_slow.wait(timeout=2)
            return []

        registry = ProviderRegistry(
            [
                FakeProvider("slow", slow_search),
                FakeProvider("fast", lambda *_args: [fast_offer]),
            ]
        )
        engine = DiscoveryEngine(
            registry,
            max_workers=2,
            max_admitted=2,
            provider_timeouts={"slow": 0.03, "fast": 1},
        )
        try:
            result = engine.search()
            self.assertTrue(slow_started.is_set())
            self.assertEqual(
                {outcome.provider_key: outcome.status for outcome in result.providers},
                {"slow": "timeout", "fast": "success"},
            )
            self.assertEqual([offer.provider_key for offer in result.offers], ["fast"])
        finally:
            release_slow.set()
            engine.shutdown()

    def test_execution_timeout_starts_when_queued_provider_begins(self):
        slow_first_started = threading.Event()
        queued_provider_started = threading.Event()

        def slow_first(_query, _max_price, _currency, _is_organic):
            slow_first_started.set()
            time.sleep(0.08)
            return []

        def queued_provider(_query, _max_price, _currency, _is_organic):
            queued_provider_started.set()
            return []

        registry = ProviderRegistry(
            [
                FakeProvider("slow_first", slow_first),
                FakeProvider("queued", queued_provider),
            ]
        )
        with DiscoveryEngine(
            registry,
            max_workers=1,
            max_admitted=2,
            queue_wait_timeout_seconds=0.5,
            provider_timeouts={"slow_first": 0.5, "queued": 0.02},
        ) as engine:
            result = engine.search()

        self.assertTrue(slow_first_started.is_set())
        self.assertTrue(queued_provider_started.is_set())
        self.assertEqual(
            {outcome.provider_key: outcome.status for outcome in result.providers},
            {"slow_first": "success", "queued": "success"},
        )

    def test_queue_wait_limit_reports_overload_without_running_provider(self):
        release_running = threading.Event()
        running_started = threading.Event()
        queued_started = threading.Event()

        def running_search(_query, _max_price, _currency, _is_organic):
            running_started.set()
            release_running.wait(timeout=0.3)
            return []

        def queued_search(_query, _max_price, _currency, _is_organic):
            queued_started.set()
            return []

        registry = ProviderRegistry(
            [
                FakeProvider("running", running_search),
                FakeProvider("queued", queued_search),
            ]
        )
        engine = DiscoveryEngine(
            registry,
            max_workers=1,
            max_admitted=2,
            queue_wait_timeout_seconds=0.15,
            provider_timeout_seconds=1,
        )
        try:
            caller = ThreadPoolExecutor(max_workers=1)
            search_future = caller.submit(engine.search)
            self.assertTrue(running_started.wait(timeout=1))
            result = search_future.result(timeout=1)

            outcomes = {outcome.provider_key: outcome for outcome in result.providers}
            self.assertEqual(outcomes["running"].status, "success")
            self.assertEqual(outcomes["queued"].status, "overloaded")
            self.assertEqual(outcomes["queued"].error_code, "queue_wait_exceeded")
            self.assertFalse(queued_started.is_set())
        finally:
            release_running.set()
            if "caller" in locals():
                caller.shutdown(wait=True)
            engine.shutdown()

    def test_running_timeout_retains_admission_slot_until_future_completes(self):
        release_provider = threading.Event()
        started = threading.Event()
        calls = 0

        def search(_query, _max_price, _currency, _is_organic):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                release_provider.wait(timeout=2)
            return []

        registry = ProviderRegistry([FakeProvider("only", search)])
        engine = DiscoveryEngine(
            registry,
            max_workers=1,
            max_admitted=1,
            provider_timeout_seconds=0.03,
        )
        try:
            timed_out = engine.search()
            self.assertTrue(started.is_set())
            self.assertEqual(timed_out.providers[0].status, "timeout")

            saturated = engine.search()
            self.assertEqual(saturated.providers[0].status, "overloaded")

            release_provider.set()
            deadline = time.monotonic() + 1
            while True:
                recovered = engine.search()
                if recovered.providers[0].status != "overloaded":
                    break
                if time.monotonic() >= deadline:
                    self.fail("Admission slot was not released after task completion.")
                time.sleep(0.01)
            self.assertEqual(recovered.providers[0].status, "success")
        finally:
            release_provider.set()
            engine.shutdown()

    def test_timeout_of_queued_task_cancels_and_releases_its_slot(self):
        release_running = threading.Event()
        running_started = threading.Event()
        queued_calls = 0

        def running_search(_query, _max_price, _currency, _is_organic):
            running_started.set()
            release_running.wait(timeout=2)
            return []

        def queued_search(_query, _max_price, _currency, _is_organic):
            nonlocal queued_calls
            queued_calls += 1
            return []

        registry = ProviderRegistry(
            [
                FakeProvider("running", running_search),
                FakeProvider("queued", queued_search),
            ]
        )
        engine = DiscoveryEngine(
            registry,
            max_workers=1,
            max_admitted=2,
            queue_wait_timeout_seconds=0.03,
            provider_timeouts={"running": 0.02, "queued": 0.03},
        )
        try:
            first = engine.search(provider_key="running")
            self.assertTrue(running_started.is_set())
            self.assertEqual(first.providers[0].status, "timeout")

            queued_timeout = engine.search(provider_key="queued")
            self.assertEqual(queued_timeout.providers[0].status, "overloaded")
            self.assertEqual(
                queued_timeout.providers[0].error_code,
                "queue_wait_exceeded",
            )

            second_queued_timeout = engine.search(provider_key="queued")
            self.assertEqual(
                second_queued_timeout.providers[0].status,
                "overloaded",
            )
            self.assertEqual(
                second_queued_timeout.providers[0].error_code,
                "queue_wait_exceeded",
            )
            self.assertEqual(queued_calls, 0)
        finally:
            release_running.set()
            engine.shutdown()

    def test_shutdown_cancels_queued_task_and_releases_slots_once(self):
        running_started = threading.Event()
        second_task_submitted = threading.Event()
        release_running = threading.Event()
        queued_calls = 0
        release_count = 0

        def running_search(_query, _max_price, _currency, _is_organic):
            running_started.set()
            release_running.wait(timeout=2)
            return []

        def queued_search(_query, _max_price, _currency, _is_organic):
            nonlocal queued_calls
            queued_calls += 1
            return []

        registry = ProviderRegistry(
            [
                FakeProvider("running", running_search),
                FakeProvider("queued", queued_search),
            ]
        )
        engine = DiscoveryEngine(
            registry,
            max_workers=1,
            max_admitted=2,
            queue_wait_timeout_seconds=1,
            provider_timeout_seconds=1,
        )
        original_release = engine._admission.release
        original_submit = engine._executor.submit
        submission_count = 0

        def tracked_submit(*args, **kwargs):
            nonlocal submission_count
            future = original_submit(*args, **kwargs)
            submission_count += 1
            if submission_count == 2:
                second_task_submitted.set()
            return future

        def counted_release():
            nonlocal release_count
            release_count += 1
            original_release()

        try:
            caller = ThreadPoolExecutor(max_workers=1)
            with (
                patch.object(engine._executor, "submit", side_effect=tracked_submit),
                patch.object(engine._admission, "release", side_effect=counted_release),
            ):
                search_future = caller.submit(engine.search)
                self.assertTrue(running_started.wait(timeout=1))
                self.assertTrue(second_task_submitted.wait(timeout=1))

                engine.shutdown(wait_for_tasks=False)
                release_running.set()
                result = search_future.result(timeout=1)
                engine.shutdown(wait_for_tasks=True)

            outcomes = {outcome.provider_key: outcome for outcome in result.providers}
            self.assertEqual(outcomes["running"].status, "success")
            self.assertEqual(outcomes["queued"].status, "not_started")
            self.assertEqual(queued_calls, 0)
            self.assertEqual(release_count, 2)
        finally:
            release_running.set()
            if "caller" in locals():
                caller.shutdown(wait=True)
            engine.shutdown()

    def test_submission_failure_releases_admission_slot(self):
        provider = FakeProvider("only", lambda *_args: [])
        registry = ProviderRegistry([provider])
        engine = DiscoveryEngine(registry, max_workers=1, max_admitted=1)
        try:
            with patch.object(
                engine._executor,
                "submit",
                side_effect=RuntimeError("executor stopped"),
            ):
                failed = engine.search()

            self.assertEqual(failed.providers[0].status, "error")
            self.assertEqual(failed.providers[0].error_code, "submission_failed")
            succeeded = engine.search()
            self.assertEqual(succeeded.providers[0].status, "success")
        finally:
            engine.shutdown()

    def test_maximum_price_requires_an_explicit_supported_currency(self):
        registry = ProviderRegistry(
            [FakeProvider("only", lambda *_args: [])]
        )
        with DiscoveryEngine(registry) as engine:
            with self.assertRaisesRegex(ValueError, "currency code is required"):
                engine.search(max_price=10)
            with self.assertRaisesRegex(ValueError, "supported ISO 4217"):
                engine.search(max_price=10, currency="XYZ")

    def test_shutdown_rejects_new_provider_submissions(self):
        registry = ProviderRegistry(
            [FakeProvider("only", lambda *_args: [])]
        )
        engine = DiscoveryEngine(registry)

        engine.shutdown()
        result = engine.search()

        self.assertEqual(result.providers[0].status, "unavailable")
        self.assertEqual(result.providers[0].error_code, "engine_closed")


if __name__ == "__main__":
    unittest.main()
