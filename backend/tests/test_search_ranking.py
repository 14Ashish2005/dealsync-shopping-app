import unittest
from typing import Literal

from backend.discovery.ranking import rank_offers, score_offer
from backend.product_attributes import ProductAttribute
from backend.providers.base import NormalizedOffer


def make_offer(
    product_id: str,
    product_name: str,
    *,
    provider_key: str = "local_catalog",
    brand: str | None = None,
    model: str | None = None,
    price_minor: int | None = 1000,
    currency: str | None = "INR",
    availability: Literal[
        "in_stock", "out_of_stock", "preorder", "backorder", "unknown"
    ] = "unknown",
    product_attributes: tuple[ProductAttribute, ...] | None = None,
) -> NormalizedOffer:
    return NormalizedOffer(
        offer_id=f"{provider_key}-{product_id}",
        provider_key=provider_key,
        provider_offer_key=f"offer:{product_id}",
        provider_product_id=product_id,
        canonical_product_id=None,
        product_name=product_name,
        brand=brand,
        model=model,
        price_minor=price_minor,
        currency=currency,
        product_url=None,
        image_url=None,
        availability=availability,
        last_checked_at="2026-01-01T00:00:00Z",
        expires_at=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        product_attributes=product_attributes,
    )


class SmartSearchRankingTests(unittest.TestCase):
    def test_exact_product_name_match_ranks_above_weaker_match(self):
        weaker = make_offer(
            "1",
            "Acme Phone Pro",
            brand="Acme",
            model="Phone",
        )
        exact = make_offer("2", "Acme Phone")

        ranked = rank_offers([weaker, exact], query="Acme Phone")

        self.assertEqual(
            [item.offer.provider_product_id for item in ranked],
            ["2", "1"],
        )
        self.assertGreater(ranked[0].score.total, ranked[1].score.total)
        self.assertEqual(ranked[0].score.exact_product_name, 40)

    def test_meaningful_product_name_token_coverage_affects_order(self):
        more_tokens = make_offer("1", "Acme Phone Pro Max")
        fewer_tokens = make_offer("2", "Acme Phone Max")

        ranked = rank_offers(
            [fewer_tokens, more_tokens],
            query="Acme Phone Pro",
        )

        self.assertEqual(
            [item.offer.provider_product_id for item in ranked],
            ["1", "2"],
        )
        self.assertEqual(ranked[0].score.product_name_token_coverage, 40)
        self.assertLess(ranked[1].score.product_name_token_coverage, 40)

    def test_brand_and_model_only_contribute_when_query_mentions_them(self):
        described = make_offer(
            "1",
            "Phone",
            brand="Acme",
            model="Z7",
        )
        undescribed = make_offer("2", "Phone")

        explicit_score = score_offer(described, query="Acme Z7 Phone")
        missing_score = score_offer(undescribed, query="Acme Z7 Phone")
        implicit_score = score_offer(described, query="Phone")
        metadata_only_score = score_offer(undescribed, query="Phone")

        self.assertEqual(explicit_score.explicit_brand_match, 10)
        self.assertEqual(explicit_score.explicit_model_match, 10)
        self.assertGreater(explicit_score.total, missing_score.total)
        self.assertEqual(implicit_score, metadata_only_score)

    def test_generic_and_category_terms_do_not_change_lexical_score(self):
        offer = make_offer("1", "Acme Smart Phone")

        decorated_query = score_offer(
            offer,
            query="Please find Acme smartphones",
            category="Smartphones",
        )
        concise_query = score_offer(
            offer,
            query="Acme",
            category="Smartphones",
        )

        self.assertEqual(decorated_query, concise_query)

    def test_missing_and_title_extracted_attributes_are_neutral(self):
        without_attributes = make_offer("1", "Acme Phone")
        title_extracted = make_offer(
            "2",
            "Acme Phone",
            product_attributes=(
                ProductAttribute(
                    "storage_capacity",
                    128,
                    "local_catalog",
                    extraction_method="product_title",
                    unit="GB",
                ),
            ),
        )

        self.assertEqual(
            score_offer(without_attributes, query="Acme Phone"),
            score_offer(title_extracted, query="Acme Phone"),
        )

    def test_equal_scores_preserve_input_order(self):
        first = make_offer("1", "Acme Phone")
        second = make_offer("2", "Acme Phone")

        ranked = rank_offers([first, second], query="Acme Phone")

        self.assertEqual(
            [item.offer.provider_product_id for item in ranked],
            ["1", "2"],
        )

    def test_price_currency_and_availability_do_not_affect_ranking(self):
        unknown = make_offer(
            "1",
            "Acme Phone",
            price_minor=None,
            currency=None,
            availability="unknown",
        )
        cheaper_other_currency = make_offer(
            "2",
            "Acme Phone",
            price_minor=1,
            currency="USD",
            availability="out_of_stock",
        )

        ranked = rank_offers(
            [unknown, cheaper_other_currency],
            query="Acme Phone",
        )

        self.assertEqual([item.offer for item in ranked], [unknown, cheaper_other_currency])
        self.assertEqual(ranked[0].score, ranked[1].score)

    def test_demo_label_and_provider_identity_are_preserved_without_bonus(self):
        local = make_offer("1", "Acme Phone")
        demo = make_offer(
            "2",
            "[DEMO SAMPLE] Acme Phone",
            provider_key="demo_catalog",
        )

        ranked = rank_offers([local, demo], query="Acme Phone")

        self.assertEqual(ranked[0].score, ranked[1].score)
        self.assertEqual(ranked[0].offer.provider_key, "local_catalog")
        self.assertEqual(ranked[1].offer.provider_key, "demo_catalog")
        self.assertEqual(ranked[1].offer.product_name, "[DEMO SAMPLE] Acme Phone")

    def test_empty_offers_remain_empty(self):
        self.assertEqual(rank_offers((), query="Acme Phone"), ())


if __name__ == "__main__":
    unittest.main()
