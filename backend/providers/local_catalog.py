from __future__ import annotations

import sqlite3
import uuid
import math
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend.currency import (
    MAX_SQLITE_INTEGER,
    parse_major_amount,
    to_minor_units,
    validate_currency_code,
)
from backend.database_paths import BUSINESS_SQLITE_PATH
from backend.migrations.v001_product_offers import LOCAL_PROVIDER_KEY, LOCAL_PROVIDER_NAME
from backend.product_attributes import build_product_attributes
from backend.providers.base import (
    InvalidProductDataError,
    NormalizedOffer,
    PriceInput,
    ProviderAdapter,
    ProviderError,
    ProviderUnavailableError,
    ResolvedProviderProduct,
)
from backend.providers.filtering import (
    build_provider_search_filters,
    remove_category_terms,
)


DEFAULT_STORE_DB_PATH = BUSINESS_SQLITE_PATH


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class LocalCatalogAdapter(ProviderAdapter):
    @property
    def search_filter_capabilities(self) -> frozenset[str]:
        return frozenset({"min_price", "max_price", "category"})

    def __init__(self, database_path: str | Path = DEFAULT_STORE_DB_PATH) -> None:
        self.database_path = Path(database_path)

    @property
    def provider_key(self) -> str:
        return LOCAL_PROVIDER_KEY

    @property
    def display_name(self) -> str:
        return LOCAL_PROVIDER_NAME

    def search_catalog_products(
        self,
        query: str = "",
        *,
        max_price: PriceInput | None = None,
        min_price: PriceInput | None = None,
        currency: str | None = None,
        is_organic: bool | None = None,
        category: str | None = None,
    ) -> list[dict[str, object]]:
        if not isinstance(query, str):
            raise ValueError("Search query must be text.")
        if is_organic is not None and not isinstance(is_organic, bool):
            raise ValueError("Organic filter must be true, false, or omitted.")
        filters = build_provider_search_filters(
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            category=category,
        )
        query = remove_category_terms(query, filters.category)

        if not self.database_path.is_file():
            raise ProviderUnavailableError(
                self.provider_key, "The local product catalog is unavailable."
            )

        try:
            database_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(database_uri, uri=True, timeout=5)) as connection:
                connection.execute("PRAGMA query_only = ON")
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(products)")
                }
                if not {"id", "name"}.issubset(columns):
                    raise ProviderError(
                        self.provider_key, "The local product catalog is invalid."
                    )

                def optional_column(name: str) -> str:
                    return f'"{name}"' if name in columns else "NULL"

                search_columns = [
                    name
                    for name in ("name", "description", "category")
                    if name in columns
                ]
                sql = f"""
                    SELECT id, name, {optional_column("category")},
                           {optional_column("price")},
                           {optional_column("description")},
                           {optional_column("is_organic")},
                           {optional_column("currency")}
                    FROM products
                """
                conditions: list[str] = []
                parameters: list[object] = []

                if query:
                    conditions.append(
                        "("
                        + " OR ".join(
                            f'"{name}" LIKE ?' for name in search_columns
                        )
                        + ")"
                    )
                    pattern = f"%{query}%"
                    parameters.extend(pattern for _ in search_columns)

                if is_organic is not None:
                    if "is_organic" not in columns:
                        return []
                    conditions.append('"is_organic" = ?')
                    parameters.append(1 if is_organic else 0)

                if filters.has_price_filter:
                    if "currency" not in columns or "price" not in columns:
                        return []
                    conditions.extend(('"currency" = ?', '"price" IS NOT NULL'))
                    parameters.append(filters.currency)

                if conditions:
                    sql += " WHERE " + " AND ".join(conditions)

                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError(
                self.provider_key, "Local catalog search failed."
            ) from exc

        products: list[dict[str, object]] = []
        for product_id, name, category, price, description, organic, product_currency in rows:
            product_category = category if isinstance(category, str) else None
            offer = self.normalize_offer(
                {
                    "id": product_id,
                    "name": name,
                    "price": price,
                    "currency": product_currency,
                    "category": product_category,
                }
            )
            if not filters.matches(offer, product_category):
                continue
            products.append({
                "id": product_id,
                "name": name,
                "category": product_category,
                "price": price,
                "description": description,
                "is_organic": bool(organic),
                "currency": offer.currency,
            })
        return products

    def list_categories(self) -> list[dict[str, object]]:
        if not self.database_path.is_file():
            raise ProviderUnavailableError(
                self.provider_key, "The local product catalog is unavailable."
            )

        try:
            database_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(database_uri, uri=True, timeout=5)) as connection:
                connection.execute("PRAGMA query_only = ON")
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(products)")
                }
                if not {"id", "name"}.issubset(columns):
                    raise ProviderError(
                        self.provider_key, "The local product catalog is invalid."
                    )
                if "category" not in columns:
                    return []
                rows = connection.execute(
                    """
                    SELECT category, COUNT(*) AS product_count
                    FROM products
                    WHERE category IS NOT NULL
                    GROUP BY category
                    ORDER BY category ASC
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError(
                self.provider_key, "Local catalog categories could not be loaded."
            ) from exc

        return [
            {"category": category, "product_count": count}
            for category, count in rows
        ]

    def normalize_offer(
        self,
        product: Mapping[str, object],
    ) -> NormalizedOffer:
        product_id = product.get("id")
        if (
            not isinstance(product_id, int)
            or isinstance(product_id, bool)
            or product_id <= 0
        ):
            raise InvalidProductDataError(
                self.provider_key, "A catalog product has an invalid ID."
            )

        name = product.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidProductDataError(
                self.provider_key,
                f"Product {product_id} has no valid name.",
            )

        currency = product.get("currency")
        if currency is None:
            if product.get("price") is not None:
                try:
                    parse_major_amount(product.get("price"))
                except ValueError as exc:
                    raise InvalidProductDataError(
                        self.provider_key,
                        f"Product {product_id} has an invalid price.",
                    ) from exc
            price_minor = None
        else:
            try:
                currency = validate_currency_code(currency)
                price_minor = to_minor_units(product.get("price"), currency)
            except ValueError as exc:
                raise InvalidProductDataError(
                    self.provider_key,
                    f"Product {product_id} has invalid currency or price metadata.",
                ) from exc

        provider_offer_key = f"local-product:{product_id}"
        offer_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_OID,
                f"DealSync:{self.provider_key}:{provider_offer_key}",
            )
        )
        timestamp = _utc_timestamp()
        brand = product.get("brand")
        model = product.get("model")
        category = product.get("category")
        return NormalizedOffer(
            offer_id=offer_id,
            provider_key=self.provider_key,
            provider_offer_key=provider_offer_key,
            provider_product_id=str(product_id),
            canonical_product_id=None,
            product_name=name,
            brand=brand.strip() if isinstance(brand, str) and brand.strip() else None,
            model=model.strip() if isinstance(model, str) and model.strip() else None,
            price_minor=price_minor,
            currency=currency,
            product_url=None,
            image_url=None,
            availability="unknown",
            last_checked_at=timestamp,
            expires_at=None,
            created_at=timestamp,
            updated_at=timestamp,
            product_attributes=build_product_attributes(
                title=name,
                provenance="local_catalog",
                brand=brand,
                model=model,
                category=category,
            ),
        )

    def resolve_products(
        self,
        provider_product_ids: Sequence[str],
    ) -> tuple[ResolvedProviderProduct, ...]:
        if len(provider_product_ids) > 5:
            raise ValueError("At most five product references can be resolved.")
        if not provider_product_ids:
            return ()

        product_ids: list[int] = []
        for value in provider_product_ids:
            if (
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
            ):
                raise InvalidProductDataError(
                    self.provider_key, "A catalog product reference is invalid."
                )
            product_id = int(value)
            if (
                product_id <= 0
                or product_id > MAX_SQLITE_INTEGER
                or str(product_id) != value
            ):
                raise InvalidProductDataError(
                    self.provider_key, "A catalog product reference is invalid."
                )
            product_ids.append(product_id)
        if len(product_ids) != len(set(product_ids)):
            raise InvalidProductDataError(
                self.provider_key, "Catalog product references must be unique."
            )

        if not self.database_path.is_file():
            raise ProviderUnavailableError(
                self.provider_key, "The local product catalog is unavailable."
            )

        try:
            database_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
            with closing(
                sqlite3.connect(database_uri, uri=True, timeout=5)
            ) as connection:
                product_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(products)")
                }
                if not {"id", "name"}.issubset(product_columns):
                    raise ProviderError(
                        self.provider_key, "The local product catalog is invalid."
                    )

                review_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(reviews)")
                }
                if {"product_id", "rating"}.issubset(review_columns):
                    review_placeholders = ", ".join("?" for _ in product_ids)
                    rating_join = f"""
                        LEFT JOIN (
                            SELECT product_id, AVG(rating) AS average_rating,
                                   COUNT(*) AS review_count
                            FROM reviews
                            WHERE product_id IN ({review_placeholders})
                            GROUP BY product_id
                        ) AS review_summary ON review_summary.product_id = p.id
                    """
                    average_rating_column = "review_summary.average_rating"
                    review_count_column = "COALESCE(review_summary.review_count, 0)"
                    parameters = [*product_ids, *product_ids]
                else:
                    rating_join = ""
                    average_rating_column = "NULL"
                    review_count_column = "NULL"
                    parameters = list(product_ids)

                def optional_column(name: str) -> str:
                    return f"p.{name}" if name in product_columns else "NULL"

                placeholders = ", ".join("?" for _ in product_ids)
                sql = f"""
                    SELECT
                        p.id,
                        p.name,
                        {optional_column("price")},
                        {optional_column("currency")},
                        {optional_column("category")},
                        {optional_column("description")},
                        {optional_column("is_organic")},
                        {average_rating_column},
                        {review_count_column}
                    FROM products AS p
                    {rating_join}
                    WHERE p.id IN ({placeholders})
                """
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError(
                self.provider_key, "Local product evidence could not be loaded."
            ) from exc

        resolved_by_id: dict[int, ResolvedProviderProduct] = {}
        for (
            product_id,
            name,
            price,
            currency,
            category,
            description,
            organic,
            average_rating,
            review_count,
        ) in rows:
            offer = self.normalize_offer(
                {
                    "id": product_id,
                    "name": name,
                    "price": price,
                    "currency": currency,
                    "category": category,
                }
            )
            if isinstance(organic, bool):
                organic_value = organic
            elif isinstance(organic, int) and organic in (0, 1):
                organic_value = bool(organic)
            else:
                organic_value = None
            if isinstance(review_count, int) and review_count >= 0:
                if review_count == 0:
                    rating_value = None
                elif (
                    isinstance(average_rating, (int, float))
                    and not isinstance(average_rating, bool)
                    and math.isfinite(float(average_rating))
                    and 0 <= float(average_rating) <= 5
                ):
                    rating_value = float(average_rating)
                else:
                    rating_value = None
            else:
                review_count = None
                rating_value = None

            resolved_by_id[product_id] = ResolvedProviderProduct(
                offer=offer,
                category=category if isinstance(category, str) and category.strip() else None,
                description=(
                    description
                    if isinstance(description, str) and description.strip()
                    else None
                ),
                is_organic=organic_value,
                average_rating=rating_value,
                review_count=review_count,
                product_attributes=offer.product_attributes,
            )

        return tuple(
            resolved_by_id[product_id]
            for product_id in product_ids
            if product_id in resolved_by_id
        )

    def search(
        self,
        query: str = "",
        *,
        max_price: PriceInput | None = None,
        min_price: PriceInput | None = None,
        currency: str | None = None,
        is_organic: bool | None = None,
        category: str | None = None,
    ) -> list[NormalizedOffer]:
        if not isinstance(query, str):
            raise ValueError("Search query must be text.")
        if is_organic is not None and not isinstance(is_organic, bool):
            raise ValueError("Organic filter must be true, false, or omitted.")
        filters = build_provider_search_filters(
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            category=category,
        )
        query = remove_category_terms(query, filters.category)

        if not self.database_path.is_file():
            raise ProviderUnavailableError(
                self.provider_key, "The local product catalog is unavailable."
            )

        try:
            database_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(database_uri, uri=True, timeout=5)) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(products)")
                }
                currency_column = "currency" if "currency" in columns else "NULL"
                category_column = '"category"' if "category" in columns else "NULL"
                sql = f"""
                    SELECT id, name, price, is_organic, {currency_column},
                           {category_column}
                    FROM products
                    WHERE 1 = 1
                """
                parameters: list[object] = []
                if query:
                    sql += " AND (name LIKE ? OR description LIKE ? OR category LIKE ?)"
                    pattern = f"%{query}%"
                    parameters.extend((pattern, pattern, pattern))
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise ProviderError(
                self.provider_key, "Local catalog search failed."
            ) from exc

        offers = []
        for product_id, name, price, organic, product_currency, category in rows:
            if is_organic is not None and bool(organic) is not is_organic:
                continue

            product_category = category if isinstance(category, str) else None
            offer = self.normalize_offer(
                {
                    "id": product_id,
                    "name": name,
                    "price": price,
                    "currency": product_currency,
                    "category": product_category,
                }
            )
            if not filters.matches(offer, product_category):
                continue
            offers.append(offer)
        return offers
