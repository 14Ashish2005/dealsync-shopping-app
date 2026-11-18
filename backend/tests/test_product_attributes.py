import unittest

from backend.product_attributes import (
    ProductAttribute,
    build_product_attributes,
)


class ProductAttributeTests(unittest.TestCase):
    def test_missing_standard_attributes_are_explicitly_unknown(self):
        attributes = build_product_attributes(
            title="Unspecified product",
            provenance="local_catalog",
        )
        by_name = {attribute.name: attribute for attribute in attributes}

        self.assertEqual(
            set(by_name),
            {
                "brand",
                "model",
                "category",
                "storage_capacity",
                "ram",
                "color",
                "size",
                "weight",
                "pack_quantity",
            },
        )
        self.assertTrue(
            all(
                attribute.value is None and attribute.provenance == "unknown"
                for attribute in by_name.values()
            )
        )

    def test_direct_and_title_extracted_values_keep_distinct_provenance(self):
        attributes = build_product_attributes(
            title=(
                "Brand: Acme, Model: M1 (128 GB, 8 GB RAM), "
                "Color: Black, 14 inches, 1.2 kg, 2-pack"
            ),
            provenance="verified_provider",
            category="Phones",
        )
        by_name = {attribute.name: attribute for attribute in attributes}

        self.assertEqual(by_name["category"].value, "Phones")
        self.assertEqual(by_name["category"].provenance, "verified_provider")
        self.assertEqual(by_name["category"].extraction_method, "direct")
        self.assertEqual(by_name["brand"].value, "Acme")
        self.assertEqual(by_name["brand"].extraction_method, "product_title")
        self.assertEqual(by_name["model"].value, "M1")
        self.assertEqual(by_name["storage_capacity"].value, 128)
        self.assertEqual(by_name["storage_capacity"].unit, "GB")
        self.assertEqual(by_name["ram"].value, 8)
        self.assertEqual(by_name["ram"].unit, "GB")
        self.assertEqual(by_name["color"].value, "Black")
        self.assertEqual(by_name["size"].value, 14)
        self.assertEqual(by_name["size"].unit, "inch")
        self.assertEqual(by_name["weight"].value, 1.2)
        self.assertEqual(by_name["weight"].unit, "kg")
        self.assertEqual(by_name["pack_quantity"].value, 2)
        self.assertTrue(
            all(
                by_name[name].extraction_method == "product_title"
                for name in (
                    "brand",
                    "model",
                    "storage_capacity",
                    "ram",
                    "color",
                    "size",
                    "weight",
                    "pack_quantity",
                )
            )
        )

    def test_ambiguous_multiple_capacities_remain_unknown(self):
        attributes = build_product_attributes(
            title="Phone 8 GB / 128 GB",
            provenance="local_catalog",
        )
        storage = next(
            attribute
            for attribute in attributes
            if attribute.name == "storage_capacity"
        )

        self.assertIsNone(storage.value)
        self.assertEqual(storage.provenance, "unknown")

    def test_network_generation_labels_are_not_extracted_as_weight(self):
        for title in ("5G smartphone", "4G phone"):
            with self.subTest(title=title):
                attributes = build_product_attributes(
                    title=title,
                    provenance="demo_sample",
                )
                weight = next(
                    attribute
                    for attribute in attributes
                    if attribute.name == "weight"
                )

                self.assertIsNone(weight.value)
                self.assertEqual(weight.provenance, "unknown")
                self.assertIsNone(weight.extraction_method)

    def test_explicit_weight_measurements_are_extracted_from_titles(self):
        cases = (
            ("5 g kitchen scale", 5, "g"),
            ("500 g product", 500, "g"),
            ("1 kg product", 1, "kg"),
            ("1.5 kg product", 1.5, "kg"),
        )
        for title, expected_value, expected_unit in cases:
            with self.subTest(title=title):
                attributes = build_product_attributes(
                    title=title,
                    provenance="local_catalog",
                )
                weight = next(
                    attribute
                    for attribute in attributes
                    if attribute.name == "weight"
                )

                self.assertEqual(weight.value, expected_value)
                self.assertEqual(weight.unit, expected_unit)
                self.assertEqual(weight.provenance, "local_catalog")
                self.assertEqual(weight.extraction_method, "product_title")

    def test_demo_sample_and_title_extraction_are_both_preserved(self):
        attributes = build_product_attributes(
            title="[DEMO SAMPLE] Android smartphone (64 GB)",
            provenance="demo_sample",
            category="Smartphones",
        )
        by_name = {attribute.name: attribute for attribute in attributes}

        self.assertEqual(by_name["category"].provenance, "demo_sample")
        self.assertEqual(by_name["storage_capacity"].value, 64)
        self.assertEqual(by_name["storage_capacity"].provenance, "demo_sample")
        self.assertEqual(
            by_name["storage_capacity"].extraction_method,
            "product_title",
        )
        self.assertEqual(by_name["ram"].provenance, "unknown")

    def test_category_specific_attributes_are_supported(self):
        attribute = ProductAttribute(
            name="processor",
            value="Example chipset",
            provenance="verified_provider",
        )
        attributes = build_product_attributes(
            title="Example phone",
            provenance="verified_provider",
            additional_attributes=(attribute,),
        )

        self.assertIn(attribute, attributes)

    def test_unknown_attribute_cannot_carry_a_value_or_provenance(self):
        with self.assertRaises(ValueError):
            ProductAttribute(
                name="storage_capacity",
                value=64,
                provenance="unknown",
            )


if __name__ == "__main__":
    unittest.main()
