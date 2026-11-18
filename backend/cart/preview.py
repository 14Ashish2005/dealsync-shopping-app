from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence


class CartPreviewStateError(ValueError):
    """Raised when a server-held cart preview cannot be confirmed."""


@dataclass(frozen=True)
class LocalCartProductSnapshot:
    product_id: int
    name: str
    price: Decimal
    currency: str | None


@dataclass(frozen=True)
class PendingCartPreview:
    token: str
    references: tuple[tuple[str, str], ...]
    facts: tuple[tuple[object, ...], ...]
    eligible_products: tuple[LocalCartProductSnapshot, ...]
    expires_at: float


@dataclass
class CartConfirmationCommit:
    token_hash: str
    references_json: str
    expires_at: float
    reply: str
    added_product_ids: tuple[int, ...]
    preview_items: tuple[dict[str, object], ...]
    response: dict[str, object] | None = None


class CartPreviewStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = 600,
        max_entries: int = 512,
    ) -> None:
        if ttl_seconds <= 0 or max_entries < 1:
            raise ValueError("Cart preview limits must be positive.")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._by_owner: dict[str, PendingCartPreview] = {}

    def create(
        self,
        owner_id: str,
        references: Sequence[tuple[str, str]],
        facts: Sequence[tuple[object, ...]],
        eligible_products: Sequence[LocalCartProductSnapshot],
    ) -> str:
        if (
            not owner_id
            or not references
            or len(references) > 5
            or len(references) != len(facts)
            or not eligible_products
        ):
            raise ValueError("A valid eligible cart preview is required.")

        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        pending = PendingCartPreview(
            token=token,
            references=tuple(references),
            facts=tuple(tuple(item) for item in facts),
            eligible_products=tuple(eligible_products),
            expires_at=now + self._ttl_seconds,
        )
        with self._lock:
            self._remove_expired(now)
            if owner_id not in self._by_owner and len(self._by_owner) >= self._max_entries:
                raise CartPreviewStateError(
                    "Cart preview capacity is temporarily unavailable."
                )
            self._by_owner[owner_id] = pending
        return token

    def validate(
        self,
        owner_id: str,
        token: str,
        references: Sequence[tuple[str, str]],
        facts: Sequence[tuple[object, ...]],
    ) -> PendingCartPreview:
        now = time.monotonic()
        with self._lock:
            self._remove_expired(now)
            pending = self._by_owner.get(owner_id)
            if pending is None:
                raise CartPreviewStateError(
                    "The cart preview expired or is no longer available."
                )
            if not secrets.compare_digest(pending.token, token):
                raise CartPreviewStateError(
                    "The cart preview token is invalid."
                )
            if (
                pending.references != tuple(references)
                or pending.facts != tuple(tuple(item) for item in facts)
            ):
                del self._by_owner[owner_id]
                raise CartPreviewStateError(
                    "The selected products changed. Prepare a new cart preview."
                )
            return pending

    def complete(self, owner_id: str, token: str) -> None:
        with self._lock:
            pending = self._by_owner.get(owner_id)
            if pending is not None and secrets.compare_digest(pending.token, token):
                del self._by_owner[owner_id]

    def invalidate(self, owner_id: str) -> None:
        with self._lock:
            self._by_owner.pop(owner_id, None)

    def _remove_expired(self, now: float) -> None:
        expired_owners = [
            owner_id
            for owner_id, pending in self._by_owner.items()
            if pending.expires_at <= now
        ]
        for owner_id in expired_owners:
            del self._by_owner[owner_id]
