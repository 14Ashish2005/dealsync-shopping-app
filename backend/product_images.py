from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTS_PUBLIC_DIR = PROJECT_ROOT / "frontend" / "public" / "images" / "products"
GENERIC_FALLBACK = "/images/products/categories/default.svg"
PRODUCT_IMAGE_FALLBACK = "/images/products/default.svg"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".svg", ".webp"}
CATEGORY_FALLBACKS = {
    "coffee": "/images/products/categories/coffee.svg",
    "dairy-alt": "/images/products/categories/dairy-alt.svg",
    "grains": "/images/products/categories/grains.svg",
    "honey": "/images/products/categories/honey.svg",
    "nuts": "/images/products/categories/nuts.svg",
    "oil": "/images/products/categories/oil.svg",
    "seeds": "/images/products/categories/seeds.svg",
    "snacks": "/images/products/categories/snacks.svg",
    "tea": "/images/products/categories/tea.svg",
}


def validate_image_url(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(character.isspace() or ord(character) < 32 for character in value):
        return None

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def validate_local_image_path(
    value: object,
    public_root: Path | None = None,
) -> str | None:
    if public_root is None:
        public_root = PRODUCTS_PUBLIC_DIR

    if (
        not isinstance(value, str)
        or not value.startswith("/images/products/")
        or value != value.strip()
        or any(character in value for character in ("\\", "%", "?", "#", ":"))
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return None

    relative_parts = value.removeprefix("/images/products/").split("/")
    if (
        not relative_parts
        or any(part in {"", ".", ".."} for part in relative_parts)
        or Path(relative_parts[-1]).suffix.casefold() not in IMAGE_EXTENSIONS
    ):
        return None

    root = public_root.resolve()
    candidate = root.joinpath(*relative_parts)
    try:
        resolved = candidate.resolve(strict=True)
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            return None
    except (OSError, ValueError):
        return None

    if not resolved.is_file():
        return None
    return value


def category_fallback(category: object) -> str:
    if isinstance(category, str):
        return CATEGORY_FALLBACKS.get(category.strip().casefold(), GENERIC_FALLBACK)
    return GENERIC_FALLBACK


def _read_product_metadata(
    product_ids: Sequence[int],
    database_path: str | Path,
) -> dict[int, tuple[str | None, str | None]]:
    if not product_ids:
        return {}

    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"Product database does not exist: {path}")

    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        connection.execute("PRAGMA query_only = ON")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'product_images'"
        ).fetchone()
        if exists is None:
            return {}

        placeholders = ",".join("?" for _ in product_ids)
        rows = connection.execute(
            "SELECT product_id, image_url, local_image_path "
            f"FROM product_images WHERE product_id IN ({placeholders})",
            tuple(product_ids),
        ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def enrich_product_images(
    products: Sequence[Mapping[str, object]],
    database_path: str | Path,
) -> list[dict[str, object]]:
    product_ids = list(dict.fromkeys(
        product["id"]
        for product in products
        if isinstance(product, Mapping)
        and isinstance(product.get("id"), int)
        and not isinstance(product.get("id"), bool)
    ))
    metadata = _read_product_metadata(product_ids, database_path)

    enriched: list[dict[str, object]] = []
    for product in products:
        if not isinstance(product, Mapping):
            raise ValueError("Product search returned an invalid product record.")
        product_id = product.get("id")
        image_url, local_image_path = (
            metadata.get(product_id, (None, None))
            if isinstance(product_id, int) and not isinstance(product_id, bool)
            else (None, None)
        )
        enriched.append({
            **product,
            "image_url": validate_image_url(image_url),
            "local_image_url": validate_local_image_path(local_image_path),
            "fallback_image": category_fallback(product.get("category")),
        })
    return enriched
