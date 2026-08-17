from __future__ import annotations

import io
import json
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
            "MONITOR_TARGET_2_BOOTSTRAP_HEADERS": (
                '{"X-Private-Test":"PRIVATE-HEADER-SENTINEL"}'
            ),
            "MONITOR_TARGET_2_API_HEADERS": (
                '{"X-Private-API":"PRIVATE-API-SENTINEL"}'
            ),
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
            private_values["MONITOR_TARGET_2_BOOTSTRAP_HEADERS"],
            private_values["MONITOR_TARGET_2_API_HEADERS"],
        ):
            self.assertNotIn(secret, public_text)
        private_headers = json.loads(
            private_values["MONITOR_TARGET_2_BOOTSTRAP_HEADERS"]
        )
        private_api_headers = json.loads(
            private_values["MONITOR_TARGET_2_API_HEADERS"]
        )
        for headers in (private_headers, private_api_headers):
            for name, value in headers.items():
                self.assertNotIn(name, public_text)
                self.assertNotIn(value, public_text)
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

    def test_fetch_falls_back_to_curl_after_transport_rejection(self) -> None:
        expected = payload()
        with (
            mock.patch.object(
                check_variant_stock,
                "_fetch_payload_with_urllib",
                side_effect=check_variant_stock.CheckUnknown(
                    "bootstrap-transport"
                ),
            ) as urllib_fetch,
            mock.patch.object(
                check_variant_stock,
                "_fetch_payload_with_curl",
                return_value=expected,
            ) as curl_fetch,
        ):
            actual = check_variant_stock.fetch_payload(
                "https://private.invalid/start",
                "https://private.invalid/target",
                bootstrap_headers={"X-Private-Test": "secret"},
                api_headers={"X-Private-API": "secret"},
                timeout=5,
                retries=1,
            )

        self.assertEqual(actual, expected)
        urllib_fetch.assert_called_once()
        curl_fetch.assert_called_once()

    def test_private_cookie_can_supply_a_preestablished_session(self) -> None:
        expected = payload()

        class FakeResponse:
            status = 200

            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return self.body if size < 0 else self.body[:size]

        opener = mock.Mock()
        opener.open.side_effect = [
            FakeResponse(b"{}"),
            FakeResponse(json.dumps(expected).encode()),
        ]
        with mock.patch.object(
            check_variant_stock,
            "build_opener",
            return_value=opener,
        ):
            actual = check_variant_stock._fetch_payload_with_urllib(
                "https://private.invalid/start",
                "https://private.invalid/target",
                bootstrap_headers={"Cookie": "PRIVATE-BOOTSTRAP-SENTINEL"},
                api_headers={"Cookie": "PRIVATE-API-SENTINEL"},
                timeout=5,
                retries=0,
            )

        self.assertEqual(actual, expected)
        bootstrap_request = opener.open.call_args_list[0].args[0]
        api_request = opener.open.call_args_list[1].args[0]
        self.assertEqual(
            bootstrap_request.get_header("Cookie"),
            "PRIVATE-BOOTSTRAP-SENTINEL",
        )
        self.assertEqual(
            api_request.get_header("Cookie"),
            "PRIVATE-API-SENTINEL",
        )

    def test_curl_keeps_bootstrap_and_api_headers_separate(self) -> None:
        expected = payload()
        bootstrap_process = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        api_process = mock.Mock(
            returncode=0,
            stdout=json.dumps(expected).encode(),
            stderr=b"",
        )
        with (
            mock.patch.object(
                check_variant_stock.shutil,
                "which",
                return_value="/usr/bin/curl",
            ),
            mock.patch.object(
                check_variant_stock.subprocess,
                "run",
                side_effect=[bootstrap_process, api_process],
            ) as run,
        ):
            actual = check_variant_stock._fetch_payload_with_curl(
                "https://private.invalid/start",
                "https://private.invalid/target",
                bootstrap_headers={
                    "Cookie": "PRIVATE-BOOTSTRAP-SENTINEL",
                    "accept": "application/private-bootstrap",
                },
                api_headers={
                    "Cookie": "PRIVATE-API-SENTINEL",
                    "referer": "https://private.invalid/private-referrer",
                },
                timeout=5,
                retries=0,
            )

        self.assertEqual(actual, expected)
        bootstrap_command = run.call_args_list[0].args[0]
        api_command = run.call_args_list[1].args[0]
        self.assertIn("Cookie: PRIVATE-BOOTSTRAP-SENTINEL", bootstrap_command)
        self.assertNotIn("Cookie: PRIVATE-API-SENTINEL", bootstrap_command)
        self.assertIn("Cookie: PRIVATE-API-SENTINEL", api_command)
        self.assertNotIn("Cookie: PRIVATE-BOOTSTRAP-SENTINEL", api_command)
        self.assertNotIn("--cookie", api_command)
        bootstrap_accept = [
            value
            for value in bootstrap_command
            if value.lower().startswith("accept:")
        ]
        api_referer = [
            value
            for value in api_command
            if value.lower().startswith("referer:")
        ]
        self.assertEqual(
            bootstrap_accept,
            ["accept: application/private-bootstrap"],
        )
        self.assertEqual(
            api_referer,
            ["referer: https://private.invalid/private-referrer"],
        )

    def test_header_merge_is_case_insensitive(self) -> None:
        merged = check_variant_stock._merge_headers(
            {"Accept": "default", "Referer": "default"},
            {"accept": "private", "referer": "private"},
        )

        self.assertEqual(
            {name.lower(): value for name, value in merged.items()},
            {"accept": "private", "referer": "private"},
        )

    def test_private_headers_are_never_forwarded_through_redirects(self) -> None:
        handler = check_variant_stock.NoRedirectHandler()
        redirected = handler.redirect_request(
            None,
            None,
            302,
            "redirect",
            {},
            "https://redirect.invalid/",
        )

        self.assertIsNone(redirected)


if __name__ == "__main__":
    unittest.main()
