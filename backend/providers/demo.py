from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_OID, uuid5

from backend.currency import (
    to_minor_units,
    validate_currency_code,
)
from backend.product_attributes import build_product_attributes
from backend.providers.base import (
    InvalidProductDataError,
    NormalizedOffer,
    PriceInput,
    ProviderAdapter,
    ResolvedProviderProduct,
)
from backend.providers.filtering import (
    build_provider_search_filters,
    remove_category_terms,
)


_SAMPLE_LABEL = "[DEMO SAMPLE] "
_SAMPLE_PRODUCTS: tuple[dict[str, object], ...] = (
    {
        "id": "demo-001",
        "name": f"{_SAMPLE_LABEL}Insulated water bottle (750 mL)",
        "category": "Drinkware",
        "description": "Illustrative sample stainless-steel bottle.",
        "price": Decimal("18.99"),
        "currency": "USD",
        "is_organic": False,
    },
    {
        "id": "demo-002",
        "name": f"{_SAMPLE_LABEL}Green tea bags (20-count)",
        "category": "Grocery",
        "description": "Illustrative sample organic green tea.",
        "price": Decimal("9.50"),
        "currency": "EUR",
        "is_organic": True,
    },
    {
        "id": "demo-003",
        "name": f"{_SAMPLE_LABEL}Adjustable desk lamp",
        "category": "Home office",
        "description": "Illustrative sample LED desk lamp.",
        "price": Decimal("24.50"),
        "currency": "GBP",
        "is_organic": False,
    },
    {
        "id": "demo-004",
        "name": f"{_SAMPLE_LABEL}Ceramic storage canisters (2-pack)",
        "category": "Kitchen",
        "description": "Illustrative sample set of two ceramic canisters.",
        "price": Decimal("15.00"),
        "currency": "CAD",
        "is_organic": False,
    },
    {
        "id": "demo-005",
        "name": f"{_SAMPLE_LABEL}Cotton tote bag",
        "category": "Accessories",
        "description": "Illustrative sample reusable cotton tote.",
        "price": Decimal("350.00"),
        "currency": "INR",
        "is_organic": False,
    },
    {
        "id": "demo-006",
        "name": f"{_SAMPLE_LABEL}Wireless headphones with active noise reduction",
        "category": "Wireless headphones",
        "description": "Illustrative sample over-ear wireless audio headset with foldable ear cups.",
        "price": Decimal("89.99"),
        "currency": "USD",
        "is_organic": False,
    },
    {
        "id": "demo-007",
        "name": f"{_SAMPLE_LABEL}Android smartphone (128 GB)",
        "category": "Smartphones",
        "description": "Illustrative sample touchscreen smartphone with dual rear cameras and 128 GB storage.",
        "price": Decimal("27999.00"),
        "currency": "INR",
        "is_organic": False,
    },
    {
        "id": "demo-008",
        "name": f"{_SAMPLE_LABEL}Compact smartphone (256 GB)",
        "category": "Smartphones",
        "description": "Illustrative sample compact smartphone with a long-lasting battery.",
        "price": Decimal("549.00"),
        "currency": "EUR",
        "is_organic": False,
    },
    {
        "id": "demo-009",
        "name": f"{_SAMPLE_LABEL}14-inch laptop (16 GB RAM)",
        "category": "Laptops",
        "description": "Illustrative sample laptop for everyday work with solid-state storage.",
        "price": Decimal("799.00"),
        "currency": "USD",
        "is_organic": False,
    },
    {
        "id": "demo-010",
        "name": f"{_SAMPLE_LABEL}Lightweight laptop (13-inch)",
        "category": "Laptops",
        "description": "Illustrative sample laptop with a lightweight chassis and 512 GB storage.",
        "price": Decimal("1049.00"),
        "currency": "EUR",
        "is_organic": False,
    },
    {
        "id": "demo-011",
        "name": f"{_SAMPLE_LABEL}GPS smartwatch",
        "category": "Smartwatches",
        "description": "Illustrative sample smartwatch with heart-rate tracking and GPS.",
        "price": Decimal("149.99"),
        "currency": "GBP",
        "is_organic": False,
    },
    {
        "id": "demo-012",
        "name": f"{_SAMPLE_LABEL}Multiport USB-C hub",
        "category": "Computer accessories",
        "description": "Illustrative sample USB-C adapter with HDMI and a card reader.",
        "price": Decimal("39.99"),
        "currency": "CAD",
        "is_organic": False,
    },
    {
        "id": "demo-013",
        "name": f"{_SAMPLE_LABEL}1080p webcam with privacy shutter",
        "category": "Computer accessories",
        "description": "Illustrative sample webcam with a built-in privacy shutter for video calls.",
        "price": Decimal("44.95"),
        "currency": "USD",
        "is_organic": False,
    },
    {
        "id": "demo-014",
        "name": f"{_SAMPLE_LABEL}Portable Bluetooth speaker",
        "category": "Speakers",
        "description": "Illustrative sample portable speaker with stereo audio and a rechargeable battery.",
        "price": Decimal("59.99"),
        "currency": "EUR",
        "is_organic": False,
    },
    {
        "id": "demo-015",
        "name": f"{_SAMPLE_LABEL}10,000 mAh power bank",
        "category": "Power banks",
        "description": "Illustrative sample power bank with USB-C charging.",
        "price": Decimal("1899.00"),
        "currency": "INR",
        "is_organic": False,
    },
    {
        "id": "demo-016",
        "name": f"{_SAMPLE_LABEL}Wireless keyboard",
        "category": "Keyboards and mice",
        "description": "Illustrative sample wireless keyboard with low-profile keys.",
        "price": Decimal("54.99"),
        "currency": "USD",
        "is_organic": False,
    },
    {
        "id": "demo-017",
        "name": f"{_SAMPLE_LABEL}Ergonomic wireless mouse",
        "category": "Keyboards and mice",
        "description": "Illustrative sample ergonomic wireless mouse with adjustable pointer speed.",
        "price": Decimal("29.99"),
        "currency": "CAD",
        "is_organic": False,
    },
    {
        "id": "demo-018",
        "name": f"{_SAMPLE_LABEL}Compact air purifier",
        "category": "Home appliances",
        "description": "Illustrative sample room air purifier with a washable pre-filter.",
        "price": Decimal("119.00"),
        "currency": "USD",
        "is_organic": False,
    },
    {
        "id": "demo-019",
        "name": f"{_SAMPLE_LABEL}Countertop air fryer",
        "category": "Home appliances",
        "description": "Illustrative sample compact air fryer with a digital timer.",
        "price": Decimal("79.99"),
        "currency": "GBP",
        "is_organic": False,
    },
    {
        "id": "demo-020",
        "name": f"{_SAMPLE_LABEL}Electric kettle (1.7 L)",
        "category": "Kitchen products",
        "description": "Illustrative sample electric kettle with auto shut-off.",
        "price": Decimal("34.99"),
        "currency": "GBP",
        "is_organic": False,
    },
    {
        "id": "demo-021",
        "name": f"{_SAMPLE_LABEL}Android smartphone (64 GB)",
        "category": "Smartphones",
        "description": "Illustrative sample Android smartphone for everyday use.",
        "price": Decimal("12999.00"),
        "currency": "INR",
        "is_organic": False,
    },
    {
        "id": "demo-022",
        "name": f"{_SAMPLE_LABEL}5G smartphone (128 GB)",
        "category": "Smartphones",
        "description": "Illustrative sample 5G smartphone with 128 GB storage.",
        "price": Decimal("16999.00"),
        "currency": "INR",
        "is_organic": False,
    },
    {
        "id": "demo-023",
        "name": f"{_SAMPLE_LABEL}Dual-SIM smartphone (128 GB)",
        "category": "Smartphones",
        "description": "Illustrative sample dual-SIM smartphone for general use.",
        "price": Decimal("18999.00"),
        "currency": "INR",
        "is_organic": False,
    },
)
_SAMPLE_PRODUCTS_BY_ID = {
    product["id"]: product for product in _SAMPLE_PRODUCTS
}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class DemoProviderAdapter(ProviderAdapter):
    @property
    def search_filter_capabilities(self) -> frozenset[str]:
        return frozenset({"min_price", "max_price", "category"})

    @property
    def provider_key(self) -> str:
        return "demo_catalog"

    @property
    def display_name(self) -> str:
        return "DealSync Demo Catalog (Sample Data)"

    @property
    def supports_external_cart(self) -> bool:
        return False

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

        offers: list[NormalizedOffer] = []
        for product in _SAMPLE_PRODUCTS:
            searchable_text = " ".join(
                str(product.get(field, ""))
                for field in ("name", "category", "description")
            )
            if query and query.casefold() not in searchable_text.casefold():
                continue
            if (
                is_organic is not None
                and product.get("is_organic") is not is_organic
            ):
                continue

            offer = self.normalize_offer(product)
            if not filters.matches(offer, product.get("category")):
                continue
            offers.append(offer)
        return offers

    def normalize_offer(
        self,
        product: Mapping[str, object],
    ) -> NormalizedOffer:
        product_id = product.get("id")
        if (
            not isinstance(product_id, str)
            or not product_id.startswith("demo-")
            or not product_id[5:].isascii()
            or not product_id[5:].isalnum()
        ):
            raise InvalidProductDataError(
                self.provider_key, "A demo product has an invalid ID."
            )

        name = product.get("name")
        if (
            not isinstance(name, str)
            or not name.startswith(_SAMPLE_LABEL)
            or not name[len(_SAMPLE_LABEL):].strip()
        ):
            raise InvalidProductDataError(
                self.provider_key, "A demo product is missing its sample label."
            )

        try:
            currency = validate_currency_code(product.get("currency"))
            price_minor = to_minor_units(product.get("price"), currency)
        except ValueError as exc:
            raise InvalidProductDataError(
                self.provider_key,
                "A demo product has invalid price or currency metadata.",
            ) from exc

        provider_offer_key = f"demo-product:{product_id}"
        offer_id = str(
            uuid5(
                NAMESPACE_OID,
                f"DealSync:{self.provider_key}:{provider_offer_key}",
            )
        )
        timestamp = _utc_timestamp()
        category = product.get("category")
        brand = product.get("brand")
        model = product.get("model")
        return NormalizedOffer(
            offer_id=offer_id,
            provider_key=self.provider_key,
            provider_offer_key=provider_offer_key,
            provider_product_id=product_id,
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
                provenance="demo_sample",
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
        if any(
            not isinstance(product_id, str) or not product_id
            for product_id in provider_product_ids
        ):
            raise InvalidProductDataError(
                self.provider_key, "A demo product reference is invalid."
            )
        if len(provider_product_ids) != len(set(provider_product_ids)):
            raise InvalidProductDataError(
                self.provider_key, "Demo product references must be unique."
            )

        resolved: list[ResolvedProviderProduct] = []
        for product_id in provider_product_ids:
            product = _SAMPLE_PRODUCTS_BY_ID.get(product_id)
            if product is None:
                continue
            offer = self.normalize_offer(product)
            resolved.append(
                ResolvedProviderProduct(
                    offer=offer,
                    category=str(product["category"]),
                    description=str(product["description"]),
                    is_organic=bool(product["is_organic"]),
                    average_rating=None,
                    review_count=None,
                    product_attributes=offer.product_attributes,
                )
            )
        return tuple(resolved)
