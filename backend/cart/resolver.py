from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend.cart.models import (
    ExternalCartSchemaError,
    ExternalCartStorageError,
    InvalidCartInputError,
    OfferNotEligibleError,
    OfferQuoteMismatchError,
    ResolvedOfferQuote,
    UnknownOfferReferenceError,
    is_quote_timestamp_fresh,
)
from backend.discovery import DiscoveryEngine
from backend.providers.base import NormalizedOffer, ProviderUnavailableError


_REQUIRED_PROVIDER_COLUMNS = {"provider_key", "enabled"}
_REQUIRED_OFFER_COLUMNS = {
    "offer_id",
    "provider_key",
    "provider_offer_key",
    "provider_product_id",
}


class ExternalOfferResolver:
    def __init__(
        self,
        database_path: str | Path,
        discovery_engine: DiscoveryEngine,
    ) -> None:
        self._database_path = Path(database_path)
        self._discovery_engine = discovery_engine

    def _load_persisted_identity(self, offer_id: str) -> sqlite3.Row:
        if not self._database_path.is_file():
            raise ExternalCartSchemaError(
                "The external-offer schema is unavailable."
            )

        try:
            database_uri = (
                f"{self._database_path.resolve().as_uri()}?mode=ro"
            )
            with closing(
                sqlite3.connect(database_uri, uri=True, timeout=5)
            ) as connection:
                connection.row_factory = sqlite3.Row
                offer_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(product_offers)"
                    ).fetchall()
                }
                provider_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(product_providers)"
                    ).fetchall()
                }
                if (
                    not _REQUIRED_OFFER_COLUMNS.issubset(offer_columns)
                    or not _REQUIRED_PROVIDER_COLUMNS.issubset(provider_columns)
                ):
                    raise ExternalCartSchemaError(
                        "The product-offer schema is unavailable."
                    )

                row = connection.execute(
                    """
                    SELECT offer.offer_id, offer.provider_key,
                           offer.provider_offer_key,
                           offer.provider_product_id, provider.enabled
                    FROM product_offers AS offer
                    JOIN product_providers AS provider
                      ON provider.provider_key = offer.provider_key
                    WHERE offer.offer_id = ?
                    """,
                    (offer_id,),
                ).fetchone()
        except ExternalCartSchemaError:
            raise
        except sqlite3.Error as exc:
            raise ExternalCartStorageError(
                "The exact offer could not be resolved."
            ) from exc

        if row is None:
            raise UnknownOfferReferenceError(
                "The exact provider offer could not be resolved."
            )
        if row["enabled"] != 1 or row["provider_key"] == "local_catalog":
            raise ProviderUnavailableError(
                row["provider_key"],
                "No eligible exact-offer provider is available.",
            )
        return row

    @staticmethod
    def _expiration_is_stale(value: str | None) -> bool:
        if value is None:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OfferNotEligibleError(
                "The provider returned an invalid offer expiration."
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise OfferNotEligibleError(
                "The provider returned an invalid offer expiration."
            )
        return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)

    def resolve(self, offer_id: str) -> ResolvedOfferQuote:
        if (
            not isinstance(offer_id, str)
            or not offer_id.strip()
            or len(offer_id) > 256
            or any(ord(character) < 32 for character in offer_id)
        ):
            raise InvalidCartInputError("Offer ID is invalid.")

        persisted = self._load_persisted_identity(offer_id)
        offer = self._discovery_engine.resolve_offer(
            persisted["provider_key"],
            persisted["provider_offer_key"],
        )
        if (
            not isinstance(offer, NormalizedOffer)
            or offer.offer_id != persisted["offer_id"]
            or offer.provider_key != persisted["provider_key"]
            or offer.provider_offer_key != persisted["provider_offer_key"]
            or offer.provider_product_id != persisted["provider_product_id"]
        ):
            raise OfferQuoteMismatchError(
                "Provider resolution did not match the persisted exact offer."
            )

        try:
            if not is_quote_timestamp_fresh(offer.last_checked_at):
                raise OfferNotEligibleError(
                    "The provider quote is older than the 5-minute freshness window."
                )
            if self._expiration_is_stale(offer.expires_at):
                raise OfferNotEligibleError(
                    "The provider offer has expired."
                )
            return ResolvedOfferQuote(
                offer_id=offer.offer_id,
                provider_key=offer.provider_key,
                provider_offer_key=offer.provider_offer_key,
                provider_product_id=offer.provider_product_id,
                product_name=offer.product_name,
                price_minor=offer.price_minor,
                currency=offer.currency,
                product_url=offer.product_url,
                availability=offer.availability,
                verified_at=offer.last_checked_at,
            )
        except InvalidCartInputError as exc:
            raise OfferNotEligibleError(
                "The provider returned invalid quote evidence."
            ) from exc
