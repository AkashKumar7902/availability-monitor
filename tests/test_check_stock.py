from __future__ import annotations

import unittest

from scripts.check_stock import classify_payload


TEST_PRODUCT_ID = 42
TEST_SKU = "TEST-SKU"
TEST_SLUG = "example-item"


def product_payload(
    *,
    stock_count: int | str = 0,
    delivery_stock: int | str | None = None,
    include_delivery_stock: bool = False,
    product_id: int = TEST_PRODUCT_ID,
    sku: str = TEST_SKU,
    slug: str = TEST_SLUG,
    product_status: str = "ACTIVE",
    not_for_sale: str | None = "No",
) -> dict:
    product = {
        "id": product_id,
        "sku_code": sku,
        "slug": slug,
        "stock_count": stock_count,
        "status": product_status,
        "not_for_sale": not_for_sale,
        "amount": 350,
    }
    if include_delivery_stock:
        product["delivery_stock"] = delivery_stock
    return {"status": 1, "message": "Success", "product": product}


def classify(payload: dict) -> object:
    return classify_payload(
        payload,
        product_id=TEST_PRODUCT_ID,
        sku=TEST_SKU,
        slug=TEST_SLUG,
    )


class ClassifyStockTests(unittest.TestCase):
    def test_zero_stock_is_unavailable(self) -> None:
        result = classify(product_payload())
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.stock_count, 0)
        self.assertEqual(result.delivery_stock, 0)

    def test_positive_global_stock_is_available(self) -> None:
        result = classify(product_payload(stock_count=12))
        self.assertEqual(result.status, "available")
        self.assertEqual(result.stock_count, 12)

    def test_positive_delivery_stock_is_available(self) -> None:
        result = classify(
            product_payload(
                stock_count=0,
                delivery_stock=3,
                include_delivery_stock=True,
            )
        )
        self.assertEqual(result.status, "available")
        self.assertEqual(result.delivery_stock, 3)

    def test_numeric_strings_are_accepted(self) -> None:
        result = classify(product_payload(stock_count="3"))
        self.assertEqual(result.status, "available")

    def test_not_for_sale_is_unavailable_even_with_stock(self) -> None:
        result = classify(
            product_payload(stock_count=12, not_for_sale=" Yes ")
        )
        self.assertEqual(result.status, "unavailable")

    def test_discontinued_is_unavailable_even_with_stock(self) -> None:
        result = classify(
            product_payload(stock_count=12, not_for_sale="discontinued")
        )
        self.assertEqual(result.status, "unavailable")

    def test_inactive_product_is_unavailable_even_with_stock(self) -> None:
        result = classify(
            product_payload(stock_count=12, product_status="INACTIVE")
        )
        self.assertEqual(result.status, "unavailable")

    def test_unexpected_product_status_is_unknown(self) -> None:
        result = classify(
            product_payload(stock_count=12, product_status="ARCHIVED")
        )
        self.assertEqual(result.status, "unknown")

    def test_identity_mismatch_is_unknown(self) -> None:
        result = classify(product_payload(product_id=99))
        self.assertEqual(result.status, "unknown")

    def test_unsuccessful_api_payload_is_unknown(self) -> None:
        result = classify({"status": 0, "message": "Product not found!"})
        self.assertEqual(result.status, "unknown")

    def test_missing_schema_is_unknown(self) -> None:
        result = classify({"status": 1, "product": {}})
        self.assertEqual(result.status, "unknown")

    def test_negative_stock_is_unknown(self) -> None:
        result = classify(product_payload(stock_count=-1))
        self.assertEqual(result.status, "unknown")


if __name__ == "__main__":
    unittest.main()
