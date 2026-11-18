from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from pydantic import field_validator, model_validator

from backend.cart.models import (
    MAX_CART_QUANTITY,
    ExternalCart,
    ExternalCartItem,
    OfferAvailability,
    OfferQuoteStatus,
)


class AddExternalOfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_id: StrictStr = Field(min_length=1, max_length=256)
    quantity: StrictInt = Field(gt=0, le=MAX_CART_QUANTITY)

    @field_validator("offer_id")
    @classmethod
    def validate_offer_id(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("offer_id is invalid.")
        return value


class UpdateExternalOfferQuantityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: StrictInt = Field(gt=0, le=MAX_CART_QUANTITY)


class ConfirmExternalPriceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: StrictBool

    @model_validator(mode="after")
    def require_confirmation(self) -> ConfirmExternalPriceRequest:
        if not self.confirm:
            raise ValueError("Explicit price confirmation is required.")
        return self


class ExternalCartItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_id: StrictStr
    provider_key: StrictStr
    provider_offer_key: StrictStr
    provider_product_id: StrictStr
    product_name: StrictStr
    price_minor: StrictInt
    currency: StrictStr
    quantity: StrictInt
    snapshot_subtotal_minor: StrictInt
    product_url: StrictStr | None
    availability: OfferAvailability
    verified_at: StrictStr | None
    added_at: StrictStr
    quote_status: OfferQuoteStatus
    pending_price_minor: StrictInt | None
    pending_currency: StrictStr | None
    pending_verified_at: StrictStr | None
    pending_product_name: StrictStr | None
    pending_product_url: StrictStr | None
    pending_availability: OfferAvailability | None

    @classmethod
    def from_item(cls, item: ExternalCartItem) -> ExternalCartItemResponse:
        return cls(
            offer_id=item.offer_id,
            provider_key=item.provider_key,
            provider_offer_key=item.provider_offer_key,
            provider_product_id=item.provider_product_id,
            product_name=item.product_name,
            price_minor=item.price_minor,
            currency=item.currency,
            quantity=item.quantity,
            snapshot_subtotal_minor=item.snapshot_subtotal_minor,
            product_url=item.product_url,
            availability=item.availability,
            verified_at=item.verified_at,
            added_at=item.added_at,
            quote_status=item.quote_status,
            pending_price_minor=item.pending_price_minor,
            pending_currency=item.pending_currency,
            pending_verified_at=item.pending_verified_at,
            pending_product_name=item.pending_product_name,
            pending_product_url=item.pending_product_url,
            pending_availability=item.pending_availability,
        )


class ExternalCartCurrencyTotalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: StrictStr
    snapshot_total_minor: StrictInt
    item_count: StrictInt
    all_quotes_current: StrictBool


class ExternalCartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[ExternalCartItemResponse, ...]
    currency_totals: tuple[ExternalCartCurrencyTotalResponse, ...]

    @classmethod
    def from_cart(cls, cart: ExternalCart) -> ExternalCartResponse:
        return cls(
            items=tuple(
                ExternalCartItemResponse.from_item(item)
                for item in cart.items
            ),
            currency_totals=tuple(
                ExternalCartCurrencyTotalResponse(
                    currency=total.currency,
                    snapshot_total_minor=total.snapshot_total_minor,
                    item_count=total.item_count,
                    all_quotes_current=total.all_quotes_current,
                )
                for total in cart.currency_totals
            ),
        )
