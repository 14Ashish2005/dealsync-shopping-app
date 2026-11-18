import unittest
from dataclasses import fields
from decimal import Decimal

from backend.discovery.intent_parser import SearchIntent, parse_search_intent


class SearchIntentParserTests(unittest.TestCase):
    def test_examples_are_parsed_without_inventing_product_evidence(self):
        examples = (
            (
                "Best wireless headphones under ₹2,000",
                "wireless headphones",
                "Wireless headphones",
                Decimal("2000"),
                "INR",
                ("best",),
            ),
            (
                "Smartphone under ₹20,000 with good camera",
                "smartphone",
                "Smartphones",
                Decimal("20000"),
                "INR",
                ("camera quality",),
            ),
            (
                "Laptop for programming under ₹50,000",
                "laptop",
                "Laptops",
                Decimal("50000"),
                "INR",
                ("programming suitability",),
            ),
            (
                "Smartwatch with long battery life",
                "smartwatch",
                "Smartwatches",
                None,
                None,
                ("battery life",),
            ),
            (
                "Find a power bank",
                "power bank",
                "Power banks",
                None,
                None,
                (),
            ),
            (
                "Find laptops under 50000 INR",
                "laptops",
                "Laptops",
                Decimal("50000"),
                "INR",
                (),
            ),
        )

        expected_fields = {
            "original_query",
            "normalized_search_query",
            "product_category",
            "minimum_budget_amount",
            "budget_amount",
            "budget_currency",
            "preferences",
            "unsupported_requirements",
            "clarification_required",
        }
        self.assertEqual({field.name for field in fields(SearchIntent)}, expected_fields)

        for query, normalized, category, amount, currency, preferences in examples:
            with self.subTest(query=query):
                intent = parse_search_intent(query)
                self.assertEqual(intent.original_query, query)
                self.assertEqual(intent.normalized_search_query, normalized)
                self.assertEqual(intent.product_category, category)
                self.assertIsNone(intent.minimum_budget_amount)
                self.assertEqual(intent.budget_amount, amount)
                self.assertEqual(intent.budget_currency, currency)
                self.assertEqual(intent.preferences, preferences)
                self.assertEqual(intent.unsupported_requirements, preferences)
                self.assertEqual(
                    intent.clarification_required,
                    amount is not None and currency is None,
                )
                self.assertFalse(hasattr(intent, "rating"))
                self.assertFalse(hasattr(intent, "specifications"))

    def test_explicit_currency_symbols_and_codes_are_recognized(self):
        cases = (
            ("Headphones under ₹100", Decimal("100"), "INR"),
            ("Headphones under 100 INR", Decimal("100"), "INR"),
            ("Headphones under USD 100", Decimal("100"), "USD"),
            ("Headphones under US$100", Decimal("100"), "USD"),
            ("Laptop under 100 EUR", Decimal("100"), "EUR"),
            ("Laptop under 100 C$", Decimal("100"), "CAD"),
            ("Laptop under £100", Decimal("100"), "GBP"),
        )
        for query, amount, currency in cases:
            with self.subTest(query=query):
                intent = parse_search_intent(query)
                self.assertEqual(intent.budget_amount, amount)
                self.assertEqual(intent.budget_currency, currency)
                self.assertFalse(intent.clarification_required)

    def test_bare_dollar_symbol_requires_currency_clarification(self):
        intent = parse_search_intent("Headphones under $100")

        self.assertEqual(intent.budget_amount, Decimal("100"))
        self.assertIsNone(intent.minimum_budget_amount)
        self.assertIsNone(intent.budget_currency)
        self.assertTrue(intent.clarification_required)
        self.assertTrue(
            any("$ symbol is ambiguous" in item for item in intent.unsupported_requirements)
        )

    def test_missing_or_ambiguous_currency_requires_clarification(self):
        missing = parse_search_intent("Laptop under 50000")
        self.assertEqual(missing.budget_amount, Decimal("50000"))
        self.assertIsNone(missing.minimum_budget_amount)
        self.assertIsNone(missing.budget_currency)
        self.assertTrue(missing.clarification_required)

        ambiguous = parse_search_intent("Smartphone under $20,000 CAD")
        self.assertEqual(ambiguous.budget_amount, Decimal("20000"))
        self.assertIsNone(ambiguous.budget_currency)
        self.assertTrue(ambiguous.clarification_required)
        self.assertTrue(ambiguous.unsupported_requirements)

        ambiguous_yen = parse_search_intent("Laptop under ¥50000")
        self.assertIsNone(ambiguous_yen.budget_currency)
        self.assertTrue(ambiguous_yen.clarification_required)

    def test_supported_maximum_budget_markers_are_parsed(self):
        for marker in (
            "under",
            "below",
            "up to",
            "at most",
            "maximum",
            "max",
            "less than",
            "no more than",
            "budget of",
        ):
            with self.subTest(marker=marker):
                intent = parse_search_intent(
                    f"Laptop {marker} USD 100"
                )
                self.assertIsNone(intent.minimum_budget_amount)
                self.assertEqual(intent.budget_amount, Decimal("100"))
                self.assertEqual(intent.budget_currency, "USD")
                self.assertFalse(intent.clarification_required)

    def test_minimum_budget_markers_are_parsed(self):
        for marker in (
            "above",
            "over",
            "greater than",
            "at least",
            "minimum",
            "min",
        ):
            with self.subTest(marker=marker):
                intent = parse_search_intent(
                    f"Laptop {marker} USD 100"
                )
                self.assertEqual(intent.minimum_budget_amount, Decimal("100"))
                self.assertIsNone(intent.budget_amount)
                self.assertEqual(intent.budget_currency, "USD")
                self.assertFalse(intent.clarification_required)

    def test_budget_ranges_and_combined_minimum_maximum_are_parsed(self):
        range_intent = parse_search_intent(
            "Smartphones between ₹10,000 and ₹20,000"
        )
        self.assertEqual(range_intent.normalized_search_query, "smartphones")
        self.assertEqual(range_intent.minimum_budget_amount, Decimal("10000"))
        self.assertEqual(range_intent.budget_amount, Decimal("20000"))
        self.assertEqual(range_intent.budget_currency, "INR")
        self.assertFalse(range_intent.clarification_required)

        combined_intent = parse_search_intent(
            "Smartphones over USD 100 and max USD 500"
        )
        self.assertEqual(combined_intent.normalized_search_query, "smartphones")
        self.assertEqual(combined_intent.minimum_budget_amount, Decimal("100"))
        self.assertEqual(combined_intent.budget_amount, Decimal("500"))
        self.assertEqual(combined_intent.budget_currency, "USD")
        self.assertFalse(combined_intent.clarification_required)

    def test_valid_decimal_and_grouped_amounts_are_parsed_completely(self):
        cases = (
            ("Laptop under 100 USD", None, Decimal("100"), "USD"),
            ("Laptop under 1,000 INR", None, Decimal("1000"), "INR"),
            ("Laptop under 10,000.50 INR", None, Decimal("10000.50"), "INR"),
            ("Laptop above 1.5 USD", Decimal("1.5"), None, "USD"),
        )
        for query, minimum, maximum, currency in cases:
            with self.subTest(query=query):
                intent = parse_search_intent(query)
                self.assertEqual(intent.minimum_budget_amount, minimum)
                self.assertEqual(intent.budget_amount, maximum)
                self.assertEqual(intent.budget_currency, currency)
                self.assertFalse(intent.clarification_required)

        combined = parse_search_intent(
            "Laptop above USD 1.5 and max USD 10,000.50"
        )
        self.assertEqual(combined.minimum_budget_amount, Decimal("1.5"))
        self.assertEqual(combined.budget_amount, Decimal("10000.50"))
        self.assertEqual(combined.budget_currency, "USD")
        self.assertFalse(combined.clarification_required)

    def test_mixed_currencies_and_contradictory_ranges_require_clarification(self):
        mixed = parse_search_intent(
            "Smartphones between ₹20,000 and USD 10,000"
        )
        self.assertIsNone(mixed.budget_currency)
        self.assertTrue(mixed.clarification_required)
        self.assertFalse(
            any("exceeds maximum" in item for item in mixed.unsupported_requirements)
        )

        reversed_range = parse_search_intent(
            "Smartphones between INR 20,000 and INR 10,000"
        )
        self.assertEqual(reversed_range.minimum_budget_amount, Decimal("20000"))
        self.assertEqual(reversed_range.budget_amount, Decimal("10000"))
        self.assertTrue(reversed_range.clarification_required)
        self.assertTrue(
            any("exceeds maximum" in item for item in reversed_range.unsupported_requirements)
        )

        combined = parse_search_intent(
            "Smartphones at least USD 200 and max USD 100"
        )
        self.assertEqual(combined.minimum_budget_amount, Decimal("200"))
        self.assertEqual(combined.budget_amount, Decimal("100"))
        self.assertTrue(combined.clarification_required)

    def test_malformed_and_unrepresentable_amounts_require_clarification(self):
        malformed = parse_search_intent("Laptop under ₹1,2,000")
        self.assertIsNone(malformed.budget_amount)
        self.assertIsNone(malformed.minimum_budget_amount)
        self.assertEqual(malformed.budget_currency, "INR")
        self.assertTrue(malformed.clarification_required)
        self.assertEqual(malformed.normalized_search_query, "laptop")

        negative = parse_search_intent("Laptop under -500 INR")
        self.assertIsNone(negative.budget_amount)
        self.assertTrue(negative.clarification_required)

        negative_minimum = parse_search_intent("Laptop above -500 INR")
        self.assertIsNone(negative_minimum.minimum_budget_amount)
        self.assertTrue(negative_minimum.clarification_required)

        malformed_range = parse_search_intent(
            "Laptop between ₹20,000 and ₹1,2,000"
        )
        self.assertIsNone(malformed_range.minimum_budget_amount)
        self.assertIsNone(malformed_range.budget_amount)
        self.assertTrue(malformed_range.clarification_required)

        over_precision = parse_search_intent("Laptop under 12.345 USD")
        self.assertIsNone(over_precision.budget_amount)
        self.assertTrue(over_precision.clarification_required)

        for query in (
            "Laptop under 1.2.3 USD",
            "Laptop under 100abc USD",
            "Laptop under 10,00,000 INR",
        ):
            with self.subTest(query=query):
                malformed_token = parse_search_intent(query)
                self.assertIsNone(malformed_token.budget_amount)
                self.assertIsNone(malformed_token.minimum_budget_amount)
                self.assertTrue(malformed_token.clarification_required)

    def test_product_model_numbers_are_not_treated_as_malformed_budgets(self):
        intent = parse_search_intent("Find 5G smartphones")

        self.assertEqual(intent.normalized_search_query, "5g smartphones")
        self.assertIsNone(intent.minimum_budget_amount)
        self.assertIsNone(intent.budget_amount)
        self.assertFalse(intent.clarification_required)

    def test_unsupported_preferences_are_reported_and_removed_from_search(self):
        intent = parse_search_intent(
            "Find a lightweight laptop with high performance under 50000 INR"
        )
        self.assertEqual(intent.normalized_search_query, "laptop")
        self.assertEqual(intent.preferences, ("performance", "portability"))
        self.assertEqual(intent.unsupported_requirements, intent.preferences)
        self.assertFalse(intent.clarification_required)

    def test_model_names_are_preserved_while_budget_is_removed(self):
        intent = parse_search_intent("Find Galaxy S24 Ultra under ₹60,000")
        self.assertEqual(intent.normalized_search_query, "galaxy s24 ultra")
        self.assertIsNone(intent.product_category)
        self.assertEqual(intent.budget_amount, Decimal("60000"))
        self.assertEqual(intent.budget_currency, "INR")

    def test_generic_headphones_are_not_assumed_to_be_wireless(self):
        intent = parse_search_intent("Find headphones")
        self.assertEqual(intent.product_category, "Headphones")

    def test_non_string_queries_are_rejected(self):
        with self.assertRaises(TypeError):
            parse_search_intent(None)


if __name__ == "__main__":
    unittest.main()
