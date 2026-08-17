#!/usr/bin/env python3
"""Check one privately configured size or variant through a JSON storefront API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


@dataclass(frozen=True)
class VariantResult:
    status: Literal["available", "unavailable", "unknown"]
    inventory: int | None = None


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    raise ValueError(f"{field} is missing or is not an integer")


def classify_payload(
    payload: Any,
    *,
    expected_id: int,
    desired_variant: str,
) -> VariantResult:
    """Validate identity and classify one exact variant without exposing it."""
    try:
        if not isinstance(payload, dict):
            raise ValueError("API response was not an object")
        style = payload.get("style")
        if not isinstance(style, dict):
            raise ValueError("API style object was missing")

        actual_id = _integer(style.get("id"), "style.id")
        if actual_id != expected_id:
            raise ValueError("Item identity did not match the private configuration")

        flags = style.get("flags")
        if not isinstance(flags, dict):
            raise ValueError("API flags object was missing")
        out_of_stock = flags.get("outOfStock")
        disable_buy = flags.get("disableBuyButton")
        if not isinstance(out_of_stock, bool) or not isinstance(disable_buy, bool):
            raise ValueError("API purchase flags were missing or invalid")

        sizes = style.get("sizes")
        if not isinstance(sizes, list):
            raise ValueError("API variants list was missing")
        matches = [
            size
            for size in sizes
            if isinstance(size, dict) and size.get("label") == desired_variant
        ]
        if len(matches) != 1:
            raise ValueError("Configured variant was missing or duplicated")

        variant = matches[0]
        if "available" not in variant:
            raise ValueError("Variant availability flag was missing")
        available = variant["available"]
        if not isinstance(available, bool):
            raise ValueError("Variant availability flag was invalid")

        if "sizeSellerData" not in variant:
            raise ValueError("Variant seller list was missing")
        sellers = variant["sizeSellerData"]
        if not isinstance(sellers, list):
            raise ValueError("Variant seller list was invalid")
        inventory = 0
        for seller in sellers:
            if not isinstance(seller, dict):
                raise ValueError("Variant seller entry was invalid")
            count = _integer(
                seller.get("sellableInventoryCount"),
                "seller.sellableInventoryCount",
            )
            if count < 0:
                raise ValueError("Variant inventory cannot be negative")
            inventory += count

        if out_of_stock or disable_buy:
            return VariantResult("unavailable", inventory)
        if not available:
            if inventory > 0:
                raise ValueError("Variant availability contradicted inventory")
            return VariantResult("unavailable", 0)
        if inventory <= 0:
            raise ValueError("Available variant had no sellable inventory")
        return VariantResult("available", inventory)
    except ValueError:
        return VariantResult("unknown")


def _retry_delay(error: Exception, attempt: int) -> float:
    if isinstance(error, HTTPError):
        retry_after = error.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 30.0)
    return min(float(2**attempt), 4.0)


def fetch_payload(
    bootstrap_url: str,
    api_url: str,
    *,
    timeout: float,
    retries: int,
) -> Any:
    """Create an anonymous session, then retrieve the configured JSON payload."""
    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    )
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        cookie_jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookie_jar))
        try:
            bootstrap = Request(
                bootstrap_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Cache-Control": "no-cache",
                    "User-Agent": user_agent,
                },
                method="GET",
            )
            with opener.open(bootstrap, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise RuntimeError("Bootstrap returned an unexpected status")
                response.read(1)
            if not list(cookie_jar):
                raise RuntimeError("Bootstrap did not establish a session")

            request = Request(
                api_url,
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                    "Referer": bootstrap_url,
                    "User-Agent": user_agent,
                },
                method="GET",
            )
            with opener.open(request, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise RuntimeError("API returned an unexpected status")
                body = response.read(5_000_001)
                if len(body) > 5_000_000:
                    raise RuntimeError("API response exceeded the size limit")
                return json.loads(body.decode("utf-8"))
        except (
            HTTPError,
            URLError,
            TimeoutError,
            RuntimeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
            retryable = not isinstance(error, HTTPError) or error.code in {
                401,
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

    raise RuntimeError("Could not fetch a valid private-target response") from last_error


def append_github_output(path: str | None, result: VariantResult) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"status={result.status}\n")


def required_private_configuration(
    parser: argparse.ArgumentParser,
) -> tuple[str, str, int, str]:
    bootstrap_url = os.environ.get("MONITOR_TARGET_2_BOOTSTRAP_URL", "").strip()
    api_url = os.environ.get("MONITOR_TARGET_2_API_URL", "").strip()
    expected_id_raw = os.environ.get("MONITOR_TARGET_2_ID", "").strip()
    desired_variant = os.environ.get("MONITOR_TARGET_2_VARIANT", "").strip()

    missing = [
        name
        for name, value in (
            ("MONITOR_TARGET_2_BOOTSTRAP_URL", bootstrap_url),
            ("MONITOR_TARGET_2_API_URL", api_url),
            ("MONITOR_TARGET_2_ID", expected_id_raw),
            ("MONITOR_TARGET_2_VARIANT", desired_variant),
        )
        if not value
    ]
    if missing:
        parser.error("missing private configuration: " + ", ".join(missing))
    try:
        expected_id = int(expected_id_raw)
    except ValueError:
        parser.error("MONITOR_TARGET_2_ID must be an integer")

    return bootstrap_url, api_url, expected_id, desired_variant


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--confirmation-delay",
        type=float,
        default=10.0,
        help="Seconds before confirming a positive availability result.",
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="Append the generic status to this GitHub Actions output file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    bootstrap_url, api_url, expected_id, desired_variant = (
        required_private_configuration(parser)
    )
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        payload = fetch_payload(
            bootstrap_url,
            api_url,
            timeout=args.timeout,
            retries=args.retries,
        )
        result = classify_payload(
            payload,
            expected_id=expected_id,
            desired_variant=desired_variant,
        )
        if result.status == "available":
            time.sleep(max(args.confirmation_delay, 0.0))
            confirmation_payload = fetch_payload(
                bootstrap_url,
                api_url,
                timeout=args.timeout,
                retries=args.retries,
            )
            confirmation = classify_payload(
                confirmation_payload,
                expected_id=expected_id,
                desired_variant=desired_variant,
            )
            result = (
                confirmation
                if confirmation.status == "available"
                else VariantResult("unknown")
            )
    except Exception:
        result = VariantResult("unknown")

    append_github_output(args.github_output, result)
    print(json.dumps({"checked_at": checked_at, "status": result.status}, indent=2))
    return 0 if result.status in {"available", "unavailable"} else 2


if __name__ == "__main__":
    sys.exit(main())
