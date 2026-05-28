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


# Canonical cascade-input keys (see the Grizz API docs §10).  submit() and
# submit_bulk() only forward keys present in this set, so callers can pass
# entire CRM-derived dicts without scrubbing extras.
_CASCADE_INPUT_KEYS = frozenset({
    "gid_company", "grizz_id", "domain",
    "company_name", "hq_city", "hq_state", "hq_country", "hq_phone",
})


def _cascade_payload(company: dict) -> dict:
    """Pull only the cascade-input keys out of `company`, dropping empties."""
    return {k: company[k] for k in _CASCADE_INPUT_KEYS if company.get(k)}


def submit(api_key: str, domain: str | None = None, **kwargs) -> dict:
    """Submit a single enrichment request.

    Pass any of the cascade-input fields (see the Grizz API docs §10):
        gid_company, grizz_id, domain, company_name,
        hq_city, hq_state, hq_country, hq_phone

    `domain` may still be passed positionally for backward compatibility.
    """
    payload = _cascade_payload({**kwargs, **({"domain": domain} if domain else {})})
    if not payload:
        raise ValueError("submit() requires at least one cascade-input field.")
    url = f"{BASE_URL}/api/v1/companies/enrich/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def submit_bulk(api_key: str, companies: list[dict]) -> dict:
    """Submit up to 200 companies in one POST.

    Each entry may carry any of the cascade-input keys
    (gid_company, grizz_id, domain, company_name, hq_city, hq_state,
    hq_country, hq_phone).  Server enforces the soft-cap to the org's
    remaining monthly budget — see the Grizz API docs §10 + the
    /api/v1/companies/enrich-bulk/ endpoint docs.  Returns the bulk
    response body verbatim:

        {
          "submitted":     N,
          "rejected":      [...],
          "results":       [EnrichmentRequest...],
          "budget_remaining_at_start": int | null,
          "budget_consumed":            int,
          "budget_remaining":           int | null,
          "budget_capped":              bool,
          "budget_resets_at":           iso8601
        }
    """
    payload = {"companies": [_cascade_payload(c) for c in companies]}
    url = f"{BASE_URL}/api/v1/companies/enrich-bulk/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=60)
    # 207 (multi-status) and 429 (budget-capped) are both valid responses
    # carrying full body content — don't raise on them.
    if resp.status_code not in (200, 201, 207, 429):
        resp.raise_for_status()
    return resp.json()


def get_budget(api_key: str) -> dict:
    """Snapshot the org's monthly enrichment-call budget.

        {
          "monthly_limit": int | null,   # null = unlimited
          "used":          int,
          "remaining":     int | null,
          "month":         "YYYY-MM",
          "resets_at":     iso8601
        }
    """
    url = f"{BASE_URL}/api/v1/companies/enrich/budget/"
    resp = requests.get(url, headers=_headers(api_key), timeout=15)
    resp.raise_for_status()
    return resp.json()


def lookup_batch(api_key: str, lookups: list[dict]) -> list[dict]:
    """Batch read-only cascade lookup.

    Takes up to 5000 input dicts (each with any cascade-input fields —
    gid_company, grizz_id, domain, company_name, hq_*) and returns Grizz's
    structured view per input.  No EnrichmentRequest rows are created and
    no credits are charged.

    Used internally by orchestrator endpoints (tech-gap, create-crm) and
    available to MCP tools that need Grizz's view for a list of records.

    Returns a list of:
        {
          "input":     {...},
          "matched":   bool,
          "match_via": "gid_company" | "grizz_id" | "domain" | "fuzzy" | null,
          "company":   {gid_company, domain, company_name, naics,
                         employee_range, ...} | null
        }
    """
    payload = {"lookups": [_cascade_payload(l) for l in lookups]}
    url = f"{BASE_URL}/api/v1/companies/lookup-batch/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=60)
    resp.raise_for_status()
    return resp.json().get("matches", [])


def tech_gap(
    api_key: str,
    crm: str,
    credentials: dict,
    field_mapping: dict | None = None,
    limit: int = 500,
) -> dict:
    """Pair of `get_tech_gap_companies` MCP tool — the combined tech-gap
    list, both halves (in_crm + not_in_crm) in one call.

    Server-side orchestration: pulls customer's CRM accounts, queries
    Grizz universe in ICP scope, composes per-record presence + status.
    Credentials live only in server worker memory for the request.

    Returns the response body verbatim from
    POST /api/v1/companies/tech-gap/:
        {
          "crm":              "salesforce",
          "count":            int,
          "in_crm_count":     int,
          "not_in_crm_count": int,
          "limit":            int,
          "records":          [{crm_id, presence, gid_company, ..., status}]
        }
    """
    payload: dict = {"crm": crm, "credentials": credentials, "limit": limit}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    url = f"{BASE_URL}/api/v1/companies/tech-gap/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=600)
    resp.raise_for_status()
    return resp.json()


def create_in_crm(
    api_key: str,
    crm: str,
    credentials: dict,
    records: list[dict],
    field_mapping: dict | None = None,
    native_field_mapping: dict | None = None,
) -> dict:
    """Pair of `create_crm_companies` MCP tool — universal Step 4 of the
    company-resolution flow.

    Server-side orchestration: for each input record, looks up Grizz's
    view via the cascade, builds a CRM create payload from every Grizz_*
    field, and POSTs to the customer's CRM (Salesforce composite/sobjects
    or HubSpot batch/create).  Returns per-record outcome with new crm_ids.

    Up to 5000 records per call; credentials live only in the Grizz backend worker
    memory.  Returns the response body verbatim from
    POST /api/v1/companies/create-crm/.
    """
    payload: dict = {"crm": crm, "credentials": credentials, "records": records}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    if native_field_mapping is not None:
        payload["native_field_mapping"] = native_field_mapping
    url = f"{BASE_URL}/api/v1/companies/create-crm/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=600)
    resp.raise_for_status()
    return resp.json()


def poll_status(api_key: str, request_id: str) -> dict:
    """Check the status of an in-flight enrichment request."""
    url = f"{BASE_URL}/api/v1/companies/enrich/{request_id}/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_results(api_key: str, request_id: str) -> dict:
    """Fetch enriched data for a completed request. Marks the result as retrieved."""
    url = f"{BASE_URL}/api/v1/companies/enrich/{request_id}/results/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def enrich(api_key: str, domain: str | None = None, on_status=None, **kwargs) -> dict | None:
    """Run the full submit → poll → fetch cycle for a single company.

    Accepts any of the cascade-input keys (see the Grizz API docs §10):
        gid_company, grizz_id, domain, company_name,
        hq_city, hq_state, hq_country, hq_phone

    `domain` may still be passed positionally; pure-domain callers are
    unchanged.

    Returns:
        Results dict on success, None if Grizz returns no_match.

    Raises:
        RuntimeError:  If the request fails.
        TimeoutError:  If polling exceeds MAX_POLLS.
        requests.HTTPError: On API errors.
    """
    data = submit(api_key, domain=domain, **kwargs)
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
            f"Check manually: GET {BASE_URL}/api/v1/companies/enrich/{request_id}/"
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


def enrich_bulk(api_key: str, companies: list[dict]) -> dict:
    """Submit a batch of companies and fetch every completed result.

    Submits via POST /api/v1/companies/enrich-bulk/, polls each PENDING request
    until COMPLETE/FAILED, then fetches results.  Each matched/no_match/
    low_conf/failed entry carries its `input` dict so callers can map
    results back to the input cohort.

        {
          "matched":     [{"input": {...}, "company": {...}, "request_id": "..."}],
          "no_match":    [{"input": {...}, "request_id": "..."}],
          "low_conf":    [{"input": {...}, "request_id": "..."}],
          "failed":      [{"input": {...}, "request_id": "...", "error": "..."}],
          "rejected":    [...],                 # passed through from bulk endpoint
          "budget":      {budget snapshot from bulk endpoint},
        }

    NOTE: companies that fall through to async scrape are polled here.
    For larger batches the wall-clock time scales with the slowest scrape.
    """
    body = submit_bulk(api_key, companies)
    initial_requests = body.get("results") or []

    matched: list[dict]  = []
    no_match: list[dict] = []
    low_conf: list[dict] = []
    failed: list[dict]   = []

    # Align each returned EnrichmentRequest with its original input dict
    # by position — the bulk endpoint preserves order for `results`.
    for raw_input, req in zip(companies, initial_requests):
        request_id = req.get("id")
        st = req.get("status")
        polls = 0
        while st not in ("COMPLETE", "FAILED") and polls < MAX_POLLS:
            time.sleep(POLL_INTERVAL)
            polls += 1
            req = poll_status(api_key, request_id)
            st = req.get("status")

        if st == "FAILED":
            failed.append({"input": raw_input, "request_id": request_id,
                           "error": req.get("error_detail", "")})
            continue
        if st != "COMPLETE":
            failed.append({"input": raw_input, "request_id": request_id,
                           "error": f"timeout after {polls} polls"})
            continue

        if req.get("result_type") == "NOT_FOUND":
            no_match.append({"input": raw_input, "request_id": request_id})
            continue
        if req.get("result_type") == "LOW_CONF":
            low_conf.append({"input": raw_input, "request_id": request_id})
            continue

        results = fetch_results(api_key, request_id)
        if results.get("source") == "no_match":
            no_match.append({"input": raw_input, "request_id": request_id})
            continue
        company = results.get("company") or {}
        if results.get("source") == "database" and company:
            company = _normalize_database_fields(company)
        matched.append({"input": raw_input, "company": company,
                        "request_id": request_id})

    return {
        "matched":  matched,
        "no_match": no_match,
        "low_conf": low_conf,
        "failed":   failed,
        "rejected": body.get("rejected") or [],
        "budget": {
            "budget_remaining_at_start": body.get("budget_remaining_at_start"),
            "budget_consumed":           body.get("budget_consumed"),
            "budget_remaining":          body.get("budget_remaining"),
            "budget_capped":             body.get("budget_capped"),
            "budget_resets_at":          body.get("budget_resets_at"),
        },
    }


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
