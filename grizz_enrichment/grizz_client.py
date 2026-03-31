"""Grizz Enrichment API client.

Handles the full submit → poll → fetch cycle.
"""

import time

import requests

BASE_URL = "https://getgrizz.com"
POLL_INTERVAL = 5   # seconds between status checks
MAX_POLLS = 24      # give up after 2 minutes


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def submit(api_key: str, domain: str) -> dict:
    """Submit an enrichment request. Returns the request object (includes id and status)."""
    url = f"{BASE_URL}/api/v1/enrichment/"
    resp = requests.post(url, json={"domain": domain}, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def poll_status(api_key: str, request_id: str) -> dict:
    """Check the status of an in-flight enrichment request."""
    url = f"{BASE_URL}/api/v1/enrichment/{request_id}/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_results(api_key: str, request_id: str) -> dict:
    """Fetch enriched data for a completed request. Marks the result as retrieved."""
    url = f"{BASE_URL}/api/v1/enrichment/{request_id}/results/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def enrich(api_key: str, domain: str, on_status=None) -> dict | None:
    """Run the full submit → poll → fetch cycle for a single domain.

    Args:
        api_key:   Grizz API key.
        domain:    Domain to enrich (e.g. "acme.com").
        on_status: Optional callable(status: str) invoked on each poll tick.

    Returns:
        Results dict on success, None if Grizz returns no_match.

    Raises:
        RuntimeError:  If the request fails.
        TimeoutError:  If polling exceeds MAX_POLLS.
        requests.HTTPError: On API errors.
    """
    data = submit(api_key, domain)
    request_id = data["id"]
    status = data["status"]

    polls = 0
    while status not in ("COMPLETE", "FAILED") and polls < MAX_POLLS:
        time.sleep(POLL_INTERVAL)
        polls += 1
        data = poll_status(api_key, request_id)
        status = data["status"]
        if on_status:
            on_status(status)

    if status == "FAILED":
        raise RuntimeError(f"Enrichment failed: {data.get('error_detail', 'unknown error')}")

    if status != "COMPLETE":
        raise TimeoutError(
            f"Timed out after {polls} polls (last status: {status}). "
            f"Check manually: GET {BASE_URL}/api/v1/enrichment/{request_id}/"
        )

    results = fetch_results(api_key, request_id)

    if results.get("source") == "no_match":
        return None

    # Results are wrapped: {"source": "...", "company": {...}}
    company = results["company"]

    # The "database" source uses different field names than "enrichment".
    # Normalize to enrichment-source names so the mapper always sees consistent keys.
    if results.get("source") == "database" and company:
        company = _normalize_database_fields(company)

    return company


# Field name mapping: database source → enrichment source
_DB_FIELD_MAP = {
    "hq_city":    "city",
    "hq_state":   "state_province_region",
    "hq_country": "country",
    "naics6":     "naics_code",
}


def _normalize_database_fields(company: dict) -> dict:
    """Rename database-source field names to match enrichment-source names."""
    normalized = {}
    for key, value in company.items():
        normalized[_DB_FIELD_MAP.get(key, key)] = value
    return normalized
