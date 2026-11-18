from __future__ import annotations

import math
import threading
import time
from concurrent.futures import (
    CancelledError,
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    wait,
)
from collections.abc import Mapping
from typing import Any, TypedDict

from backend.providers.base import (
    NormalizedOffer,
    PriceInput,
    ProviderAdapter,
    ProviderCapacityError,
    ProviderError,
    ProviderOfferNotFoundError,
    ProviderResolutionTimeoutError,
    ProviderUnavailableError,
)
from backend.providers.filtering import build_provider_search_filters
from backend.providers.registry import ProviderRegistry
from backend.discovery.matcher import group_offers, summarize_prices
from backend.discovery.models import (
    DiscoveryResult,
    ProviderOutcome,
)


_DEFAULT_PROVIDER_TIMEOUT_SECONDS = 3.0
_DEFAULT_QUEUE_WAIT_TIMEOUT_SECONDS = 1.0
_PROVIDER_ERROR_MESSAGE = "Provider search failed."
_NOT_STARTED = object()


class _ProviderSearchKwargs(TypedDict, total=False):
    max_price: PriceInput | None
    min_price: PriceInput | None
    currency: str | None
    is_organic: bool | None
    category: str | None


class _AdmissionSlot:
    def __init__(self, semaphore: threading.BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._semaphore.release()


class _ProviderTask:
    def __init__(self, queue_wait_timeout_seconds: float) -> None:
        self._lock = threading.Lock()
        self._submitted_at = time.monotonic()
        self._queue_deadline = (
            self._submitted_at + queue_wait_timeout_seconds
        )
        self._started_at: float | None = None
        self._queue_expired = False

    def mark_started(self) -> float | None:
        with self._lock:
            now = time.monotonic()
            if self._queue_expired or now >= self._queue_deadline:
                self._queue_expired = True
                return None
            self._started_at = now
            return now

    def expire_if_queued(self, now: float) -> bool:
        with self._lock:
            if self._started_at is not None:
                return False
            if self._queue_expired:
                return True
            if now < self._queue_deadline:
                return False
            self._queue_expired = True
            return True

    def next_deadline(self, execution_timeout_seconds: float) -> float:
        with self._lock:
            if self._started_at is None:
                return self._queue_deadline
            return self._started_at + execution_timeout_seconds

    def execution_expired(
        self,
        now: float,
        execution_timeout_seconds: float,
    ) -> bool:
        with self._lock:
            return (
                self._started_at is not None
                and now >= self._started_at + execution_timeout_seconds
            )


class DiscoveryEngine:
    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        max_workers: int = 4,
        max_admitted: int = 8,
        provider_timeout_seconds: float = _DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        queue_wait_timeout_seconds: float = _DEFAULT_QUEUE_WAIT_TIMEOUT_SECONDS,
        provider_timeouts: Mapping[str, float] | None = None,
    ) -> None:
        if (
            not isinstance(max_workers, int)
            or isinstance(max_workers, bool)
            or max_workers < 1
        ):
            raise ValueError("max_workers must be a positive integer.")
        if (
            not isinstance(max_admitted, int)
            or isinstance(max_admitted, bool)
            or max_admitted < 1
        ):
            raise ValueError("max_admitted must be a positive integer.")
        self._validate_timeout(provider_timeout_seconds, "provider_timeout_seconds")
        self._validate_timeout(
            queue_wait_timeout_seconds,
            "queue_wait_timeout_seconds",
        )

        configured_timeouts = dict(provider_timeouts or {})
        for provider_key, timeout in configured_timeouts.items():
            if provider_key not in registry.provider_keys:
                raise ValueError(f"Timeout configured for unknown provider {provider_key!r}.")
            self._validate_timeout(timeout, f"timeout for {provider_key}")

        self._registry = registry
        self._provider_timeout_seconds = provider_timeout_seconds
        self._queue_wait_timeout_seconds = queue_wait_timeout_seconds
        self._provider_timeouts = configured_timeouts
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="dealsync-discovery",
        )
        self._admission = threading.BoundedSemaphore(max_admitted)
        self._state_lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _validate_timeout(value: float, name: str) -> None:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number.")

    def shutdown(self, *, wait_for_tasks: bool = True) -> None:
        """Close admission and cancel queued work where possible.

        Non-waiting shutdown does not stop running threads. Python may still
        join executor threads at interpreter exit if a provider remains stuck.
        """
        with self._state_lock:
            self._closed = True
        self._executor.shutdown(wait=wait_for_tasks, cancel_futures=True)

    def __enter__(self) -> DiscoveryEngine:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.shutdown()

    def search(
        self,
        query: str = "",
        *,
        provider_key: str | None = None,
        min_price: PriceInput | None = None,
        max_price: PriceInput | None = None,
        currency: str | None = None,
        is_organic: bool | None = None,
        category: str | None = None,
    ) -> DiscoveryResult:
        if not isinstance(query, str):
            raise ValueError("Search query must be text.")
        filters = build_provider_search_filters(
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            category=category,
        )
        min_price = filters.min_price
        max_price = filters.max_price
        currency = filters.currency
        category = filters.category

        if is_organic is not None and not isinstance(is_organic, bool):
            raise ValueError("Organic filter must be true, false, or omitted.")

        if provider_key is None:
            provider_keys = self._registry.provider_keys
        else:
            self._registry.get(provider_key)
            provider_keys = (provider_key,)

        outcomes: dict[str, ProviderOutcome] = {}
        offers_by_provider: dict[str, tuple[NormalizedOffer, ...]] = {}
        pending: dict[Future[object], tuple[str, _ProviderTask, float]] = {}
        search_kwargs: _ProviderSearchKwargs = {
            "max_price": max_price,
            "is_organic": is_organic,
        }
        requested_filters: set[str] = set()
        if min_price is not None:
            search_kwargs["min_price"] = min_price
            requested_filters.add("min_price")
        if max_price is not None:
            requested_filters.add("max_price")
        if category is not None:
            search_kwargs["category"] = category
            requested_filters.add("category")
        if currency is not None:
            search_kwargs["currency"] = currency

        for key in provider_keys:
            provider = self._registry.get(key)
            unsupported_filters = requested_filters.difference(
                provider.search_filter_capabilities
            )
            if unsupported_filters:
                filter_names = ", ".join(sorted(unsupported_filters))
                outcomes[key] = ProviderOutcome(
                    provider_key=key,
                    status="unsupported",
                    offer_count=0,
                    error_code="unsupported_filter",
                    error_message=(
                        f"Provider cannot enforce the requested filter(s): "
                        f"{filter_names}."
                    ),
                )
                continue

            future, deadline, rejected = self._submit(
                provider,
                query,
                search_kwargs,
                key,
            )
            if rejected is not None:
                outcomes[key] = rejected
            elif future is not None and deadline is not None:
                task, execution_timeout = deadline
                pending[future] = (key, task, execution_timeout)

        while pending:
            completed = {future for future in pending if future.done()}
            for future in completed:
                key, _task, _execution_timeout = pending.pop(future)
                outcome, provider_offers = self._collect_result(key, future)
                outcomes[key] = outcome
                if outcome.status == "success":
                    offers_by_provider[key] = provider_offers

            now = time.monotonic()
            queue_expired = []
            execution_expired = []
            for future, (key, task, execution_timeout) in pending.items():
                if task.expire_if_queued(now):
                    queue_expired.append(future)
                elif task.execution_expired(now, execution_timeout):
                    execution_expired.append(future)

            for future in queue_expired:
                key, _task, _execution_timeout = pending.pop(future)
                if future.done():
                    outcome, provider_offers = self._collect_result(key, future)
                    outcomes[key] = outcome
                    if outcome.status == "success":
                        offers_by_provider[key] = provider_offers
                else:
                    future.cancel()
                    outcomes[key] = ProviderOutcome(
                        provider_key=key,
                        status="overloaded",
                        offer_count=0,
                        error_code="queue_wait_exceeded",
                        error_message=(
                            "Provider could not start before the queue-wait limit."
                        ),
                    )

            for future in execution_expired:
                key, _task, _execution_timeout = pending.pop(future)
                if future.done():
                    outcome, provider_offers = self._collect_result(key, future)
                    outcomes[key] = outcome
                    if outcome.status == "success":
                        offers_by_provider[key] = provider_offers
                else:
                    outcomes[key] = ProviderOutcome(
                        provider_key=key,
                        status="timeout",
                        offer_count=0,
                        error_code="provider_timeout",
                        error_message="Provider search timed out.",
                    )

            if pending:
                next_deadline = min(
                    task.next_deadline(execution_timeout)
                    for _key, task, execution_timeout in pending.values()
                )
                wait(
                    tuple(pending),
                    timeout=max(0.0, next_deadline - time.monotonic()),
                    return_when=FIRST_COMPLETED,
                )

        ordered_outcomes = tuple(outcomes[key] for key in provider_keys)
        successful_offers: list[NormalizedOffer] = []
        for key in provider_keys:
            successful_offers.extend(offers_by_provider.get(key, ()))

        offers = tuple(successful_offers)
        return DiscoveryResult(
            offers=offers,
            providers=ordered_outcomes,
            groups=group_offers(offers),
            price_summaries=summarize_prices(offers),
        )

    def resolve_offer(
        self,
        provider_key: str,
        provider_offer_key: str,
    ) -> NormalizedOffer:
        if not isinstance(provider_offer_key, str) or not provider_offer_key.strip():
            raise ValueError("A provider offer key is required.")

        provider = self._registry.get(provider_key)
        future, deadline, rejected = self._submit_callable(
            provider_key,
            lambda: provider.resolve_offer(provider_offer_key),
        )
        if rejected is not None:
            if rejected.status == "overloaded":
                raise ProviderCapacityError(provider_key, rejected.error_message or "")
            raise ProviderUnavailableError(provider_key, rejected.error_message or "")

        if future is None or deadline is None:
            raise ProviderUnavailableError(
                provider_key, "Exact offer resolution is unavailable."
            )

        task, execution_timeout = deadline
        while not future.done():
            now = time.monotonic()
            if task.expire_if_queued(now):
                future.cancel()
                raise ProviderCapacityError(
                    provider_key, "Exact offer resolution exceeded its queue limit."
                )
            if task.execution_expired(now, execution_timeout):
                raise ProviderResolutionTimeoutError(
                    provider_key, "Exact offer resolution timed out."
                )
            wait(
                (future,),
                timeout=max(0.0, task.next_deadline(execution_timeout) - now),
                return_when=FIRST_COMPLETED,
            )

        try:
            offer = future.result()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                provider_key, "Exact offer resolution failed."
            ) from exc

        if offer is _NOT_STARTED:
            raise ProviderCapacityError(
                provider_key, "Exact offer resolution exceeded its queue limit."
            )
        if not isinstance(offer, NormalizedOffer):
            raise ProviderError(provider_key, "Provider returned invalid offer data.")
        if offer.provider_key != provider_key:
            raise ProviderError(provider_key, "Provider identity did not match.")
        if offer.provider_offer_key != provider_offer_key:
            raise ProviderOfferNotFoundError(
                provider_key, "Exact provider offer identity did not match."
            )
        return offer

    def _submit(
        self,
        provider: ProviderAdapter,
        query: str,
        search_kwargs: _ProviderSearchKwargs,
        provider_key: str,
    ) -> tuple[
        Future[object] | None,
        tuple[_ProviderTask, float] | None,
        ProviderOutcome | None,
    ]:
        return self._submit_callable(
            provider_key,
            lambda: provider.search(query, **search_kwargs),
        )

    def _submit_callable(
        self,
        provider_key: str,
        function: Any,
    ) -> tuple[
        Future[object] | None,
        tuple[_ProviderTask, float] | None,
        ProviderOutcome | None,
    ]:
        with self._state_lock:
            if self._closed:
                return None, None, ProviderOutcome(
                    provider_key=provider_key,
                    status="unavailable",
                    offer_count=0,
                    error_code="engine_closed",
                    error_message="Discovery engine is shutting down.",
                )
            if not self._admission.acquire(blocking=False):
                return None, None, ProviderOutcome(
                    provider_key=provider_key,
                    status="overloaded",
                    offer_count=0,
                    error_code="capacity_exceeded",
                    error_message="Provider capacity is temporarily full.",
                )

            slot = _AdmissionSlot(self._admission)
            task = _ProviderTask(self._queue_wait_timeout_seconds)

            def execute_provider() -> object:
                if task.mark_started() is None:
                    return _NOT_STARTED
                return function()

            try:
                future = self._executor.submit(execute_provider)
            except Exception:
                slot.release()
                return None, None, ProviderOutcome(
                    provider_key=provider_key,
                    status="error",
                    offer_count=0,
                    error_code="submission_failed",
                    error_message=_PROVIDER_ERROR_MESSAGE,
                )

            timeout = self._provider_timeouts.get(
                provider_key,
                self._provider_timeout_seconds,
            )
            future.add_done_callback(lambda _future: slot.release())
            return future, (task, timeout), None

    @staticmethod
    def _collect_result(
        provider_key: str,
        future: Future[object],
    ) -> tuple[ProviderOutcome, tuple[NormalizedOffer, ...]]:
        try:
            offers = future.result()
            if offers is _NOT_STARTED:
                return (
                    ProviderOutcome(
                        provider_key=provider_key,
                        status="overloaded",
                        offer_count=0,
                        error_code="queue_wait_exceeded",
                        error_message=(
                            "Provider could not start before the queue-wait limit."
                        ),
                    ),
                    (),
                )
            if not isinstance(offers, list) or not all(
                isinstance(offer, NormalizedOffer)
                and offer.provider_key == provider_key
                for offer in offers
            ):
                raise TypeError("Provider returned invalid offer data.")
        except CancelledError:
                return (
                    ProviderOutcome(
                        provider_key=provider_key,
                        status="not_started",
                        offer_count=0,
                        error_code="task_cancelled",
                        error_message="Provider search was cancelled before execution.",
                    ),
                    (),
                )
        except ProviderUnavailableError:
            return (
                ProviderOutcome(
                    provider_key=provider_key,
                    status="unavailable",
                    offer_count=0,
                    error_code="provider_unavailable",
                    error_message="Provider is temporarily unavailable.",
                ),
                (),
            )
        except ProviderError:
            return (
                ProviderOutcome(
                    provider_key=provider_key,
                    status="error",
                    offer_count=0,
                    error_code="provider_error",
                    error_message=_PROVIDER_ERROR_MESSAGE,
                ),
                (),
            )
        except Exception:
            return (
                ProviderOutcome(
                    provider_key=provider_key,
                    status="error",
                    offer_count=0,
                    error_code="provider_error",
                    error_message=_PROVIDER_ERROR_MESSAGE,
                ),
                (),
            )

        return (
            ProviderOutcome(
                provider_key=provider_key,
                status="success",
                offer_count=len(offers),
                error_code=None,
                error_message=None,
            ),
            tuple(offers),
        )
