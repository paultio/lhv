# LHV API export test

A small, dependency-free Python script that fetches:

- every account and its current balance from `GET /accounts`;
- statements, reservations, and balances from `GET /accounts/{iban}/statement` for every account.

## Setup

Python 3.10 or newer is required. Put credentials in `.env` (already ignored by Git):

```dotenv
ACCESS_TOKEN=<access token>
REFRESH_TOKEN=<optional refresh token>
```

`REFRESH_TOKEN` is optional, but lets the script obtain a token when `ACCESS_TOKEN` is absent and retry once when an access token expires. Existing shell environment variables take precedence over `.env`.

## Run

The statement endpoint requires a date range:

```bash
python3 lhv_export.py --date-from 2026-01-01 --date-to 2026-01-31
```

The result is written to `lhv-export.json`. To write JSON to standard output instead:

```bash
python3 lhv_export.py --date-from 2026-01-01 --date-to 2026-01-31 --output -
```

Run `python3 lhv_export.py --help` for all options.

### Conda SSL certificate errors

The script automatically uses `certifi` when it is installed. If Conda still reports `CERTIFICATE_VERIFY_FAILED`, update its certificate packages and retry:

```bash
conda install -n base -c conda-forge ca-certificates certifi openssl
```

You can also select a PEM certificate bundle explicitly:

```bash
python3 lhv_export.py --date-from 2026-01-01 --date-to 2026-01-31 \
  --ca-bundle "$(python3 -m certifi)"
```

As a temporary diagnostic workaround, certificate verification can be disabled. This is unsafe because the bearer token and financial data could be intercepted; do not use it on an untrusted network:

```bash
python3 lhv_export.py --date-from 2026-01-01 --date-to 2026-01-31 --insecure
```

Both `includeReservations` and `includeBalances` are always enabled. If a response has `hasMore: true`, the script automatically divides the date range and requests both halves. Responses are retained as date-stamped chunks so no API fields are discarded. If one day still has more rows than `--limit`, the script stops with an explicit error because the documented API has no cursor or offset; retry with a larger supported limit.

## Updating an access token manually

```bash
curl -X POST https://auth.lhv.ai/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=refresh_token" \
  -d "refresh_token=<refresh_token>" \
  -d "client_id=api-access"
```
