from __future__ import annotations

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)


Text40 = Annotated[str, StringConstraints(min_length=1, max_length=40)]
Text80 = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Text120 = Annotated[str, StringConstraints(min_length=1, max_length=120)]
Text160 = Annotated[str, StringConstraints(min_length=1, max_length=160)]
Text240 = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class VisualAnalysisConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_grocery_product: float | None = Field(default=None, ge=0, le=1)
    is_organic: float | None = Field(default=None, ge=0, le=1)
    product_category: float | None = Field(default=None, ge=0, le=1)
    product_type: float | None = Field(default=None, ge=0, le=1)
    brand: float | None = Field(default=None, ge=0, le=1)
    model: float | None = Field(default=None, ge=0, le=1)
    primary_color: float | None = Field(default=None, ge=0, le=1)
    secondary_colors: float | None = Field(default=None, ge=0, le=1)
    shape_or_design: float | None = Field(default=None, ge=0, le=1)
    distinctive_features: float | None = Field(default=None, ge=0, le=1)
    visible_text: float | None = Field(default=None, ge=0, le=1)
    search_query: float | None = Field(default=None, ge=0, le=1)


class VisualAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    is_grocery_product: StrictBool
    is_organic: StrictBool | None = None
    product_category: Text80 | None = None
    product_type: Text80 | None = None
    brand: Text80 | None = None
    model: Text80 | None = None
    primary_color: Text40 | None = None
    secondary_colors: list[Text40] = Field(default_factory=list, max_length=5)
    shape_or_design: Text120 | None = None
    distinctive_features: list[Text120] = Field(default_factory=list, max_length=6)
    visible_text: list[Text80] = Field(default_factory=list, max_length=6)
    search_query: Text160 | None = None
    description: Text240 | None = None
    confidence: VisualAnalysisConfidence = Field(
        default_factory=VisualAnalysisConfidence
    )

    @model_validator(mode="after")
    def clear_uncertain_identity(self) -> VisualAnalysis:
        for field_name in ("brand", "model"):
            confidence = getattr(self.confidence, field_name)
            if (
                getattr(self, field_name) is None
                or confidence is None
                or confidence < 0.6
            ):
                setattr(self, field_name, None)
                setattr(self.confidence, field_name, None)

        if self.is_grocery_product and (
            self.product_type is None or self.search_query is None
        ):
            raise ValueError(
                "Grocery product analyses require product_type and search_query."
            )
        return self

    def to_legacy_response(self) -> dict[str, bool | str | None]:
        is_grocery = self.is_grocery_product
        return {
            "is_grocery_product": is_grocery,
            "product_type": self.product_type if is_grocery else None,
            "search_query": self.search_query if is_grocery else None,
            "is_organic": self.is_organic if is_grocery else None,
            "description": self.description
            or (
                "The image does not appear to show a grocery product."
                if not is_grocery
                else "The image appears to show a grocery product."
            ),
        }
