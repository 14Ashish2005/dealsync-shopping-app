from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from backend.currency import MAX_SQLITE_INTEGER, parse_major_amount, to_minor_units


@dataclass(frozen=True)
class SearchIntent:
    original_query: str
    normalized_search_query: str
    product_category: str | None
    budget_amount: Decimal | None
    budget_currency: str | None
    preferences: tuple[str, ...]
    unsupported_requirements: tuple[str, ...]
    clarification_required: bool
    minimum_budget_amount: Decimal | None = None


_CURRENCY_TOKEN_SOURCE = (
    r"(?:US\$|C\$|A\$|INR|USD|EUR|GBP|CAD|AUD|JPY|CNY|₹|\$|€|£|¥)"
)
_BUDGET_AMOUNT_SOURCE = r"[+-]?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?"
_MINIMUM_BUDGET_MARKER_SOURCE = (
    r"(?:greater\s+than|at\s+least|minimum|min|above|over)"
)
_MAXIMUM_BUDGET_MARKER_SOURCE = (
    r"(?:less\s+than|no\s+more\s+than|up\s+to|upto|at\s+most|"
    r"under|below|maximum|max|budget(?:\s+of)?)"
)
_BUDGET_MARKER_SOURCE = (
    rf"(?:{_MINIMUM_BUDGET_MARKER_SOURCE}|"
    rf"{_MAXIMUM_BUDGET_MARKER_SOURCE}|between)"
)
_BUDGET_MARKER_RE = re.compile(rf"\b{_BUDGET_MARKER_SOURCE}\b", re.IGNORECASE)
_BUDGET_CLAUSE_RE = re.compile(
    rf"\b(?P<marker>{_MINIMUM_BUDGET_MARKER_SOURCE}|"
    rf"{_MAXIMUM_BUDGET_MARKER_SOURCE})\b\s*"
    rf"(?P<prefix>{_CURRENCY_TOKEN_SOURCE})?\s*"
    rf"(?P<amount>{_BUDGET_AMOUNT_SOURCE})"
    rf"\s*(?P<suffix>{_CURRENCY_TOKEN_SOURCE})?"
    rf"(?![\w.,])",
    re.IGNORECASE,
)
_BUDGET_RANGE_RE = re.compile(
    rf"\b(?P<marker>between)\b\s*"
    rf"(?P<minimum_prefix>{_CURRENCY_TOKEN_SOURCE})?\s*"
    rf"(?P<minimum_amount>{_BUDGET_AMOUNT_SOURCE})\s*"
    rf"(?P<minimum_suffix>{_CURRENCY_TOKEN_SOURCE})?"
    rf"(?![\w.,])"
    rf"\s+and\s+"
    rf"(?P<maximum_prefix>{_CURRENCY_TOKEN_SOURCE})?\s*"
    rf"(?P<maximum_amount>{_BUDGET_AMOUNT_SOURCE})\s*"
    rf"(?P<maximum_suffix>{_CURRENCY_TOKEN_SOURCE})?"
    rf"(?![\w.,])",
    re.IGNORECASE,
)
_CURRENCY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z])(?P<code>INR|USD|EUR|GBP|CAD|AUD|JPY|CNY)(?![A-Za-z])"
    r"|(?P<symbol>US\$|C\$|A\$|₹|\$|€|£|¥)",
    re.IGNORECASE,
)
_CURRENCY_CODES = {
    "INR": "INR",
    "USD": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
    "CAD": "CAD",
    "AUD": "AUD",
    "JPY": "JPY",
    "CNY": None,
}
_CURRENCY_SYMBOLS = {
    "₹": "INR",
    "$": None,
    "US$": "USD",
    "C$": "CAD",
    "A$": "AUD",
    "€": "EUR",
    "£": "GBP",
    "¥": None,
}

_PREFERENCE_PATTERNS = (
    (
        "camera quality",
        re.compile(
            r"\b(?:with\s+)?(?:good|great|better|excellent|high[- ]quality)\s+"
            r"camera(?:\s+quality)?\b|\bcamera\s+quality\b",
            re.IGNORECASE,
        ),
    ),
    (
        "battery life",
        re.compile(
            r"\b(?:with\s+)?(?:long[- ]lasting|long|extended|good|excellent)?\s*"
            r"battery\s+life\b|\b(?:with\s+)?long[- ]lasting\s+battery\b",
            re.IGNORECASE,
        ),
    ),
    (
        "programming suitability",
        re.compile(
            r"\b(?:for\s+)?(?:programming|coding|software development)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "performance",
        re.compile(
            r"\b(?:with\s+)?(?:high|strong|good)\s+performance\b"
            r"|\bperformance\b",
            re.IGNORECASE,
        ),
    ),
    (
        "portability",
        re.compile(r"\b(?:portable|lightweight|portability)\b", re.IGNORECASE),
    ),
    ("best", re.compile(r"\b(?:best|top[- ]rated)\b", re.IGNORECASE)),
)

_CATEGORY_PATTERNS = (
    ("Wireless headphones", re.compile(r"\bwireless\s+headphones?\b", re.I)),
    ("Headphones", re.compile(r"\bheadphones?\b", re.I)),
    ("Smartphones", re.compile(r"\bsmartphones?\b", re.I)),
    ("Laptops", re.compile(r"\blaptops?\b", re.I)),
    ("Smartwatches", re.compile(r"\bsmart\s*watch(?:es)?\b", re.I)),
    ("Computer accessories", re.compile(r"\bcomputer\s+accessories\b", re.I)),
    ("Speakers", re.compile(r"\bspeakers?\b", re.I)),
    ("Power banks", re.compile(r"\bpower[\s-]+banks?\b", re.I)),
    (
        "Keyboards and mice",
        re.compile(r"\b(?:keyboards?|mice|computer\s+mice)\b", re.I),
    ),
    ("Home appliances", re.compile(r"\bhome\s+appliances?\b", re.I)),
    ("Kitchen products", re.compile(r"\bkitchen\s+products?\b", re.I)),
)

_REQUEST_PREFIX_RE = re.compile(
    r"^\s*(?:(?:please\s+)?(?:find|search\s+for|show\s+me|look\s+for|"
    r"i\s+need|i\s+want|looking\s+for)\s+)+",
    re.IGNORECASE,
)
_ARTICLE_PREFIX_RE = re.compile(r"^\s*(?:a|an|the)\s+", re.IGNORECASE)


def _resolve_currency(query: str) -> tuple[str | None, tuple[str, ...]]:
    currencies: set[str] = set()
    issues: list[str] = []
    ambiguous_symbols: set[str] = set()

    for match in _CURRENCY_TOKEN_RE.finditer(query):
        if match.group("code") is not None:
            token = match.group("code").upper()
            currency = _CURRENCY_CODES[token]
            if currency is None:
                issues.append(f"Currency {token} is not supported.")
            else:
                currencies.add(currency)
            continue

        symbol = match.group("symbol")
        currency = _CURRENCY_SYMBOLS[symbol.upper() if symbol.isascii() else symbol]
        if currency is None:
            ambiguous_symbols.add(symbol)
        else:
            currencies.add(currency)

    if len(currencies) > 1:
        issues.append("Multiple currencies are present; choose one.")
    if "$" in ambiguous_symbols:
        issues.append(
            "The $ symbol is ambiguous; specify which currency you mean using an ISO currency code."
        )
    if "¥" in ambiguous_symbols:
        issues.append("The ¥ symbol is ambiguous; use an ISO currency code.")

    if issues or len(currencies) != 1:
        return None, tuple(dict.fromkeys(issues))
    return next(iter(currencies)), ()


def _parse_budget_amount(raw_amount: str, currency: str | None) -> Decimal:
    if "e" in raw_amount.lower():
        raise ValueError("Scientific notation is not supported.")

    integer_part = raw_amount.lstrip("+-").split(".", 1)[0]
    if "," in integer_part and not re.fullmatch(r"\d{1,3}(?:,\d{3})+", integer_part):
        raise ValueError("The thousands separators are malformed.")

    amount = parse_major_amount(raw_amount.replace(",", ""))
    if currency is not None:
        to_minor_units(amount, currency)
    else:
        minor_units = amount * 100
        if (
            minor_units != minor_units.to_integral_value()
            or minor_units > MAX_SQLITE_INTEGER
        ):
            raise ValueError("The amount is outside the supported numeric range.")
    return amount


def _extract_budget(
    query: str,
) -> tuple[
    Decimal | None,
    Decimal | None,
    str | None,
    bool,
    tuple[str, ...],
    list[tuple[int, int]],
]:
    clause_matches = list(_BUDGET_CLAUSE_RE.finditer(query))
    range_matches = list(_BUDGET_RANGE_RE.finditer(query))
    markers = list(_BUDGET_MARKER_RE.finditer(query))
    matches = [*clause_matches, *range_matches]
    spans = sorted((match.start(), match.end()) for match in matches)

    if not markers:
        return None, None, None, False, (), spans

    recognized_marker_spans = {
        match.span("marker") for match in matches
    }
    unmatched_markers = [
        marker for marker in markers if marker.span() not in recognized_marker_spans
    ]
    spans.extend((marker.start(), marker.end()) for marker in unmatched_markers)
    spans.sort()

    if unmatched_markers:
        currency, currency_issues = _resolve_currency(query)
        return (
            None,
            None,
            currency,
            True,
            (*currency_issues, "Budget amount or range is missing or malformed."),
            spans,
        )

    if not matches:
        currency, currency_issues = _resolve_currency(query)
        return (
            None,
            None,
            currency,
            True,
            (*currency_issues, "Budget amount or range is missing or malformed."),
            spans,
        )

    bounds: list[tuple[str, str]] = []
    for match in range_matches:
        bounds.extend(
            (
                ("minimum", match.group("minimum_amount")),
                ("maximum", match.group("maximum_amount")),
            )
        )

    minimum_markers = {
        "above",
        "at least",
        "greater than",
        "min",
        "minimum",
        "over",
    }
    for match in clause_matches:
        marker = " ".join(match.group("marker").casefold().split())
        direction = "minimum" if marker in minimum_markers else "maximum"
        bounds.append((direction, match.group("amount")))

    minimum_bounds = [amount for direction, amount in bounds if direction == "minimum"]
    maximum_bounds = [amount for direction, amount in bounds if direction == "maximum"]
    if len(minimum_bounds) > 1 or len(maximum_bounds) > 1:
        currency, currency_issues = _resolve_currency(query)
        return (
            None,
            None,
            currency,
            True,
            (*currency_issues, "Multiple budget amounts require clarification."),
            spans,
        )

    currency, currency_issues = _resolve_currency(query)
    parsed_bounds: dict[str, Decimal] = {}
    try:
        for direction, raw_amount in bounds:
            parsed_bounds[direction] = _parse_budget_amount(raw_amount, currency)
    except ValueError:
        return (
            None,
            None,
            currency,
            True,
            (*currency_issues, "Budget amount is malformed or out of range."),
            spans,
        )

    issues = list(currency_issues)
    if currency is None and not issues:
        issues.append("Budget currency is missing; select a currency.")
    minimum_amount = parsed_bounds.get("minimum")
    maximum_amount = parsed_bounds.get("maximum")
    if (
        currency is not None
        and not currency_issues
        and minimum_amount is not None
        and maximum_amount is not None
        and minimum_amount > maximum_amount
    ):
        issues.append("Minimum budget exceeds maximum budget; clarify the range.")

    return (
        minimum_amount,
        maximum_amount,
        currency,
        bool(issues),
        tuple(dict.fromkeys(issues)),
        spans,
    )


def _normalize_search_query(
    query: str, removed_spans: list[tuple[int, int]]
) -> tuple[str, tuple[str, ...]]:
    normalized = query
    merged_spans: list[tuple[int, int]] = []
    for start, end in sorted(removed_spans):
        if merged_spans and re.fullmatch(
            r"\s*\band\b\s*",
            query[merged_spans[-1][1] : start],
            re.IGNORECASE,
        ):
            previous_start, _previous_end = merged_spans[-1]
            merged_spans[-1] = (previous_start, end)
        else:
            merged_spans.append((start, end))

    for start, end in reversed(merged_spans):
        normalized = normalized[:start] + " " + normalized[end:]

    normalized = _BUDGET_MARKER_RE.sub(" ", normalized)
    preferences: list[str] = []
    for preference, pattern in _PREFERENCE_PATTERNS:
        if pattern.search(normalized):
            preferences.append(preference)
            normalized = pattern.sub(" ", normalized)

    normalized = _REQUEST_PREFIX_RE.sub("", normalized)
    normalized = _ARTICLE_PREFIX_RE.sub("", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" \t\r\n,.;:!?")
    return normalized.casefold(), tuple(preferences)


def _find_product_category(search_query: str) -> str | None:
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(search_query):
            return category
    return None


def parse_search_intent(query: str) -> SearchIntent:
    """Parse shopping intent deterministically without inferring product evidence."""
    if not isinstance(query, str):
        raise TypeError("Search query must be a string.")

    (
        minimum_amount,
        maximum_amount,
        currency,
        clarification_required,
        budget_issues,
        budget_spans,
    ) = _extract_budget(query)
    normalized_query, preferences = _normalize_search_query(query, budget_spans)
    unsupported_requirements = tuple(
        dict.fromkeys((*preferences, *budget_issues))
    )

    return SearchIntent(
        original_query=query,
        normalized_search_query=normalized_query,
        product_category=_find_product_category(normalized_query),
        minimum_budget_amount=minimum_amount,
        budget_amount=maximum_amount,
        budget_currency=currency,
        preferences=preferences,
        unsupported_requirements=unsupported_requirements,
        clarification_required=clarification_required,
    )
