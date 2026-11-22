"""Grizz Enrichment API client.

Handles the full submit → poll → fetch cycle.
"""

import os
import random
import time

import requests

from .domain_utils import clean_domain

BASE_URL = "https://getgrizz.com"
POLL_INTERVAL = 5   # seconds between status checks
MAX_POLLS = 24      # give up after 2 minutes

# ── HTTP resilience ─────────────────────────────────────────────────────────
# The enrich submit → poll → fetch cycle runs under concurrency (e.g. `run.py
# enrich --concurrency 12`) and can hit endpoint throttling. Without retries a
# single Read-timeout or 429 fails the whole record — that's what timed out
# 79/100 at concurrency 12 near the tail of a large enrichment day. Retry
# transient failures with JITTERED exponential backoff (the jitter de-syncs the
# concurrent workers so they stop stampeding a throttled endpoint) and honor
# Retry-After. Tunable via env; timeout defaults higher than the old hardcoded 30.
DEFAULT_TIMEOUT = int(os.environ.get("GRIZZ_HTTP_TIMEOUT", "60"))
_MAX_RETRIES = int(os.environ.get("GRIZZ_HTTP_RETRIES", "4"))
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_BACKOFF_CAP = 20   # seconds


def _retry_after_seconds(resp: "requests.Response", default: int) -> int:
    """Seconds to wait from a 429 Retry-After header (integer count OR HTTP-date),
    capped.  Falls back to `default` when the header is absent/unparseable."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return default
    try:
        return max(1, min(int(raw), _BACKOFF_CAP))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return max(1, min(int((dt - datetime.now(timezone.utc)).total_seconds()), _BACKOFF_CAP))
    except Exception:
        return default


def _request(method: str, url: str, *, timeout: int | None = None, **kwargs) -> "requests.Response":
    """requests.request with retry + jittered exponential backoff on TRANSIENT
    failures (ReadTimeout / ConnectionError / 429 / 5xx).  Non-transient 4xx
    responses are returned unretried for the caller to `_raise_with_body` — a bad
    request is never retried.  Timeout defaults to GRIZZ_HTTP_TIMEOUT, retries to
    GRIZZ_HTTP_RETRIES."""
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                time.sleep(min(2 ** attempt, _BACKOFF_CAP) + random.uniform(0, 1))
                continue
            raise
        if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
            base = min(2 ** attempt, _BACKOFF_CAP) + random.uniform(0, 1)
            time.sleep(_retry_after_seconds(resp, int(base)) if resp.status_code == 429 else base)
            continue
        return resp
    raise last_exc  # pragma: no cover


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


# Canonical cascade-input keys (see the Grizz API docs).  submit() and
# submit_bulk() only forward keys present in this set, so callers can pass
# entire CRM-derived dicts without scrubbing extras.
LOOKUP_BATCH_MAX = 5000   # server-side cap on one lookup-batch call

_CASCADE_INPUT_KEYS = frozenset({
    "gid_company", "grizz_id", "domain",
    "company_name", "hq_city", "hq_state", "hq_country", "hq_phone",
})


def _cascade_payload(company: dict) -> dict:
    """Pull only the cascade-input keys out of `company`, dropping empties.

    `domain` is normalized through `clean_domain` because NO server-side match
    endpoint normalizes it (see the Grizz API docs) — a dirty `https://www.x.com/`
    silently misses a company Grizz knows.  The adapters already cleaned domains
    on the way out of a CRM, but CSV- and file-fed callers (`run.py lookup`,
    `_resolve_gids`) never touched an adapter, so they sent raw values straight
    through.  Doing it here covers every cascade path at once.
    """
    payload = {k: company[k] for k in _CASCADE_INPUT_KEYS if company.get(k)}
    if "domain" in payload:
        cleaned = clean_domain(payload["domain"])
        if cleaned:
            payload["domain"] = cleaned
        else:
            del payload["domain"]
    return payload


def submit(api_key: str, domain: str | None = None, **kwargs) -> dict:
    """Submit a single enrichment request.

    Pass any of the cascade-input fields (see the Grizz API docs):
        gid_company, grizz_id, domain, company_name,
        hq_city, hq_state, hq_country, hq_phone

    `domain` may still be passed positionally for backward compatibility.
    """
    payload = _cascade_payload({**kwargs, **({"domain": domain} if domain else {})})
    if not payload:
        raise ValueError("submit() requires at least one cascade-input field.")
    url = f"{BASE_URL}/api/v1/companies/enrich/"
    resp = _request("POST", url, json=payload, headers=_headers(api_key))
    _raise_with_body(resp)
    return resp.json()


def submit_bulk(api_key: str, companies: list[dict]) -> dict:
    """Submit up to 200 companies in one POST.

    Each entry may carry any of the cascade-input keys
    (gid_company, grizz_id, domain, company_name, hq_city, hq_state,
    hq_country, hq_phone).  Server enforces the soft-cap to the org's
    remaining monthly budget — see the Grizz API docs and the
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

    Used by `run.py lookup` / `_resolve_gids`.

    Inputs over the endpoint's 5000 cap are chunked automatically and the
    results concatenated, so callers never chunk themselves.  Transient 429/5xx
    are retried with jittered backoff via `_request`.

    Domains are normalized by `_cascade_payload`, so the `input` echoed back
    carries the CLEANED domain, not the caller's raw value.  Key results by
    `clean_domain(...)` when mapping them onto your own rows.

    Returns a list of:
        {
          "input":     {...},
          "matched":   bool,
          "match_via": "gid_company" | "grizz_id" | "domain" | "fuzzy" | null,
          "company":   {gid_company, domain, company_name, naics,
                         employee_range, ...} | null
        }
    A company outside the caller's plan comes back with `matched: false`,
    `company: null` and `reason: "outside_plan"` (see `is_outside_plan`).
    """
    url = f"{BASE_URL}/api/v1/companies/lookup-batch/"
    matches: list[dict] = []
    for i in range(0, len(lookups), LOOKUP_BATCH_MAX):
        payload = {"lookups": [_cascade_payload(l)
                               for l in lookups[i:i + LOOKUP_BATCH_MAX]]}
        resp = _request("POST", url, json=payload, headers=_headers(api_key))
        _raise_with_body(resp)
        matches.extend(resp.json().get("matches", []))
    return matches


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


def seed_field_mappings(api_key: str, crm: str, object_type: str) -> dict:
    """Seed the org's Grizz field mappings for a CRM object (idempotent: only
    canonical fields the org doesn't have yet are added)."""
    resp = requests.post(f"{BASE_URL}/api/v1/admin/crm-field-mappings/seed/",
                         json={"crm": crm, "object_type": object_type},
                         headers=_headers(api_key), timeout=60)
    _raise_with_body(resp)
    return resp.json()


def mark_field_mappings_synced(api_key: str, crm: str, object_types: list[str]) -> dict:
    """Record that setup created the mapped fields in the CRM, which closes the
    dashboard's setup to-dos."""
    resp = requests.post(f"{BASE_URL}/api/v1/admin/crm-field-mappings/mark-synced/",
                         json={"crm": crm, "object_types": object_types},
                         headers=_headers(api_key), timeout=60)
    _raise_with_body(resp)
    return resp.json()


def prepare_contact_writes(
    api_key: str,
    crm: str,
    records: list[dict],
    crm_contacts: list[dict],
    field_mapping: dict | None = None,
) -> dict:
    """Headless, account-scoped contact write plan (POST
    /api/v1/people/prepare-writes/) for attio, hubspot or salesforce.  Grizz
    performs NO CRM I/O and never receives CRM credentials — the caller passes
    the parent account's existing CRM contacts (`crm_contacts`, read with the
    caller's own credentials) and Grizz runs the dedup cascade (grizz person id →
    email → linkedin → fuzzy name), returning per input record whether it
    matches an existing contact.

    `records`      — [{gid_person|primary_email|linkedin_url, first_name,
                       last_name, title, gid_company}], ≤200 per call.
    `crm_contacts` — the account's existing people:
                     [{record_id, first_name, last_name, email_addresses,
                       linkedin_url, grizz_person_id}].

    Returns {crm, object, total, prepared, no_grizz_match, matched_existing,
    new_contacts, records:[{input_*, matched, crm_match:{record_id, match_via,
    confidence}|null, values, on_create_values, parent}]}.  Pair rows to inputs
    by the echoed `input_*` keys, not by position.  match_via "fuzzy" carries a confidence the
    caller should verify before merging (don't silently merge)."""
    payload: dict = {"crm": crm, "records": records, "crm_contacts": crm_contacts}
    if field_mapping is not None:
        payload["field_mapping"] = field_mapping
    url = f"{BASE_URL}/api/v1/people/prepare-writes/"
    resp = requests.post(url, json=payload, headers=_headers(api_key), timeout=120)
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


def poll_status(api_key: str, request_id: str) -> dict:
    """Check the status of an in-flight enrichment request."""
    url = f"{BASE_URL}/api/v1/companies/enrich/{request_id}/"
    resp = _request("GET", url, headers=_headers(api_key))
    _raise_with_body(resp)
    return resp.json()


def fetch_results(api_key: str, request_id: str) -> dict:
    """Fetch enriched data for a completed request. Marks the result as retrieved."""
    url = f"{BASE_URL}/api/v1/companies/enrich/{request_id}/results/"
    resp = _request("GET", url, headers=_headers(api_key))
    _raise_with_body(resp)
    return resp.json()


OUTSIDE_PLAN = "outside_plan"


def is_outside_plan(payload: dict | None) -> bool:
    """True if a Grizz result says the company is outside the caller's plan.

    Orgs on a plan that covers part of Grizz get, for a company outside it, the
    normal result shape with no company data and `reason: "outside_plan"`
    (`result_type: "OUTSIDE_PLAN"` on enrich requests). That is not a miss: the
    company exists, it just isn't in the plan, so don't re-enrich it.
    """
    p = payload or {}
    return (p.get("reason") == OUTSIDE_PLAN or p.get("result_type") == "OUTSIDE_PLAN"
            or p.get("status") == OUTSIDE_PLAN)


def enrich_detailed(api_key: str, domain: str | None = None, on_status=None, **kwargs) -> dict:
    """Like `enrich`, but says *why* there's no company.

    Returns {"company": dict | None, "outside_plan": bool, "plan": str | None,
             "request_id": str}.
    Each call counts as one enrichment call, outside_plan results included.
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
    outside = is_outside_plan(results) or is_outside_plan(data)
    return {"company": None if outside else (results.get("company") or None),
            "outside_plan": outside, "plan": results.get("plan") if outside else None,
            "request_id": request_id}


def enrich(api_key: str, domain: str | None = None, on_status=None, **kwargs) -> dict | None:
    """Run the full submit → poll → fetch cycle for a single company.

    Accepts any of the cascade-input keys (see the Grizz API docs):
        gid_company, grizz_id, domain, company_name,
        hq_city, hq_state, hq_country, hq_phone

    `domain` may still be passed positionally; pure-domain callers are
    unchanged.

    Returns:
        Results dict on success, None if there's no company data: not found, or
        outside the caller's plan (use `enrich_detailed` to tell them apart).

    Raises:
        RuntimeError:  If the request fails.
        TimeoutError:  If polling exceeds MAX_POLLS.
        requests.HTTPError: On API errors.
    """
    return enrich_detailed(api_key, domain=domain, on_status=on_status, **kwargs)["company"]


def _echo_key(d: dict) -> tuple:
    """The identity a bulk result echoes back, from either an input dict or a result row."""
    from grizz_enrichment.domain_utils import clean_domain
    get = lambda *ks: next((d.get(k) for k in ks if d.get(k)), "") or ""
    return (clean_domain(get("input_domain", "domain")) or "",
            str(get("input_grizz_id", "gid_company", "grizz_id")).strip(),
            str(get("input_company_name", "company_name")).strip().lower())


def _pair_by_echo(inputs: list[dict], results: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Pair each bulk result with the input it echoes. Returns (pairs, unpaired_results).

    Passes, in priority order: exact cleaned domain, then Grizz id, then company
    name only when exactly one remaining input has that name (branches and
    franchises share names). Inputs with nothing to echo (the server rejects
    them, so they have no result) never take part. There is no positional
    fallback: a result that matches no input is returned unpaired rather than
    guessed onto the wrong record.
    """
    keys = [_echo_key(_cascade_payload(i) if isinstance(i, dict) else {"domain": i}) for i in inputs]
    remaining = [i for i, k in enumerate(keys) if any(k)]
    pairs: list[tuple[dict, dict]] = []
    pending = list(results)
    for field in range(3):
        still: list[dict] = []
        for res in pending:
            value = _echo_key(res)[field]
            cands = [i for i in remaining if value and keys[i][field] == value] if value else []
            if len(cands) > 1:
                # several inputs share this key: narrow by the full echo; take the first
                # only when the remaining candidates are indistinguishable
                full = _echo_key(res)
                exact = [i for i in cands if keys[i] == full]
                cands = exact if exact else cands
                if len({keys[i] for i in cands}) > 1:
                    cands = []
            if cands and (len(cands) == 1 or field < 2):
                hit = cands[0]
                remaining.remove(hit)
                pairs.append((inputs[hit], res))
            else:
                still.append(res)
        pending = still
    return pairs, pending


def enrich_bulk(api_key: str, companies: list[dict]) -> dict:
    """Submit a batch of companies and fetch every completed result.

    Submits via POST /api/v1/companies/enrich-bulk/, polls each PENDING request
    until COMPLETE/FAILED, then fetches results.  Each matched/no_match/
    outside_plan/low_conf/failed entry carries its `input` dict so callers can
    map results back to the input cohort.  Results are paired with inputs by the
    input values the API echoes back (not by position), so a skipped or
    reordered slot can't shift every later result onto the wrong company.

        {
          "matched":     [{"input": {...}, "company": {...}, "request_id": "..."}],
          "no_match":    [{"input": {...}, "request_id": "..."}],
          "outside_plan":[{"input": {...}, "request_id": "..."}],   # in Grizz, not in your plan
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
    outside_plan: list[dict] = []
    low_conf: list[dict] = []
    failed: list[dict]   = []

    pairs, unpaired = _pair_by_echo(companies, initial_requests)
    for req in unpaired:  # never guessed onto an input by position
        failed.append({"input": {}, "request_id": req.get("id"),
                       "error": "result could not be paired with an input"})
    for raw_input, req in pairs:
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

        if is_outside_plan(req):
            outside_plan.append({"input": raw_input, "request_id": request_id})
            continue
        if req.get("result_type") == "NOT_FOUND":
            no_match.append({"input": raw_input, "request_id": request_id})
            continue
        if req.get("result_type") == "LOW_CONF":
            low_conf.append({"input": raw_input, "request_id": request_id})
            continue

        results = fetch_results(api_key, request_id)
        if is_outside_plan(results):
            outside_plan.append({"input": raw_input, "request_id": request_id})
            continue
        company = results.get("company")
        if not company:
            no_match.append({"input": raw_input, "request_id": request_id})
            continue
        matched.append({"input": raw_input, "company": company,
                        "request_id": request_id})

    return {
        "matched":  matched,
        "no_match": no_match,
        "outside_plan": outside_plan,
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
