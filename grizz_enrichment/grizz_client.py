"""Grizz Enrichment API client.

Handles the full submit → poll → fetch cycle.
"""

import time

import requests

BASE_URL = "https://getgrizz.com"
POLL_INTERVAL = 5   # seconds between status checks
MAX_POLLS = 24      # give up after 2 minutes


def _raise_with_body(resp: "requests.Response") -> None:
    """Drop-in replacement for ``resp.raise_for_status()`` that folds the
    response body into the raised HTTPError's message, so callers (the MCP,
    customer scripts) see the actual server ``{"detail": "..."}`` instead of
    just '502 Server Error'.
    """
    if resp.status_code < 400:
        return
    try:
        body = (resp.text or "")[:500].strip()
    except Exception:
        body = "<unreadable body>"
    msg = f"{resp.status_code} {resp.reason} for url: {resp.url}"
    if body:
        msg = f"{msg} — {body}"
    raise requests.HTTPError(msg, response=resp)


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
    _raise_with_body(resp)
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
        _raise_with_body(resp)
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
    _raise_with_body(resp)
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
    _raise_with_body(resp)
    return resp.json().get("matches", [])


def setup_instructions(api_key: str, crm: str = "attio") -> dict:
    """Headless (Attio) one-time setup checklist — the grizz_* attribute catalog.

    Read-only.  Rendered server-side from the canonical catalog
    server-side, so consumers stay in lock-step with the API/MCP
    instead of hardcoding the schema.  Returns:
        {"crm", "required_attributes": {<object>: [api_slug, ...]},
         "attributes": {<object>: [{api_slug, title, type, required}, ...]},
         "how_to_provision"}
    """
    url = f"{BASE_URL}/api/v1/admin/crm-setup-instructions/"
    resp = requests.get(url, params={"crm": crm}, headers=_headers(api_key), timeout=30)
    _raise_with_body(resp)
    return resp.json()


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
    _raise_with_body(resp)
    return resp.json()


def get_crm(
    api_key: str,
    crm: str,
    credentials: dict,
    filter: str = "stale",
    field_mapping: dict | None = None,
    limit: int = 500,
    stale_days: int = 60,
) -> dict:
    """Pair of the `get_crm_companies` MCP tool — paginated CRM detail rows
    with per-account Grizz status.

    Wraps POST /api/v1/companies/get-crm/.  Server-side: pulls the
    customer's CRM, filters by Last Sync status (awaiting / stale / all),
    joins Grizz's view via the cascade.  Returns the response body
    verbatim: {crm, filter, count, limit, records}.
    """
    payload: dict = {"crm": crm, "credentials": credentials,
                     "filter": filter, "limit": limit, "stale_days": stale_days}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    url = f"{BASE_URL}/api/v1/companies/get-crm/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=600)
    _raise_with_body(resp)
    return resp.json()


def update_crm(
    api_key: str,
    crm: str,
    credentials: dict,
    records: list[dict],
    field_mapping: dict | None = None,
) -> dict:
    """Pair of the `update_crm_companies` MCP tool — submit a CRM-account
    update job and get back a request_id for polling.

    Wraps POST /api/v1/companies/update-crm/.  The server queues the
    work on Celery (the SF Account-table pagination that runs when
    records lack crm_id alone can exceed the gunicorn worker timeout)
    and returns 202 with `{request_id, status: "PENDING", total}`
    immediately.  Poll `check_crm_write_request(request_id)` for the
    final result.
    """
    payload: dict = {"crm": crm, "credentials": credentials, "records": records}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    url = f"{BASE_URL}/api/v1/companies/update-crm/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=60)
    _raise_with_body(resp)
    return resp.json()


def persona_gap(
    api_key: str,
    crm: str,
    credentials: dict,
    field_mapping: dict | None = None,
    limit: int = 500,
) -> dict:
    """Pair of the `get_persona_gap_contacts` MCP tool — the prospecting
    list of ICP-persona contacts at the customer's CRM accounts that aren't
    in their CRM yet.  Server-side: pulls accounts + contacts, queries
    Grizz Persons matching the org's ICP personas at those accounts,
    excludes any already represented.  Returns the body verbatim from
    POST /api/v1/people/persona-gap/:
        {crm, count, limit, icp_personas, records: [...]}.
    Each record is ready to pass into create_crm_contacts.
    """
    payload: dict = {"crm": crm, "credentials": credentials, "limit": limit}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    url = f"{BASE_URL}/api/v1/people/persona-gap/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=600)
    _raise_with_body(resp)
    return resp.json()


def get_crm_contacts(
    api_key: str,
    crm: str,
    credentials: dict,
    filter: str = "all",
    field_mapping: dict | None = None,
    limit: int = 500,
) -> dict:
    """Pair of the `get_crm_contacts` MCP tool — paginated CRM contact rows
    with the Grizz Contact view joined in.

    Wraps POST /api/v1/people/get-crm/.  Server-side: pulls the customer's
    CRM contacts, filters by `all` or `unmatched` (no Grizz dedup stamp),
    joins Grizz's view via the cascade.  Returns the response body
    verbatim: {crm, filter, count, limit, records}.
    """
    payload: dict = {"crm": crm, "credentials": credentials,
                     "filter": filter, "limit": limit}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    url = f"{BASE_URL}/api/v1/people/get-crm/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=600)
    _raise_with_body(resp)
    return resp.json()


def create_in_crm_contacts(
    api_key: str,
    crm: str,
    credentials: dict,
    records: list[dict],
    field_mapping: dict | None = None,
    native_field_mapping: dict | None = None,
) -> dict:
    """Pair of the `create_crm_contacts` MCP tool — submit a create job and
    get back a request_id for polling.

    Wraps POST /api/v1/people/create-crm/.  The server queues the work on
    Celery (parent linking + creates can run for minutes on 50-100+ contacts)
    and returns 202 with `{request_id, status: "PENDING", total}` immediately.
    Poll `check_crm_write_request(request_id)` for the final result.
    """
    payload: dict = {"crm": crm, "credentials": credentials, "records": records}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    if native_field_mapping is not None:
        payload["native_field_mapping"] = native_field_mapping
    url = f"{BASE_URL}/api/v1/people/create-crm/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=60)
    _raise_with_body(resp)
    return resp.json()


def prepare_contact_writes(
    api_key: str,
    crm: str,
    records: list[dict],
    crm_contacts: list[dict],
    field_mapping: dict | None = None,
) -> dict:
    """Headless, account-scoped contact MATCHING for Attio (POST
    /api/v1/people/prepare-writes/).  Grizz performs NO CRM I/O — the caller
    passes the parent account's existing CRM contacts (`crm_contacts`, read from
    Attio) and Grizz runs the SAME dedup cascade the HubSpot/Salesforce server
    path uses (grizz_person_id → email → linkedin → fuzzy name), returning per
    input record whether it matches an existing contact.

    `records`      — [{gid_person|primary_email|linkedin_url, first_name,
                       last_name, title, gid_company}], ≤200 per call.
    `crm_contacts` — the account's existing people:
                     [{record_id, first_name, last_name, email_addresses,
                       linkedin_url, grizz_person_id}].

    Returns {crm, object, total, prepared, no_grizz_match, matched_existing,
    new_contacts, records:[{matched, crm_match:{record_id, match_via,
    confidence}|null, values, on_create_values, parent}]}.  `records` is 1:1 and
    in order with the input `records`.  match_via "fuzzy" carries a confidence the
    caller should verify before merging (don't silently merge)."""
    payload: dict = {"crm": crm, "records": records, "crm_contacts": crm_contacts}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    url = f"{BASE_URL}/api/v1/people/prepare-writes/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=120)
    _raise_with_body(resp)
    return resp.json()


def check_crm_write_request(api_key: str, request_id: str) -> dict:
    """Pair of the `check_crm_write_request` MCP tool — poll a CRM write job.

    Wraps GET /api/v1/crm-write-requests/<request_id>/.  Returns:
      - {status: "PENDING" | "RUNNING", request_id, kind, crm, total}
        while the job is still working.
      - {status: "COMPLETE", ..., results: [...], created, no_grizz_match,
        no_parent_match, parent_linked, errors} when done.
      - {status: "FAILED", error_message} on failure.
    """
    url = f"{BASE_URL}/api/v1/crm-write-requests/{request_id}/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    _raise_with_body(resp)
    return resp.json()


def enrich_contacts_batch(
    api_key: str,
    gids: list[str],
    include_email: bool = True,
    include_phone: bool = False,
) -> dict:
    """Pair of the `enrich_contacts` MCP tool — submit a bulk enrich job.

    Wraps POST /api/v1/people/enrich-batch/.  The server fans out one
    per-(gid, contact_type) Celery task per requested field and returns
    202 with {request_id, status: "PENDING", total_contacts} immediately.
    Poll `check_person_enrich_batch(request_id)` for the final result.

    Doing this as one submit + one poll (instead of N submits + N polls)
    is what makes bulk enrich practical: 800 companies' worth of contacts
    can't fit inside an MCP tool call's wall-clock budget if every contact
    is polled sequentially from the client side.
    """
    payload = {
        "gids":          gids,
        "include_email": include_email,
        "include_phone": include_phone,
    }
    url = f"{BASE_URL}/api/v1/people/enrich-batch/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=60)
    _raise_with_body(resp)
    return resp.json()


def check_person_enrich_batch(api_key: str, request_id: str) -> dict:
    """Pair of the `check_person_enrich_batch` MCP tool — poll a bulk enrich job.

    Wraps GET /api/v1/people/enrich-batch/<request_id>/.  Returns:
      - While running: {status: "PENDING" | "RUNNING", request_id,
        completed, failed, pending, total_requests, total_contacts}
      - On completion: same plus {found_email, found_phone, results: [
        {gid, email, phone, email_entitled, phone_entitled}, ...
      ]}
      - On failure: {status: "FAILED", error_message}
    """
    url = f"{BASE_URL}/api/v1/people/enrich-batch/{request_id}/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    _raise_with_body(resp)
    return resp.json()


def create_people_audience(
    api_key: str,
    prompt: str | None = None,
    titles: list[str] | None = None,
    domains: list[str] | None = None,
    company_gids: list[str] | None = None,
    source_audience_id: str | None = None,
    named_contacts: list[dict] | None = None,
    max_per_company: int = 3,
) -> dict:
    """Pair of the `build_people_audience` MCP tool — start a people-discovery
    build over a company list and get back an audience id to poll.

    Wraps POST /api/v1/people/audiences/.  Supply the company set exactly one
    way — `company_gids` (preferred: a Grizz-resolved company is the most
    precise key and round-trips cleanly to your rows), `domains`, or an
    existing `source_audience_id`.  Discovery itself is free; the returned
    persons carry no email/phone until you enrich them.

    The server resolves each company, plans personas from `prompt`/`titles`
    (or the org's saved ICP when the prompt is vague), and returns 202 with
    {id, status, prompt, titles} immediately.  The build runs async — poll
    `get_people_audience(id)` until status == "COMPLETE", then page
    `get_people_audience_members(id)`.
    """
    payload: dict = {"max_per_company": max_per_company}
    if prompt:
        payload["prompt"] = prompt
    if titles:
        payload["titles"] = titles
    if domains:
        payload["domains"] = domains
    if company_gids:
        payload["company_gids"] = company_gids
    if source_audience_id:
        payload["source_audience_id"] = source_audience_id
    if named_contacts:
        payload["named_contacts"] = named_contacts
    url = f"{BASE_URL}/api/v1/people/audiences/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=60)
    _raise_with_body(resp)
    return resp.json()


def get_people_audience(api_key: str, audience_id: str) -> dict:
    """Poll a people audience's status/summary.

    Wraps GET /api/v1/people/audiences/<id>/.  Returns
    {id, status, name, prompt, titles, result_count, max_per_company,
     source_audience_id, created_at, completed_at}.  status is one of
    PENDING | RUNNING | COMPLETE | FAILED.
    """
    url = f"{BASE_URL}/api/v1/people/audiences/{audience_id}/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    _raise_with_body(resp)
    return resp.json()


def get_people_audience_members(
    api_key: str,
    audience_id: str,
    page: int = 1,
    per_page: int = 200,
) -> dict:
    """One page of the persons a completed people audience discovered.

    Wraps GET /api/v1/people/audiences/<id>/members/.  Until the build is
    COMPLETE this returns 202 with {detail, status} and no `results` key, so
    callers should poll `get_people_audience` first.  On COMPLETE returns
    {audience_id, total, page, per_page, results}, where each result is
    {gid, first_name, last_name, title, persona, seniority, department,
     company, company_gid, city, state, country, linkedin_url, email, phone,
     email_entitled, phone_entitled, fallback}.  `fallback=True` means no
     persona matched at that company and Grizz cascaded to the most senior
     ICP-department person it could find.
    """
    url = f"{BASE_URL}/api/v1/people/audiences/{audience_id}/members/"
    resp = requests.get(url, params={"page": page, "per_page": per_page},
                        headers=_headers(api_key), timeout=60)
    _raise_with_body(resp)
    return resp.json()


def update_crm_contacts(
    api_key: str,
    crm: str,
    credentials: dict,
    records: list[dict],
    field_mapping: dict | None = None,
) -> dict:
    """Pair of the `update_crm_contacts` MCP tool — rewrite Grizz_Contact_*
    fields on existing CRM contacts.

    Wraps POST /api/v1/people/update-crm/.  Server-side: looks up Grizz's view
    per record, rewrites every Grizz_Contact_* field, leaves native columns
    (FirstName, LastName, Email, Phone, Title, Mailing*) untouched.  Each
    record needs `crm_id`; records without it come back as no_crm_match.
    """
    payload: dict = {"crm": crm, "credentials": credentials, "records": records}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    url = f"{BASE_URL}/api/v1/people/update-crm/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=600)
    _raise_with_body(resp)
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
    _raise_with_body(resp)
    return resp.json()


def poll_status(api_key: str, request_id: str) -> dict:
    """Check the status of an in-flight enrichment request."""
    url = f"{BASE_URL}/api/v1/companies/enrich/{request_id}/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    _raise_with_body(resp)
    return resp.json()


def fetch_results(api_key: str, request_id: str) -> dict:
    """Fetch enriched data for a completed request. Marks the result as retrieved."""
    url = f"{BASE_URL}/api/v1/companies/enrich/{request_id}/results/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    _raise_with_body(resp)
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

    NOTE: companies still being enriched are polled here.
    For larger batches the wall-clock time scales with the slowest enrichment.
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
