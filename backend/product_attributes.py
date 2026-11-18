from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal


AttributeProvenance = Literal[
    "verified_provider",
    "local_catalog",
    "demo_sample",
    "unknown",
]
AttributeExtractionMethod = Literal["direct", "product_title"]
AttributeValue = str | int | float | bool | None

_ATTRIBUTE_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_STANDARD_ATTRIBUTE_NAMES = (
    "brand",
    "model",
    "category",
    "storage_capacity",
    "ram",
    "color",
    "size",
    "weight",
    "pack_quantity",
)
_RESERVED_ATTRIBUTE_NAMES = {
    "availability",
    "description",
    "image_url",
    "is_organic",
    "price",
    "product_name",
    "product_url",
    "review_count",
    "average_rating",
    "specifications",
}
_RAM_AFTER_VALUE_PATTERN = re.compile(
    r"(?<![\w.])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>GB|TB)\s*RAM\b",
    re.IGNORECASE,
)
_RAM_BEFORE_VALUE_PATTERN = re.compile(
    r"\bRAM\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>GB|TB)\b",
    re.IGNORECASE,
)
_CAPACITY_PATTERN = re.compile(
    r"(?<![\w.])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>GB|TB)\b",
    re.IGNORECASE,
)
_COLOR_PATTERN = re.compile(
    r"\b(?:color|colour)\s*[:=]\s*(?P<value>[^,;|()]+)",
    re.IGNORECASE,
)
_SIZE_LABEL_PATTERN = re.compile(
    r"\bsize\s*[:=]\s*(?P<value>[^,;|()]+)",
    re.IGNORECASE,
)
_SIZE_MEASURE_PATTERN = re.compile(
    r"(?<![\w.])(?P<value>\d+(?:\.\d+)?)[\s-]*(?P<unit>inches?|in)\b",
    re.IGNORECASE,
)
_WEIGHT_PATTERN = re.compile(
    r"(?<![\w.])(?P<value>\d+(?:\.\d+)?)\s+(?P<unit>mg|g|kg|oz|lbs?)\b",
    re.IGNORECASE,
)
_PACK_PATTERN = re.compile(
    r"(?<![\w.])(?P<value>\d+)\s*[- ]?\s*"
    r"(?P<unit>packs?|count|pieces?|pcs)\b",
    re.IGNORECASE,
)
_LABELLED_TEXT_PATTERNS = {
    "brand": re.compile(
        r"\bbrand\s*[:=]\s*(?P<value>[^,;|()]+)",
        re.IGNORECASE,
    ),
    "model": re.compile(
        r"\bmodel\s*[:=]\s*(?P<value>[^,;|()]+)",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class ProductAttribute:
    name: str
    value: AttributeValue
    provenance: AttributeProvenance
    extraction_method: AttributeExtractionMethod | None = "direct"
    unit: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ATTRIBUTE_NAME_PATTERN.fullmatch(
            self.name
        ):
            raise ValueError("Product attribute names must be normalized identifiers.")
        if self.name in _RESERVED_ATTRIBUTE_NAMES:
            raise ValueError(f"{self.name!r} is not a product specification attribute.")
        if self.provenance not in {
            "verified_provider",
            "local_catalog",
            "demo_sample",
            "unknown",
        }:
            raise ValueError("Product attribute provenance is not supported.")
        if self.value is not None and not isinstance(
            self.value,
            (str, int, float, bool),
        ):
            raise ValueError("Product attribute values must be scalar.")
        if (self.value is None) != (self.provenance == "unknown"):
            raise ValueError(
                "Unknown attributes must have no value; sourced attributes need one."
            )
        if self.provenance == "unknown":
            if self.extraction_method is not None or self.unit is not None:
                raise ValueError(
                    "Unknown attributes cannot have an extraction method or unit."
                )
        elif self.extraction_method not in {"direct", "product_title"}:
            raise ValueError(
                "Sourced attributes require a supported extraction method."
            )
        if isinstance(self.value, str) and not self.value.strip():
            raise ValueError("Product attribute text values cannot be blank.")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("Product attribute numeric values must be finite.")
        if self.unit is not None and (
            not isinstance(self.unit, str) or not self.unit.strip()
        ):
            raise ValueError("Product attribute units must be non-empty text.")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _unknown_attribute(name: str) -> ProductAttribute:
    return ProductAttribute(
        name=name,
        value=None,
        provenance="unknown",
        extraction_method=None,
    )


def _numeric_value(raw_value: str) -> str | int | float:
    try:
        value = float(raw_value)
    except (OverflowError, ValueError):
        return raw_value
    if not math.isfinite(value):
        return raw_value
    if value == 0 and any(character in "123456789" for character in raw_value):
        return raw_value
    if value.is_integer():
        try:
            return int(raw_value.split(".", 1)[0])
        except ValueError:
            return raw_value
    return value


def _title_attribute(
    name: str,
    value: str | int | float,
    provenance: AttributeProvenance,
    *,
    unit: str | None = None,
) -> ProductAttribute:
    return ProductAttribute(
        name=name,
        value=value,
        provenance=provenance,
        extraction_method="product_title",
        unit=unit,
    )


def _extract_title_attributes(
    title: str,
    provenance: AttributeProvenance,
) -> dict[str, ProductAttribute]:
    extracted: dict[str, ProductAttribute] = {}
    if provenance == "unknown":
        return extracted

    for name, pattern in _LABELLED_TEXT_PATTERNS.items():
        match = pattern.search(title)
        if match is not None and match.group("value").strip():
            extracted[name] = _title_attribute(
                name,
                match.group("value").strip(),
                provenance,
            )

    ram_matches = [
        *list(_RAM_AFTER_VALUE_PATTERN.finditer(title)),
        *list(_RAM_BEFORE_VALUE_PATTERN.finditer(title)),
    ]
    if len(ram_matches) == 1:
        match = ram_matches[0]
        extracted["ram"] = _title_attribute(
            "ram",
            _numeric_value(match.group("value")),
            provenance,
            unit=match.group("unit").upper(),
        )

    capacity_matches = []
    for match in _CAPACITY_PATTERN.finditer(title):
        if any(
            match.start() < ram.end() and ram.start() < match.end()
            for ram in ram_matches
        ):
            continue
        capacity_matches.append(match)
    if len(capacity_matches) == 1:
        match = capacity_matches[0]
        extracted["storage_capacity"] = _title_attribute(
            "storage_capacity",
            _numeric_value(match.group("value")),
            provenance,
            unit=match.group("unit").upper(),
        )

    color_match = _COLOR_PATTERN.search(title)
    if color_match is not None and color_match.group("value").strip():
        extracted["color"] = _title_attribute(
            "color",
            color_match.group("value").strip(),
            provenance,
        )

    size_match = _SIZE_LABEL_PATTERN.search(title)
    if size_match is not None and size_match.group("value").strip():
        extracted["size"] = _title_attribute(
            "size",
            size_match.group("value").strip(),
            provenance,
        )
    else:
        size_match = _SIZE_MEASURE_PATTERN.search(title)
        if size_match is not None:
            size_unit = size_match.group("unit").casefold()
            extracted["size"] = _title_attribute(
                "size",
                _numeric_value(size_match.group("value")),
                provenance,
                unit="inch" if size_unit in {"in", "inch", "inches"} else size_unit,
            )

    weight_match = _WEIGHT_PATTERN.search(title)
    if weight_match is not None:
        extracted["weight"] = _title_attribute(
            "weight",
            _numeric_value(weight_match.group("value")),
            provenance,
            unit=weight_match.group("unit").casefold(),
        )

    pack_match = _PACK_PATTERN.search(title)
    if pack_match is not None:
        extracted["pack_quantity"] = _title_attribute(
            "pack_quantity",
            int(pack_match.group("value")),
            provenance,
            unit="count",
        )

    return extracted


def build_product_attributes(
    *,
    title: str,
    provenance: AttributeProvenance,
    brand: object = None,
    model: object = None,
    category: object = None,
    additional_attributes: Sequence[ProductAttribute] = (),
) -> tuple[ProductAttribute, ...]:
    if provenance not in {"verified_provider", "local_catalog", "demo_sample"}:
        raise ValueError("Product attributes require a data provenance.")

    attributes = {
        name: _unknown_attribute(name)
        for name in _STANDARD_ATTRIBUTE_NAMES
    }

    for name, value in (
        ("brand", brand),
        ("model", model),
        ("category", category),
    ):
        if isinstance(value, str) and value.strip():
            attributes[name] = ProductAttribute(
                name=name,
                value=value.strip(),
                provenance=provenance,
            )

    for name, attribute in _extract_title_attributes(title, provenance).items():
        if attributes[name].provenance == "unknown":
            attributes[name] = attribute

    extra_names: list[str] = []
    for attribute in additional_attributes:
        if attribute.name in attributes:
            existing = attributes[attribute.name]
            if existing.provenance == "unknown":
                attributes[attribute.name] = attribute
                continue
            if attribute.extraction_method == "direct":
                if (
                    existing.extraction_method == "direct"
                    and existing.value != attribute.value
                ):
                    raise ValueError(
                        f"Product attribute {attribute.name!r} has conflicting direct values."
                    )
                attributes[attribute.name] = attribute
                continue
            if existing != attribute:
                raise ValueError(
                    f"Product attribute {attribute.name!r} has conflicting values."
                )
            continue
        if attribute.name in extra_names:
            raise ValueError(f"Additional attribute {attribute.name!r} is duplicated.")
        extra_names.append(attribute.name)
        attributes[attribute.name] = attribute

    return tuple(attributes.values())
