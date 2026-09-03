#!/usr/bin/env python3
"""Export LHV accounts and statements to one JSON document.

Uses only the Python standard library. Credentials are read from the environment
(or from .env): ACCESS_TOKEN and, optionally, REFRESH_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_URL = "https://api.lhv.ai/api/v1"
TOKEN_URL = "https://auth.lhv.ai/oauth2/token"
CLIENT_ID = "api-access"


class ApiError(RuntimeError):
    """An HTTP or API-level error."""


class LhvClient:
    def __init__(
        self,
        access_token: str | None,
        refresh_token: str | None,
        api_url: str = API_URL,
        token_url: str = TOKEN_URL,
        timeout: float = 30,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.api_url = api_url.rstrip("/")
        self.token_url = token_url
        self.timeout = timeout
        self.ssl_context = ssl_context or ssl.create_default_context()

    def refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise ApiError("No ACCESS_TOKEN or usable REFRESH_TOKEN was provided")

        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": CLIENT_ID,
            }
        ).encode()
        request = Request(
            self.token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        payload = self._send(request)
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise ApiError("Token endpoint did not return an access_token")
        self.access_token = str(payload["access_token"])
        if payload.get("refresh_token"):
            self.refresh_token = str(payload["refresh_token"])

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        if not self.access_token:
            self.refresh_access_token()

        url = f"{self.api_url}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode(params)

        for attempt in range(2):
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.access_token}",
                },
            )
            try:
                return self._send(request)
            except ApiError as error:
                if attempt == 0 and error.args and error.args[0].startswith("HTTP 401") and self.refresh_token:
                    self.refresh_access_token()
                    continue
                raise
        raise AssertionError("unreachable")

    def _send(self, request: Request) -> Any:
        try:
            with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("detail") or parsed.get("message") or detail
            except (json.JSONDecodeError, AttributeError):
                pass
            raise ApiError(f"HTTP {error.code} from {request.full_url}: {detail}") from error
        except URLError as error:
            hint = ""
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                hint = " (update the CA certificates in your Python environment or use --ca-bundle)"
            raise ApiError(f"Could not reach {request.full_url}: {error.reason}{hint}") from error

        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ApiError(f"Invalid JSON returned by {request.full_url}") from error

    def accounts(self) -> list[dict[str, Any]]:
        result = self.get("accounts")
        if not isinstance(result, list):
            raise ApiError("The accounts endpoint did not return a JSON array")
        return result

    def statement(self, iban: str, date_from: date, date_to: date, limit: int) -> dict[str, Any]:
        result = self.get(
            f"accounts/{quote(iban, safe='')}/statement",
            {
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "limit": str(limit),
                "includeReservations": "true",
                "includeBalances": "true",
            },
        )
        if not isinstance(result, dict):
            raise ApiError(f"Statement endpoint for {iban} did not return a JSON object")
        return result


def load_dotenv(path: Path) -> None:
    """Load basic KEY=VALUE entries without replacing existing environment values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def create_ssl_context(ca_bundle: Path | None, insecure: bool = False) -> ssl.SSLContext:
    """Create a TLS context, preferring certifi when it is installed."""
    if insecure:
        return ssl._create_unverified_context()
    if ca_bundle is not None:
        return ssl.create_default_context(cafile=str(ca_bundle))

    # Conda Python installations sometimes do not connect their bundled CA file
    # to OpenSSL correctly. Certifi is normally present in those environments.
    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date (YYYY-MM-DD)") from error


def complete_statement_chunks(
    client: LhvClient, iban: str, start: date, end: date, limit: int
) -> list[dict[str, Any]]:
    """Split a truncated range until every returned statement has hasMore=false."""
    statement = client.statement(iban, start, end, limit)
    if not statement.get("hasMore", False):
        return [{"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "data": statement}]

    if start == end:
        raise ApiError(
            f"Statement for {iban} on {start.isoformat()} still has hasMore=true at limit={limit}; "
            "the documented API has no cursor/offset parameter. Increase --limit."
        )

    midpoint = start + timedelta(days=(end - start).days // 2)
    return complete_statement_chunks(client, iban, start, midpoint, limit) + complete_statement_chunks(
        client, iban, midpoint + timedelta(days=1), end, limit
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export all LHV accounts and statement data as JSON")
    parser.add_argument("--date-from", required=True, type=parse_date, help="first statement date (YYYY-MM-DD)")
    parser.add_argument("--date-to", required=True, type=parse_date, help="last statement date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=50, help="rows requested per call (default: 50)")
    parser.add_argument("--output", default="lhv-export.json", help="output file, or - for stdout")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="credentials file (default: .env)")
    parser.add_argument("--api-url", default=API_URL, help=argparse.SUPPRESS)
    parser.add_argument("--token-url", default=TOKEN_URL, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds (default: 30)")
    tls_group = parser.add_mutually_exclusive_group()
    tls_group.add_argument("--ca-bundle", type=Path, help="PEM CA certificate bundle for HTTPS verification")
    tls_group.add_argument(
        "--insecure",
        action="store_true",
        help="disable HTTPS certificate verification (unsafe; testing only)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.date_from > args.date_to:
        raise ApiError("--date-from must not be later than --date-to")
    if args.limit < 1:
        raise ApiError("--limit must be positive")

    load_dotenv(args.env_file)
    if args.insecure:
        print(
            "WARNING: TLS certificate verification is disabled; credentials and financial data are not protected "
            "against man-in-the-middle attacks.",
            file=sys.stderr,
        )
    client = LhvClient(
        os.getenv("ACCESS_TOKEN"),
        os.getenv("REFRESH_TOKEN"),
        api_url=args.api_url,
        token_url=args.token_url,
        timeout=args.timeout,
        ssl_context=create_ssl_context(args.ca_bundle, args.insecure),
    )

    accounts = client.accounts()
    statements: dict[str, list[dict[str, Any]]] = {}
    for account in accounts:
        iban = account.get("iban")
        if not isinstance(iban, str) or not iban:
            raise ApiError("An account did not contain a valid iban")
        print(f"Fetching statement for {iban}...", file=sys.stderr)
        statements[iban] = complete_statement_chunks(client, iban, args.date_from, args.date_to, args.limit)

    export = {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "dateFrom": args.date_from.isoformat(),
        "dateTo": args.date_to.isoformat(),
        "accounts": accounts,
        "statements": statements,
    }
    rendered = json.dumps(export, ensure_ascii=False, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(rendered)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
