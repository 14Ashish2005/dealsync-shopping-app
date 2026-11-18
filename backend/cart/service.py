from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, cast

from backend.cart.models import (
    MAX_CART_QUANTITY,
    CartItemNotFoundError,
    CurrencyTotal,
    ExternalCart,
    ExternalCartItem,
    ExternalCartSchemaError,
    ExternalCartStorageError,
    InvalidCartInputError,
    OfferAvailability,
    OfferNotEligibleError,
    OfferQuoteMismatchError,
    OfferQuoteStatus,
    PriceConfirmationRequiredError,
    ResolvedOfferQuote,
    effective_quote_status,
    is_quote_timestamp_fresh,
    UnknownOfferReferenceError,
    validate_provider_url,
)
from backend.currency import MAX_SQLITE_INTEGER, validate_currency_code


_REQUIRED_CART_COLUMNS = {
    "owner_id",
    "offer_id",
    "quantity",
    "selected_product_name",
    "selected_provider_key",
    "selected_provider_offer_key",
    "selected_provider_product_id",
    "selected_price_minor",
    "selected_currency",
    "selected_product_url",
    "selected_availability",
    "selected_verified_at",
    "added_at",
    "quote_status",
    "pending_price_minor",
    "pending_currency",
    "pending_verified_at",
    "pending_product_name",
    "pending_product_url",
    "pending_availability",
}
_PURCHASABLE_AVAILABILITY = {"in_stock", "preorder", "backorder"}
_AVAILABILITY_VALUES = {
    "in_stock",
    "out_of_stock",
    "preorder",
    "backorder",
    "unknown",
}
_QUOTE_STATUS_VALUES = {
    "verified_current",
    "stale_unverified",
    "unavailable",
    "price_change_pending",
}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_owner_id(owner_id: object) -> str:
    if (
        not isinstance(owner_id, str)
        or not owner_id.strip()
        or len(owner_id) > 256
        or any(ord(character) < 32 for character in owner_id)
    ):
        raise InvalidCartInputError("Authenticated owner identity is invalid.")
    return owner_id


def _validate_quantity(quantity: object) -> int:
    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity < 1
        or quantity > MAX_CART_QUANTITY
    ):
        raise InvalidCartInputError(
            f"Quantity must be a whole number from 1 to {MAX_CART_QUANTITY}."
        )
    return quantity


def _validate_offer_id(offer_id: object) -> str:
    if (
        not isinstance(offer_id, str)
        or not offer_id.strip()
        or len(offer_id) > 256
        or any(ord(character) < 32 for character in offer_id)
    ):
        raise InvalidCartInputError("Offer ID is invalid.")
    return offer_id


def _same_saved_details(item: ExternalCartItem, quote: ResolvedOfferQuote) -> bool:
    return (
        item.provider_key == quote.provider_key
        and item.provider_offer_key == quote.provider_offer_key
        and item.provider_product_id == quote.provider_product_id
        and item.product_name == quote.product_name
        and item.product_url == quote.product_url
        and item.availability == quote.availability
    )


def _calculate_currency_totals(
    items: tuple[ExternalCartItem, ...],
) -> tuple[CurrencyTotal, ...]:
    totals: dict[str, dict[str, int | bool]] = defaultdict(
        lambda: {
            "amount": 0,
            "item_count": 0,
            "all_current": True,
        }
    )
    for item in items:
        currency = validate_currency_code(item.currency)
        total = totals[currency]
        total["amount"] = int(total["amount"]) + item.snapshot_subtotal_minor
        total["item_count"] = int(total["item_count"]) + item.quantity
        total["all_current"] = bool(total["all_current"]) and (
            item.quote_status == "verified_current"
        )

    return tuple(
        CurrencyTotal(
            currency=currency,
            snapshot_total_minor=int(values["amount"]),
            item_count=int(values["item_count"]),
            all_quotes_current=bool(values["all_current"]),
        )
        for currency, values in sorted(totals.items())
    )


class ExternalOfferCartService:
    """Read/write service for externally sourced offer snapshots.

    `ResolvedOfferQuote` must be created by trusted server-side provider resolution,
    never by parsing client-submitted quote fields. The quote is also checked against
    the persisted `product_offers` row before it can enter this cart. Owner arguments
    must come from an authenticated server-side principal, never a request body.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        if not self._database_path.is_file():
            raise ExternalCartSchemaError(
                "The store database or external-cart migration is unavailable."
            )
        try:
            connection = sqlite3.connect(self._database_path, timeout=5)
        except sqlite3.Error as exc:
            raise ExternalCartStorageError(
                "The external-offer cart could not be opened."
            ) from exc
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(cart_offer_items)"
                ).fetchall()
            }
            if not _REQUIRED_CART_COLUMNS.issubset(columns):
                raise ExternalCartSchemaError(
                    "Apply the product-offer migration and external-cart quote migration "
                    "before using the external-offer cart."
                )
            return connection
        except ExternalCartSchemaError:
            connection.close()
            raise
        except sqlite3.Error as exc:
            connection.close()
            raise ExternalCartStorageError(
                "The external-offer cart schema could not be inspected."
            ) from exc

    @contextmanager
    def _connection(
        self,
        *,
        write: bool,
    ) -> Generator[sqlite3.Connection, None, None]:
        connection = self._connect()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except sqlite3.Error as exc:
            if write:
                connection.rollback()
            raise ExternalCartStorageError(
                "The external-offer cart operation failed."
            ) from exc
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _load_resolved_offer(
        connection: sqlite3.Connection,
        quote: ResolvedOfferQuote,
    ) -> sqlite3.Row:
        if quote.provider_key == "local_catalog":
            raise OfferNotEligibleError(
                "Local catalog products must use the existing local-product cart."
            )

        row = connection.execute(
            """
            SELECT offer.offer_id, offer.provider_key, offer.provider_offer_key,
                   offer.provider_product_id, offer.product_name,
                   offer.price_minor, offer.currency, offer.product_url,
                   offer.availability, offer.last_checked_at, provider.enabled
            FROM product_offers AS offer
            JOIN product_providers AS provider
              ON provider.provider_key = offer.provider_key
            WHERE offer.offer_id = ?
            """,
            (quote.offer_id,),
        ).fetchone()
        if row is None:
            raise UnknownOfferReferenceError(
                "The exact provider offer could not be resolved."
            )
        if row["enabled"] != 1:
            raise OfferNotEligibleError("The offer provider is not enabled.")

        persisted_fields = (
            row["provider_key"] == quote.provider_key,
            row["provider_offer_key"] == quote.provider_offer_key,
            row["provider_product_id"] == quote.provider_product_id,
            row["product_name"] == quote.product_name,
            row["price_minor"] == quote.price_minor,
            row["currency"] == quote.currency,
            row["product_url"] == quote.product_url,
            row["availability"] == quote.availability,
            row["last_checked_at"] == quote.verified_at,
        )
        if not all(persisted_fields):
            raise OfferQuoteMismatchError(
                "Resolved offer evidence does not match the stored provider offer."
            )
        return row

    @staticmethod
    def _require_purchase_ready(quote: ResolvedOfferQuote) -> None:
        if quote.price_minor is None or quote.currency is None:
            raise OfferNotEligibleError(
                "An offer needs a verified price and explicit currency before it "
                "can be added to the external-offer cart."
            )
        if quote.availability not in _PURCHASABLE_AVAILABILITY:
            raise OfferNotEligibleError(
                "The offer has no verified purchase-ready availability."
            )

    @staticmethod
    def _require_fresh_quote(quote: ResolvedOfferQuote) -> None:
        if not is_quote_timestamp_fresh(quote.verified_at):
            raise OfferNotEligibleError(
                "The provider quote is older than the 5-minute freshness window."
            )

    @staticmethod
    def _load_offer_identity(
        connection: sqlite3.Connection,
        quote: ResolvedOfferQuote,
    ) -> sqlite3.Row:
        if quote.provider_key == "local_catalog":
            raise OfferNotEligibleError(
                "Local catalog products must use the existing local-product cart."
            )
        row = connection.execute(
            """
            SELECT offer.offer_id, offer.provider_key, offer.provider_offer_key,
                   offer.provider_product_id, provider.enabled
            FROM product_offers AS offer
            JOIN product_providers AS provider
              ON provider.provider_key = offer.provider_key
            WHERE offer.offer_id = ?
            """,
            (quote.offer_id,),
        ).fetchone()
        if row is None:
            raise UnknownOfferReferenceError(
                "The exact provider offer could not be resolved."
            )
        if row["enabled"] != 1:
            raise OfferNotEligibleError("The offer provider is not enabled.")
        if (
            row["offer_id"] != quote.offer_id
            or row["provider_key"] != quote.provider_key
            or row["provider_offer_key"] != quote.provider_offer_key
            or row["provider_product_id"] != quote.provider_product_id
        ):
            raise OfferQuoteMismatchError(
                "Resolved offer identity does not match the stored provider offer."
            )
        return row

    @classmethod
    def _persist_provider_quote(
        cls,
        connection: sqlite3.Connection,
        quote: ResolvedOfferQuote,
    ) -> None:
        cls._require_fresh_quote(quote)
        cls._require_purchase_ready(quote)
        cls._load_offer_identity(connection, quote)
        cursor = connection.execute(
            """
            UPDATE product_offers
            SET product_name = ?, price_minor = ?, currency = ?,
                product_url = ?, availability = ?, last_checked_at = ?,
                updated_at = ?
            WHERE offer_id = ? AND provider_key = ?
              AND provider_offer_key = ? AND provider_product_id = ?
            """,
            (
                quote.product_name,
                quote.price_minor,
                quote.currency,
                quote.product_url,
                quote.availability,
                quote.verified_at,
                _utc_timestamp(),
                quote.offer_id,
                quote.provider_key,
                quote.provider_offer_key,
                quote.provider_product_id,
            ),
        )
        if cursor.rowcount != 1:
            raise OfferQuoteMismatchError(
                "Resolved offer identity does not match the stored provider offer."
            )

    @staticmethod
    def _get_item(
        connection: sqlite3.Connection,
        owner_id: str,
        offer_id: str,
    ) -> ExternalCartItem:
        row = connection.execute(
            """
            SELECT offer_id, selected_provider_key, selected_provider_offer_key,
                   selected_provider_product_id, selected_product_name,
                   selected_price_minor, selected_currency, quantity,
                   selected_product_url, selected_availability,
                   selected_verified_at, added_at, quote_status,
                   pending_price_minor, pending_currency, pending_verified_at,
                   pending_product_name, pending_product_url, pending_availability
            FROM cart_offer_items
            WHERE owner_id = ? AND offer_id = ?
            """,
            (owner_id, offer_id),
        ).fetchone()
        if row is None:
            raise CartItemNotFoundError("The offer is not in this owner's cart.")

        availability = row["selected_availability"]
        quote_status = row["quote_status"]
        if availability not in _AVAILABILITY_VALUES:
            raise ExternalCartSchemaError(
                "The external cart contains an invalid availability value."
            )
        if quote_status not in _QUOTE_STATUS_VALUES:
            raise ExternalCartSchemaError(
                "The external cart contains an invalid quote status."
            )
        try:
            effective_status = effective_quote_status(
                cast(OfferQuoteStatus, quote_status),
                row["selected_verified_at"],
            )
        except InvalidCartInputError as exc:
            raise ExternalCartSchemaError(
                "The external cart contains an invalid verification timestamp."
            ) from exc
        try:
            currency = validate_currency_code(row["selected_currency"])
        except ValueError as exc:
            raise ExternalCartSchemaError(
                "The external cart contains invalid currency metadata."
            ) from exc
        if (
            not isinstance(row["selected_price_minor"], int)
            or row["selected_price_minor"] < 0
            or row["selected_price_minor"] > MAX_SQLITE_INTEGER
            or not isinstance(row["quantity"], int)
            or row["quantity"] < 1
            or row["quantity"] > MAX_CART_QUANTITY
        ):
            raise ExternalCartSchemaError(
                "The external cart contains an invalid price or quantity."
            )
        try:
            product_url = validate_provider_url(row["selected_product_url"])
        except InvalidCartInputError as exc:
            raise ExternalCartSchemaError(
                "The external cart contains an invalid provider URL."
            ) from exc

        pending_currency = row["pending_currency"]
        if pending_currency is not None:
            try:
                pending_currency = validate_currency_code(pending_currency)
            except ValueError as exc:
                raise ExternalCartSchemaError(
                    "The external cart contains invalid pending currency metadata."
                ) from exc
        pending_price = row["pending_price_minor"]
        if pending_price is not None and (
            not isinstance(pending_price, int)
            or isinstance(pending_price, bool)
            or pending_price < 0
            or pending_price > MAX_SQLITE_INTEGER
        ):
            raise ExternalCartSchemaError(
                "The external cart contains an invalid pending price."
            )
        if quote_status == "price_change_pending":
            if (
                pending_price is None
                or pending_currency is None
                or row["pending_verified_at"] is None
                or row["pending_product_name"] is None
                or row["pending_availability"] not in _AVAILABILITY_VALUES
            ):
                raise ExternalCartSchemaError(
                    "The external cart contains an incomplete pending quote."
                )
            try:
                ResolvedOfferQuote(
                    offer_id=row["offer_id"],
                    provider_key=row["selected_provider_key"],
                    provider_offer_key=row["selected_provider_offer_key"],
                    provider_product_id=row["selected_provider_product_id"],
                    product_name=row["pending_product_name"],
                    price_minor=pending_price,
                    currency=pending_currency,
                    product_url=row["pending_product_url"],
                    availability=row["pending_availability"],
                    verified_at=row["pending_verified_at"],
                )
            except InvalidCartInputError as exc:
                raise ExternalCartSchemaError(
                    "The external cart contains an invalid pending quote."
                ) from exc
        elif any(
            row[column] is not None
            for column in (
                "pending_price_minor",
                "pending_currency",
                "pending_verified_at",
                "pending_product_name",
                "pending_product_url",
                "pending_availability",
            )
        ):
            raise ExternalCartSchemaError(
                "The external cart has pending quote data without a pending status."
            )
        if effective_status == "verified_current":
            try:
                ResolvedOfferQuote(
                    offer_id=row["offer_id"],
                    provider_key=row["selected_provider_key"],
                    provider_offer_key=row["selected_provider_offer_key"],
                    provider_product_id=row["selected_provider_product_id"],
                    product_name=row["selected_product_name"],
                    price_minor=row["selected_price_minor"],
                    currency=currency,
                    product_url=product_url,
                    availability=availability,
                    verified_at=row["selected_verified_at"],
                )
            except InvalidCartInputError as exc:
                raise ExternalCartSchemaError(
                    "The external cart contains an invalid current quote."
                ) from exc

        return ExternalCartItem(
            offer_id=row["offer_id"],
            provider_key=row["selected_provider_key"],
            provider_offer_key=row["selected_provider_offer_key"],
            provider_product_id=row["selected_provider_product_id"],
            product_name=row["selected_product_name"],
            price_minor=row["selected_price_minor"],
            currency=currency,
            quantity=row["quantity"],
            product_url=product_url,
            availability=cast(OfferAvailability, availability),
            verified_at=row["selected_verified_at"],
            added_at=row["added_at"],
            quote_status=effective_status,
            pending_price_minor=row["pending_price_minor"],
            pending_currency=pending_currency,
            pending_verified_at=row["pending_verified_at"],
            pending_product_name=row["pending_product_name"],
            pending_product_url=row["pending_product_url"],
            pending_availability=(
                cast(OfferAvailability, row["pending_availability"])
                if row["pending_availability"] is not None
                else None
            ),
        )

    def add_offer(
        self,
        owner_id: str,
        quote: ResolvedOfferQuote,
        quantity: int = 1,
    ) -> ExternalCartItem:
        owner = _validate_owner_id(owner_id)
        amount = _validate_quantity(quantity)
        if not isinstance(quote, ResolvedOfferQuote):
            raise InvalidCartInputError(
                "A server-resolved offer quote is required."
            )

        with self._connection(write=True) as connection:
            return self._add_offer_in_connection(
                connection, owner, quote, amount
            )

    def add_resolved_offer(
        self,
        owner_id: str,
        quote: ResolvedOfferQuote,
        quantity: int = 1,
    ) -> ExternalCartItem:
        owner = _validate_owner_id(owner_id)
        amount = _validate_quantity(quantity)
        if not isinstance(quote, ResolvedOfferQuote):
            raise InvalidCartInputError(
                "A server-resolved offer quote is required."
            )
        with self._connection(write=True) as connection:
            self._persist_provider_quote(connection, quote)
            return self._add_offer_in_connection(
                connection, owner, quote, amount
            )

    def _add_offer_in_connection(
        self,
        connection: sqlite3.Connection,
        owner: str,
        quote: ResolvedOfferQuote,
        amount: int,
    ) -> ExternalCartItem:
        self._load_resolved_offer(connection, quote)
        self._require_fresh_quote(quote)
        self._require_purchase_ready(quote)
        existing_row = connection.execute(
            """
            SELECT 1
            FROM cart_offer_items
            WHERE owner_id = ? AND offer_id = ?
            """,
            (owner, quote.offer_id),
        ).fetchone()

        if existing_row is None:
            connection.execute(
                """
                INSERT INTO cart_offer_items (
                    owner_id, offer_id, quantity, selected_product_name,
                    selected_provider_key, selected_provider_offer_key,
                    selected_provider_product_id, selected_price_minor,
                    selected_currency, selected_product_url,
                    selected_availability, selected_verified_at, added_at,
                    quote_status, pending_price_minor, pending_currency,
                    pending_verified_at, pending_product_name,
                    pending_product_url, pending_availability
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'verified_current', NULL, NULL, NULL, NULL,
                          NULL, NULL)
                """,
                (
                    owner,
                    quote.offer_id,
                    amount,
                    quote.product_name,
                    quote.provider_key,
                    quote.provider_offer_key,
                    quote.provider_product_id,
                    quote.price_minor,
                    quote.currency,
                    quote.product_url,
                    quote.availability,
                    quote.verified_at,
                    _utc_timestamp(),
                ),
            )
            return self._get_item(connection, owner, quote.offer_id)

        item = self._get_item(connection, owner, quote.offer_id)
        if item.quote_status == "price_change_pending":
            raise PriceConfirmationRequiredError(
                "Confirm or resolve the pending price change before adding "
                "more quantity."
            )

        if (
            item.price_minor != quote.price_minor
            or item.currency != quote.currency
        ):
            connection.execute(
                """
                UPDATE cart_offer_items
                SET quote_status = 'price_change_pending',
                    pending_price_minor = ?,
                    pending_currency = ?,
                    pending_verified_at = ?,
                    pending_product_name = ?,
                    pending_product_url = ?,
                    pending_availability = ?
                WHERE owner_id = ? AND offer_id = ?
                """,
                (
                    quote.price_minor,
                    quote.currency,
                    quote.verified_at,
                    quote.product_name,
                    quote.product_url,
                    quote.availability,
                    owner,
                    quote.offer_id,
                ),
            )
            return self._get_item(connection, owner, quote.offer_id)

        if not _same_saved_details(item, quote):
            connection.execute(
                """
                UPDATE cart_offer_items
                SET quote_status = 'stale_unverified',
                    pending_price_minor = NULL,
                    pending_currency = NULL,
                    pending_verified_at = NULL,
                    pending_product_name = NULL,
                    pending_product_url = NULL,
                    pending_availability = NULL
                WHERE owner_id = ? AND offer_id = ?
                """,
                (owner, quote.offer_id),
            )
            return self._get_item(connection, owner, quote.offer_id)

        new_quantity = item.quantity + amount
        _validate_quantity(new_quantity)
        connection.execute(
            """
            UPDATE cart_offer_items
            SET quantity = ?, selected_verified_at = ?,
                quote_status = 'verified_current'
            WHERE owner_id = ? AND offer_id = ?
            """,
            (new_quantity, quote.verified_at, owner, quote.offer_id),
        )
        return self._get_item(connection, owner, quote.offer_id)

    def refresh_offer(
        self,
        owner_id: str,
        quote: ResolvedOfferQuote,
    ) -> ExternalCartItem:
        owner = _validate_owner_id(owner_id)
        if not isinstance(quote, ResolvedOfferQuote):
            raise InvalidCartInputError(
                "A server-resolved offer quote is required."
            )

        with self._connection(write=True) as connection:
            item = self._get_item(connection, owner, quote.offer_id)
            if item.quote_status == "price_change_pending":
                raise PriceConfirmationRequiredError(
                    "Confirm the pending price change before refreshing the offer."
                )
            self._load_offer_identity(connection, quote)
            self._require_fresh_quote(quote)

            if quote.availability == "out_of_stock":
                connection.execute(
                    """
                    UPDATE cart_offer_items
                    SET quote_status = 'unavailable'
                    WHERE owner_id = ? AND offer_id = ?
                    """,
                    (owner, quote.offer_id),
                )
                return self._get_item(connection, owner, quote.offer_id)

            if (
                quote.price_minor is None
                or quote.currency is None
                or quote.availability == "unknown"
            ):
                if item.quote_status != "unavailable":
                    connection.execute(
                        """
                        UPDATE cart_offer_items
                        SET quote_status = 'stale_unverified'
                        WHERE owner_id = ? AND offer_id = ?
                        """,
                        (owner, quote.offer_id),
                    )
                return self._get_item(connection, owner, quote.offer_id)

            self._persist_provider_quote(connection, quote)
            if (
                item.price_minor != quote.price_minor
                or item.currency != quote.currency
            ):
                connection.execute(
                    """
                    UPDATE cart_offer_items
                    SET quote_status = 'price_change_pending',
                        pending_price_minor = ?,
                        pending_currency = ?,
                        pending_verified_at = ?,
                        pending_product_name = ?,
                        pending_product_url = ?,
                        pending_availability = ?
                    WHERE owner_id = ? AND offer_id = ?
                    """,
                    (
                        quote.price_minor,
                        quote.currency,
                        quote.verified_at,
                        quote.product_name,
                        quote.product_url,
                        quote.availability,
                        owner,
                        quote.offer_id,
                    ),
                )
                return self._get_item(connection, owner, quote.offer_id)

            if not _same_saved_details(item, quote):
                connection.execute(
                    """
                    UPDATE cart_offer_items
                    SET quote_status = 'stale_unverified'
                    WHERE owner_id = ? AND offer_id = ?
                    """,
                    (owner, quote.offer_id),
                )
                return self._get_item(connection, owner, quote.offer_id)

            connection.execute(
                """
                UPDATE cart_offer_items
                SET selected_verified_at = ?, quote_status = 'verified_current'
                WHERE owner_id = ? AND offer_id = ?
                """,
                (quote.verified_at, owner, quote.offer_id),
            )
            return self._get_item(connection, owner, quote.offer_id)

    def get_cart(self, owner_id: str) -> ExternalCart:
        owner = _validate_owner_id(owner_id)
        with self._connection(write=False) as connection:
            rows = connection.execute(
                """
                SELECT offer_id
                FROM cart_offer_items
                WHERE owner_id = ?
                ORDER BY added_at, offer_id
                """,
                (owner,),
            ).fetchall()
            items = tuple(
                self._get_item(connection, owner, row["offer_id"])
                for row in rows
            )
        return ExternalCart(
            items=items,
            currency_totals=_calculate_currency_totals(items),
        )

    def update_quantity(
        self,
        owner_id: str,
        offer_id: str,
        quantity: int,
    ) -> ExternalCartItem:
        owner = _validate_owner_id(owner_id)
        reference = _validate_offer_id(offer_id)
        amount = _validate_quantity(quantity)
        with self._connection(write=True) as connection:
            self._get_item(connection, owner, reference)
            connection.execute(
                """
                UPDATE cart_offer_items
                SET quantity = ?
                WHERE owner_id = ? AND offer_id = ?
                """,
                (amount, owner, reference),
            )
            return self._get_item(connection, owner, reference)

    def remove_item(self, owner_id: str, offer_id: str) -> None:
        owner = _validate_owner_id(owner_id)
        reference = _validate_offer_id(offer_id)
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                DELETE FROM cart_offer_items
                WHERE owner_id = ? AND offer_id = ?
                """,
                (owner, reference),
            )
            if cursor.rowcount == 0:
                raise CartItemNotFoundError("The offer is not in this owner's cart.")

    def clear_cart(self, owner_id: str) -> int:
        owner = _validate_owner_id(owner_id)
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "DELETE FROM cart_offer_items WHERE owner_id = ?",
                (owner,),
            )
            return cursor.rowcount

    def mark_stale(self, owner_id: str, offer_id: str) -> ExternalCartItem:
        return self._set_quote_state(owner_id, offer_id, "stale_unverified")

    def mark_unavailable(self, owner_id: str, offer_id: str) -> ExternalCartItem:
        return self._set_quote_state(owner_id, offer_id, "unavailable")

    def _set_quote_state(
        self,
        owner_id: str,
        offer_id: str,
        state: OfferQuoteStatus,
    ) -> ExternalCartItem:
        owner = _validate_owner_id(owner_id)
        reference = _validate_offer_id(offer_id)
        with self._connection(write=True) as connection:
            self._get_item(connection, owner, reference)
            connection.execute(
                """
                UPDATE cart_offer_items
                SET quote_status = ?,
                    pending_price_minor = NULL,
                    pending_currency = NULL,
                    pending_verified_at = NULL,
                    pending_product_name = NULL,
                    pending_product_url = NULL,
                    pending_availability = NULL
                WHERE owner_id = ? AND offer_id = ?
                """,
                (state, owner, reference),
            )
            return self._get_item(connection, owner, reference)

    def stage_price_change(
        self,
        owner_id: str,
        quote: ResolvedOfferQuote,
    ) -> ExternalCartItem:
        owner = _validate_owner_id(owner_id)
        if not isinstance(quote, ResolvedOfferQuote):
            raise InvalidCartInputError(
                "A server-resolved offer quote is required."
            )
        with self._connection(write=True) as connection:
            self._load_resolved_offer(connection, quote)
            self._require_fresh_quote(quote)
            self._require_purchase_ready(quote)
            item = self._get_item(connection, owner, quote.offer_id)
            if (
                item.price_minor == quote.price_minor
                and item.currency == quote.currency
            ):
                raise InvalidCartInputError(
                    "The resolved quote does not contain a price change."
                )
            if item.quote_status == "price_change_pending":
                if (
                    item.pending_price_minor == quote.price_minor
                    and item.pending_currency == quote.currency
                    and item.pending_verified_at == quote.verified_at
                ):
                    return item
                raise PriceConfirmationRequiredError(
                    "A different price change is already awaiting confirmation."
                )
            connection.execute(
                """
                UPDATE cart_offer_items
                SET quote_status = 'price_change_pending',
                    pending_price_minor = ?,
                    pending_currency = ?,
                    pending_verified_at = ?,
                    pending_product_name = ?,
                    pending_product_url = ?,
                    pending_availability = ?
                WHERE owner_id = ? AND offer_id = ?
                """,
                (
                    quote.price_minor,
                    quote.currency,
                    quote.verified_at,
                    quote.product_name,
                    quote.product_url,
                    quote.availability,
                    owner,
                    quote.offer_id,
                ),
            )
            return self._get_item(connection, owner, quote.offer_id)

    def confirm_price_change(
        self,
        owner_id: str,
        offer_id: str,
    ) -> ExternalCartItem:
        owner = _validate_owner_id(owner_id)
        reference = _validate_offer_id(offer_id)
        with self._connection(write=True) as connection:
            item = self._get_item(connection, owner, reference)
            if (
                item.quote_status != "price_change_pending"
                or item.pending_price_minor is None
                or item.pending_currency is None
                or item.pending_verified_at is None
            ):
                raise PriceConfirmationRequiredError(
                    "There is no verified price change awaiting confirmation."
                )
            pending_quote = ResolvedOfferQuote(
                offer_id=item.offer_id,
                provider_key=item.provider_key,
                provider_offer_key=item.provider_offer_key,
                provider_product_id=item.provider_product_id,
                product_name=item.pending_product_name or item.product_name,
                price_minor=item.pending_price_minor,
                currency=item.pending_currency,
                product_url=item.pending_product_url,
                availability=item.pending_availability or item.availability,
                verified_at=item.pending_verified_at,
            )
            pending_quote_is_current = False
            try:
                self._load_resolved_offer(connection, pending_quote)
                self._require_purchase_ready(pending_quote)
            except (
                OfferNotEligibleError,
                OfferQuoteMismatchError,
                UnknownOfferReferenceError,
            ):
                pass
            else:
                pending_quote_is_current = is_quote_timestamp_fresh(
                    pending_quote.verified_at
                )
            connection.execute(
                """
                UPDATE cart_offer_items
                SET selected_price_minor = pending_price_minor,
                    selected_currency = pending_currency,
                    selected_verified_at = pending_verified_at,
                    selected_product_name = pending_product_name,
                    selected_product_url = pending_product_url,
                    selected_availability = pending_availability,
                    quote_status = ?,
                    pending_price_minor = NULL,
                    pending_currency = NULL,
                    pending_verified_at = NULL,
                    pending_product_name = NULL,
                    pending_product_url = NULL,
                    pending_availability = NULL
                WHERE owner_id = ? AND offer_id = ?
                """,
                (
                    "verified_current"
                    if pending_quote_is_current
                    else "stale_unverified",
                    owner,
                    reference,
                ),
            )
            return self._get_item(connection, owner, reference)
