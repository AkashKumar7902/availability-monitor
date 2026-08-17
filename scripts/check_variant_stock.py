#!/usr/bin/env python3
"""Check one privately configured size or variant through a JSON storefront API."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)


@dataclass(frozen=True)
class VariantResult:
    status: Literal["available", "unavailable", "unknown"]
    inventory: int | None = None
    reason: str | None = None


class CheckUnknown(RuntimeError):
    """Carry a fixed, non-sensitive reason code for an inconclusive check."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class NoRedirectHandler(HTTPRedirectHandler):
    """Keep private headers and cookies on their explicitly configured hosts."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


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
        return VariantResult("unknown", reason="payload")


def _retry_delay(error: Exception, attempt: int) -> float:
    if isinstance(error, HTTPError):
        retry_after = error.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 30.0)
    return min(float(2**attempt), 4.0)


def _user_agent() -> str:
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    )


def _merge_headers(
    defaults: dict[str, str],
    private: dict[str, str],
) -> dict[str, str]:
    """Merge headers case-insensitively, with private values taking precedence."""
    merged = {
        name.lower(): (name, value)
        for name, value in defaults.items()
    }
    for name, value in private.items():
        merged[name.lower()] = (name, value)
    return {name: value for name, value in merged.values()}


def _curl_header_args(headers: dict[str, str]) -> list[str]:
    return [
        part
        for name, value in headers.items()
        for part in ("--header", f"{name}: {value}")
    ]


def _fetch_payload_with_urllib(
    bootstrap_url: str,
    api_url: str,
    *,
    bootstrap_headers: dict[str, str],
    api_headers: dict[str, str],
    timeout: float,
    retries: int,
) -> Any:
    """Retrieve the payload with the Python standard-library transport."""
    user_agent = _user_agent()
    last_error: Exception | None = None
    last_reason = "transport"

    for attempt in range(retries + 1):
        cookie_jar = CookieJar()
        opener = build_opener(
            HTTPCookieProcessor(cookie_jar),
            NoRedirectHandler(),
        )
        phase = "bootstrap"
        try:
            bootstrap = Request(
                bootstrap_url,
                headers=_merge_headers(
                    {
                        "Accept": "text/html,application/xhtml+xml",
                        "Cache-Control": "no-cache",
                        "User-Agent": user_agent,
                    },
                    bootstrap_headers,
                ),
                method="GET",
            )
            with opener.open(bootstrap, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise CheckUnknown("bootstrap-status")
                response.read(1)
            has_private_cookie = any(
                name.lower() == "cookie" for name in bootstrap_headers
            )
            if not list(cookie_jar) and not has_private_cookie:
                raise CheckUnknown("bootstrap-session")

            phase = "api"
            request = Request(
                api_url,
                headers=_merge_headers(
                    {
                        "Accept": "application/json",
                        "Cache-Control": "no-cache",
                        "Referer": bootstrap_url,
                        "User-Agent": user_agent,
                    },
                    api_headers,
                ),
                method="GET",
            )
            with opener.open(request, timeout=timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise CheckUnknown("api-status")
                body = response.read(5_000_001)
                if len(body) > 5_000_000:
                    raise CheckUnknown("api-size")
                return json.loads(body.decode("utf-8"))
        except (
            CheckUnknown,
            HTTPError,
            URLError,
            TimeoutError,
            RuntimeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
            if isinstance(error, CheckUnknown):
                last_reason = error.reason
            elif isinstance(error, HTTPError):
                last_reason = f"{phase}-http-{error.code}"
            elif isinstance(error, (UnicodeDecodeError, json.JSONDecodeError)):
                last_reason = f"{phase}-format"
            else:
                last_reason = f"{phase}-transport"
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

    raise CheckUnknown(last_reason) from last_error


def _fetch_payload_with_curl(
    bootstrap_url: str,
    api_url: str,
    *,
    bootstrap_headers: dict[str, str],
    api_headers: dict[str, str],
    timeout: float,
    retries: int,
) -> Any:
    """Retry through curl when a storefront rejects the default TLS client."""
    curl = shutil.which("curl")
    if not curl:
        raise CheckUnknown("curl-missing")

    common = [
        curl,
        "--http1.1",
        "--fail",
        "--silent",
        "--show-error",
        "--compressed",
        "--retry",
        str(retries),
        "--retry-all-errors",
        "--connect-timeout",
        str(min(timeout, 10.0)),
        "--max-time",
        str(timeout),
    ]
    process_timeout = max((retries + 1) * (timeout + 5.0), 15.0)
    bootstrap_header_args = _curl_header_args(
        _merge_headers(
            {
                "Accept": "text/html,application/xhtml+xml",
                "Cache-Control": "no-cache",
                "User-Agent": _user_agent(),
            },
            bootstrap_headers,
        )
    )
    api_header_args = _curl_header_args(
        _merge_headers(
            {
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Referer": bootstrap_url,
                "User-Agent": _user_agent(),
            },
            api_headers,
        )
    )

    with tempfile.TemporaryDirectory(prefix="private-monitor-") as directory:
        cookie_path = str(Path(directory, "cookies.txt"))
        cookie_args = (
            []
            if any(name.lower() == "cookie" for name in api_headers)
            else ["--cookie", cookie_path]
        )
        try:
            bootstrap = subprocess.run(
                [
                    *common,
                    *bootstrap_header_args,
                    "--cookie-jar",
                    cookie_path,
                    "--output",
                    os.devnull,
                    bootstrap_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=process_timeout,
            )
            if bootstrap.returncode != 0:
                raise CheckUnknown("curl-bootstrap")

            response = subprocess.run(
                [
                    *common,
                    *api_header_args,
                    *cookie_args,
                    api_url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=process_timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise CheckUnknown("curl-timeout") from error

    if response.returncode != 0:
        raise CheckUnknown("curl-api")
    if len(response.stdout) > 5_000_000:
        raise CheckUnknown("curl-size")
    try:
        return json.loads(response.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckUnknown("curl-format") from error


def fetch_payload(
    bootstrap_url: str,
    api_url: str,
    *,
    bootstrap_headers: dict[str, str],
    api_headers: dict[str, str],
    timeout: float,
    retries: int,
) -> Any:
    """Create an anonymous session, then retrieve the configured JSON payload."""
    try:
        return _fetch_payload_with_urllib(
            bootstrap_url,
            api_url,
            bootstrap_headers=bootstrap_headers,
            api_headers=api_headers,
            timeout=timeout,
            retries=retries,
        )
    except CheckUnknown as error:
        if error.reason == "api-size":
            raise
        return _fetch_payload_with_curl(
            bootstrap_url,
            api_url,
            bootstrap_headers=bootstrap_headers,
            api_headers=api_headers,
            timeout=timeout,
            retries=retries,
        )


def append_github_output(path: str | None, result: VariantResult) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"status={result.status}\n")


def required_private_configuration(
    parser: argparse.ArgumentParser,
) -> tuple[str, str, int, str, dict[str, str], dict[str, str]]:
    bootstrap_url = os.environ.get("MONITOR_TARGET_2_BOOTSTRAP_URL", "").strip()
    api_url = os.environ.get("MONITOR_TARGET_2_API_URL", "").strip()
    expected_id_raw = os.environ.get("MONITOR_TARGET_2_ID", "").strip()
    desired_variant = os.environ.get("MONITOR_TARGET_2_VARIANT", "").strip()
    bootstrap_headers_raw = os.environ.get(
        "MONITOR_TARGET_2_BOOTSTRAP_HEADERS", ""
    ).strip()
    api_headers_raw = os.environ.get(
        "MONITOR_TARGET_2_API_HEADERS", ""
    ).strip()

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

    def parse_headers(raw: str, variable: str) -> dict[str, str]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            parser.error(f"{variable} must be a JSON object")
        if not isinstance(value, dict) or len(value) > 12:
            parser.error(f"{variable} must be a JSON object")
        header_name = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
        forbidden = {"content-length", "host", "set-cookie"}
        headers: dict[str, str] = {}
        for name, header_value in value.items():
            valid_value = (
                isinstance(header_value, str)
                and bool(header_value)
                and len(header_value) <= 8_192
                and all(
                    32 <= ord(character) <= 126
                    for character in header_value
                )
            )
            if (
                not isinstance(name, str)
                or not header_name.fullmatch(name)
                or name.lower() in forbidden
                or not valid_value
            ):
                parser.error(f"{variable} is invalid")
            headers[name] = header_value
        return headers

    bootstrap_headers = parse_headers(
        bootstrap_headers_raw,
        "MONITOR_TARGET_2_BOOTSTRAP_HEADERS",
    )
    api_headers = parse_headers(
        api_headers_raw,
        "MONITOR_TARGET_2_API_HEADERS",
    )

    return (
        bootstrap_url,
        api_url,
        expected_id,
        desired_variant,
        bootstrap_headers,
        api_headers,
    )


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
    (
        bootstrap_url,
        api_url,
        expected_id,
        desired_variant,
        bootstrap_headers,
        api_headers,
    ) = required_private_configuration(parser)
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        payload = fetch_payload(
            bootstrap_url,
            api_url,
            bootstrap_headers=bootstrap_headers,
            api_headers=api_headers,
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
                bootstrap_headers=bootstrap_headers,
                api_headers=api_headers,
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
                else VariantResult("unknown", reason="confirmation")
            )
    except CheckUnknown as error:
        result = VariantResult("unknown", reason=error.reason)
    except Exception:
        result = VariantResult("unknown", reason="internal")

    append_github_output(args.github_output, result)
    public_result = {"checked_at": checked_at, "status": result.status}
    if result.reason:
        public_result["reason"] = result.reason
    print(json.dumps(public_result, indent=2))
    return 0 if result.status in {"available", "unavailable"} else 2


if __name__ == "__main__":
    sys.exit(main())
