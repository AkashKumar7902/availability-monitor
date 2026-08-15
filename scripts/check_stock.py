#!/usr/bin/env python3
"""Check one privately configured item's availability through a JSON API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class StockResult:
    status: Literal["available", "unavailable", "unknown"]
    evidence: str
    stock_count: int | None = None
    delivery_stock: int | None = None
    price: int | float | None = None


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    raise ValueError(f"{field} is missing or is not an integer")


def classify_payload(
    payload: Any,
    *,
    product_id: int,
    sku: str,
    slug: str,
) -> StockResult:
    """Validate identity and convert the API response to a three-state result."""
    try:
        if not isinstance(payload, dict) or payload.get("status") != 1:
            raise ValueError("API status was not successful")
        product = payload.get("product")
        if not isinstance(product, dict):
            raise ValueError("API product object was missing")

        actual_id = _integer(product.get("id"), "product.id")
        if actual_id != product_id:
            raise ValueError("Product ID did not match the private configuration")
        if product.get("sku_code") != sku:
            raise ValueError("Product SKU did not match the private configuration")
        if product.get("slug") != slug:
            raise ValueError("Product slug did not match the private configuration")

        stock_count = _integer(product.get("stock_count"), "product.stock_count")
        delivery_raw = product.get("delivery_stock")
        delivery_stock = (
            0
            if delivery_raw is None
            else _integer(delivery_raw, "product.delivery_stock")
        )
        if stock_count < 0 or delivery_stock < 0:
            raise ValueError("Stock values cannot be negative")

        sale_flag = product.get("not_for_sale")
        if sale_flag is None:
            normalised_sale_flag = ""
        elif isinstance(sale_flag, str):
            normalised_sale_flag = sale_flag.strip().casefold()
        else:
            raise ValueError("product.not_for_sale had an unexpected type")
        if normalised_sale_flag not in {"", "no", "yes", "discontinued"}:
            raise ValueError(
                f"product.not_for_sale had an unexpected value: {sale_flag!r}"
            )

        product_status = product.get("status")
        if not isinstance(product_status, str):
            raise ValueError("product.status was missing or not a string")
        normalised_status = product_status.strip().upper()
        if normalised_status not in {"ACTIVE", "INACTIVE"}:
            raise ValueError(
                f"product.status had an unexpected value: {product_status!r}"
            )

        price = product.get("amount")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            price = None

        if normalised_sale_flag in {"yes", "discontinued"}:
            return StockResult(
                "unavailable",
                f"API sale flag blocks purchase ({sale_flag!r}).",
                stock_count,
                delivery_stock,
                price,
            )
        if normalised_status != "ACTIVE":
            return StockResult(
                "unavailable",
                f"Product status is {normalised_status}.",
                stock_count,
                delivery_stock,
                price,
            )
        if stock_count > 0 or delivery_stock > 0:
            return StockResult(
                "available",
                f"API stock_count={stock_count}, delivery_stock={delivery_stock}; "
                "product is active and for sale.",
                stock_count,
                delivery_stock,
                price,
            )
        return StockResult(
            "unavailable",
            "API stock_count and delivery_stock are both 0.",
            stock_count,
            delivery_stock,
            price,
        )
    except ValueError as error:
        return StockResult("unknown", str(error))


def _retry_delay(error: Exception, attempt: int) -> float:
    if isinstance(error, HTTPError):
        retry_after = error.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 30.0)
    return min(float(2**attempt), 4.0)


def fetch_payload(api_url: str, *, timeout: float, retries: int) -> Any:
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": (
            "Mozilla/5.0 (compatible; AvailabilityMonitor/1.0; "
            "+https://github.com/)"
        ),
    }
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        request = Request(api_url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise RuntimeError(f"Unexpected HTTP status {status}")
                return json.loads(response.read().decode("utf-8"))
        except (
            HTTPError,
            URLError,
            TimeoutError,
            RuntimeError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
            retryable = not isinstance(error, HTTPError) or error.code in {
                408,
                425,
                429,
                500,
                502,
                503,
                504,
            }
            if attempt < retries and retryable:
                time.sleep(_retry_delay(error, attempt))
                continue
            break

    raise RuntimeError(f"Could not fetch valid API JSON: {last_error}")


def append_github_output(path: str | None, result: StockResult) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"status={result.status}\n")


def required_private_configuration(
    parser: argparse.ArgumentParser,
) -> tuple[str, int, str, str]:
    """Read target values without providing identifying public defaults."""
    api_url = os.environ.get("MONITOR_API_URL", "").strip()
    product_id_raw = os.environ.get("MONITOR_PRODUCT_ID", "").strip()
    sku = os.environ.get("MONITOR_PRODUCT_SKU", "").strip()
    slug = os.environ.get("MONITOR_PRODUCT_SLUG", "").strip()

    missing = [
        name
        for name, value in (
            ("MONITOR_API_URL", api_url),
            ("MONITOR_PRODUCT_ID", product_id_raw),
            ("MONITOR_PRODUCT_SKU", sku),
            ("MONITOR_PRODUCT_SLUG", slug),
        )
        if not value
    ]
    if missing:
        parser.error("missing private configuration: " + ", ".join(missing))
    try:
        product_id = int(product_id_raw)
    except ValueError:
        parser.error("MONITOR_PRODUCT_ID must be an integer")

    return api_url, product_id, sku, slug


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--confirmation-delay",
        type=float,
        default=10.0,
        help="Seconds before a second check when availability is first detected.",
    )
    parser.add_argument(
        "--json-file",
        help="Classify a saved JSON response instead of making a network request.",
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="Append status outputs to this GitHub Actions output file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    api_url, product_id, sku, slug = required_private_configuration(parser)
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        if args.json_file:
            payload = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        else:
            payload = fetch_payload(
                api_url, timeout=args.timeout, retries=args.retries
            )
        result = classify_payload(
            payload,
            product_id=product_id,
            sku=sku,
            slug=slug,
        )

        # A positive result is rare and consequential. Confirm it with a fresh
        # request before creating an alert; contradictory results fail closed.
        if result.status == "available" and not args.json_file:
            time.sleep(max(args.confirmation_delay, 0.0))
            confirmation_payload = fetch_payload(
                api_url, timeout=args.timeout, retries=args.retries
            )
            confirmation = classify_payload(
                confirmation_payload,
                product_id=product_id,
                sku=sku,
                slug=slug,
            )
            if confirmation.status != "available":
                result = StockResult(
                    "unknown",
                    "Initial availability was not confirmed by the second API check.",
                )
            else:
                result = StockResult(
                    "available",
                    "Availability confirmed twice; "
                    f"stock_count={confirmation.stock_count}, "
                    f"delivery_stock={confirmation.delivery_stock}.",
                    confirmation.stock_count,
                    confirmation.delivery_stock,
                    confirmation.price,
                )
    except Exception as error:  # Inconclusive checks must be visible, not OOS.
        result = StockResult("unknown", str(error))

    append_github_output(args.github_output, result)
    payload = {
        "checked_at": checked_at,
        "status": result.status,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.status in {"available", "unavailable"} else 2


if __name__ == "__main__":
    sys.exit(main())
