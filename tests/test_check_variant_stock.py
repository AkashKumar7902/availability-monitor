from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import check_variant_stock


TEST_ID = 42
TEST_VARIANT = "L"


def payload(
    *,
    item_id: int = TEST_ID,
    available: bool = False,
    inventory: int = 0,
    out_of_stock: bool = False,
    disable_buy: bool = False,
    include_variant: bool = True,
) -> dict:
    sizes = []
    if include_variant:
        sizes.append(
            {
                "label": TEST_VARIANT,
                "available": available,
                "sizeSellerData": (
                    [{"sellableInventoryCount": inventory}]
                    if inventory or available
                    else []
                ),
            }
        )
    return {
        "style": {
            "id": item_id,
            "flags": {
                "outOfStock": out_of_stock,
                "disableBuyButton": disable_buy,
            },
            "sizes": sizes,
        }
    }


def classify(value: dict) -> check_variant_stock.VariantResult:
    return check_variant_stock.classify_payload(
        value,
        expected_id=TEST_ID,
        desired_variant=TEST_VARIANT,
    )


class ClassifyVariantTests(unittest.TestCase):
    def test_available_requires_positive_sellable_inventory(self) -> None:
        result = classify(payload(available=True, inventory=3))
        self.assertEqual(result.status, "available")
        self.assertEqual(result.inventory, 3)

    def test_unavailable_variant_is_unavailable(self) -> None:
        self.assertEqual(classify(payload()).status, "unavailable")

    def test_global_out_of_stock_is_unavailable(self) -> None:
        result = classify(
            payload(available=True, inventory=3, out_of_stock=True)
        )
        self.assertEqual(result.status, "unavailable")

    def test_disabled_buy_button_is_unavailable(self) -> None:
        result = classify(payload(available=True, inventory=3, disable_buy=True))
        self.assertEqual(result.status, "unavailable")

    def test_available_without_inventory_is_unknown(self) -> None:
        self.assertEqual(
            classify(payload(available=True, inventory=0)).status,
            "unknown",
        )

    def test_inventory_contradicting_unavailable_is_unknown(self) -> None:
        self.assertEqual(
            classify(payload(available=False, inventory=2)).status,
            "unknown",
        )

    def test_identity_mismatch_is_unknown(self) -> None:
        self.assertEqual(classify(payload(item_id=99)).status, "unknown")

    def test_missing_variant_is_unknown(self) -> None:
        self.assertEqual(
            classify(payload(include_variant=False)).status,
            "unknown",
        )

    def test_missing_schema_is_unknown(self) -> None:
        self.assertEqual(classify({"style": {}}).status, "unknown")

    def test_missing_available_field_is_unknown(self) -> None:
        value = payload()
        del value["style"]["sizes"][0]["available"]
        self.assertEqual(classify(value).status, "unknown")

    def test_missing_seller_data_is_unknown(self) -> None:
        value = payload()
        del value["style"]["sizes"][0]["sizeSellerData"]
        self.assertEqual(classify(value).status, "unknown")


class VariantMainTests(unittest.TestCase):
    def private_env(self) -> dict[str, str]:
        return {
            "MONITOR_TARGET_2_BOOTSTRAP_URL": "https://private.invalid/start",
            "MONITOR_TARGET_2_API_URL": "https://private.invalid/secret-target",
            "MONITOR_TARGET_2_ID": str(TEST_ID),
            "MONITOR_TARGET_2_VARIANT": TEST_VARIANT,
        }

    def test_positive_result_is_confirmed_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, self.private_env(), clear=True),
                mock.patch.object(
                    check_variant_stock,
                    "fetch_payload",
                    return_value=payload(available=True, inventory=3),
                ) as fetch,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                code = check_variant_stock.main(
                    [
                        "--confirmation-delay",
                        "0",
                        "--github-output",
                        str(output),
                    ]
                )
                output_text = output.read_text()

        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('"status": "available"', stdout.getvalue())
        self.assertEqual(output_text, "status=available\n")

    def test_private_values_and_fetch_errors_are_not_printed(self) -> None:
        private_values = self.private_env()
        private_values["MONITOR_TARGET_2_VARIANT"] = "PRIVATE-VARIANT-SENTINEL"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, private_values, clear=True),
                mock.patch.object(
                    check_variant_stock,
                    "fetch_payload",
                    side_effect=RuntimeError(
                        "response exposed https://private.invalid/secret-target"
                    ),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = check_variant_stock.main(
                    ["--github-output", str(output)]
                )

            public_text = stdout.getvalue() + stderr.getvalue() + output.read_text()

        self.assertEqual(code, 2)
        for secret in (
            private_values["MONITOR_TARGET_2_BOOTSTRAP_URL"],
            private_values["MONITOR_TARGET_2_API_URL"],
            private_values["MONITOR_TARGET_2_VARIANT"],
        ):
            self.assertNotIn(secret, public_text)
        self.assertIn('"status": "unknown"', public_text)
        self.assertIn('"reason": "internal"', public_text)

    def test_safe_fetch_reason_is_reported_without_private_values(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, self.private_env(), clear=True),
                mock.patch.object(
                    check_variant_stock,
                    "fetch_payload",
                    side_effect=check_variant_stock.CheckUnknown("api-http-401"),
                ),
                redirect_stdout(stdout),
            ):
                code = check_variant_stock.main(
                    ["--github-output", str(output)]
                )
                output_text = output.read_text()

        self.assertEqual(code, 2)
        self.assertIn('"reason": "api-http-401"', stdout.getvalue())
        self.assertEqual(output_text, "status=unknown\n")

    def test_unconfirmed_positive_result_becomes_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            with (
                mock.patch.dict(os.environ, self.private_env(), clear=True),
                mock.patch.object(
                    check_variant_stock,
                    "fetch_payload",
                    side_effect=[
                        payload(available=True, inventory=3),
                        payload(available=False, inventory=0),
                    ],
                ),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                code = check_variant_stock.main(
                    [
                        "--confirmation-delay",
                        "0",
                        "--github-output",
                        str(output),
                    ]
                )
                output_text = output.read_text()

        self.assertEqual(code, 2)
        self.assertIn('"status": "unknown"', stdout.getvalue())
        self.assertEqual(output_text, "status=unknown\n")


if __name__ == "__main__":
    unittest.main()
