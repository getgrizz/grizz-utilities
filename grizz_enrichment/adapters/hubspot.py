"""HubSpot CRM adapter."""

import os
import time

import requests

from .base import CRMAdapter

_BASE_URL    = "https://api.hubapi.com"
_SEARCH_BATCH = 50   # max values per IN filter (HubSpot limit)
_WRITE_BATCH  = 100  # max records per batch create/update (HubSpot limit)

_URL_PREFIXES = ("https://", "http://", "www.")


def _clean_domain(raw: str) -> str | None:
    """Strip protocol and www from a URL to get a bare domain."""
    domain = raw.strip().lower()
    for prefix in _URL_PREFIXES:
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.rstrip("/").split("/")[0]
    return domain or None


class HubSpotAdapter(CRMAdapter):

    def __init__(self):
        self._session: requests.Session | None = None

    def connect(self) -> None:
        """Connect using HUBSPOT_API_KEY (Private App token)."""
        token = os.environ.get("HUBSPOT_API_KEY")
        if not token:
            raise RuntimeError("HUBSPOT_API_KEY is not set.")

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

        # Lightweight validation call
        resp = self._session.get(
            f"{_BASE_URL}/crm/v3/objects/companies",
            params={"limit": 1},
            timeout=10,
        )
        if resp.status_code == 401:
            raise RuntimeError("HUBSPOT_API_KEY is invalid or lacks required scopes.")
        resp.raise_for_status()

    def get_domain(self, record_id: str, domain_field: str) -> str | None:
        """Fetch a company record and return its domain field value."""
        resp = self._session.get(
            f"{_BASE_URL}/crm/v3/objects/companies/{record_id}",
            params={"properties": domain_field},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json().get("properties", {}).get(domain_field) or ""
        return _clean_domain(raw) if raw else None

    def update_record(self, record_id: str, fields: dict) -> None:
        """Patch a single company record with the provided fields."""
        resp = self._session.patch(
            f"{_BASE_URL}/crm/v3/objects/companies/{record_id}",
            json={"properties": fields},
            timeout=10,
        )
        resp.raise_for_status()

    def find_account(self, domain: str | None, grizz_id: str | None) -> str | None:
        """Look up a company by grizz_company_id or domain.

        Tries grizz_id first (exact match), then domain.
        Returns the HubSpot company ID, or None if not found.
        """
        if grizz_id:
            results = self._search(
                {"propertyName": "grizz_company_id", "operator": "EQ", "value": str(grizz_id)},
                properties=["grizz_company_id"],
            )
            if results:
                return results[0]["id"]

        if domain:
            results = self._search(
                {"propertyName": "domain", "operator": "EQ", "value": domain},
                properties=["domain"],
            )
            if results:
                return results[0]["id"]

        return None

    def find_accounts_bulk(self, companies: list[dict], grizz_id_field: str) -> dict[str, str]:
        """Match companies to HubSpot companies in bulk using search.

        Queries in batches of 50 to stay within the IN filter limit.
        Respects the Search API rate limit of 4 req/s via a small sleep.

        Returns {grizz_id: hs_company_id} for grizz_id matches, and
        {domain: hs_company_id} for domain-only matches.
        """
        matched: dict[str, str] = {}

        grizz_ids = [str(c["grizz_id"]) for c in companies if c.get("grizz_id")]
        domains   = [c["domain"] for c in companies if c.get("domain")]

        # Match by grizz_id_field from config
        for i in range(0, len(grizz_ids), _SEARCH_BATCH):
            batch = grizz_ids[i:i + _SEARCH_BATCH]
            results = self._search(
                {"propertyName": grizz_id_field, "operator": "IN", "values": batch},
                properties=[grizz_id_field],
            )
            for record in results:
                gid = (record.get("properties") or {}).get(grizz_id_field)
                if gid:
                    matched[gid] = record["id"]
            time.sleep(0.25)  # respect 4 req/s Search API limit

        # Match remaining unmatched companies by domain
        matched_grizz_ids = set(matched.keys())
        unmatched_domains = [
            c["domain"] for c in companies
            if c.get("domain") and str(c.get("grizz_id", "")) not in matched_grizz_ids
        ]
        for i in range(0, len(unmatched_domains), _SEARCH_BATCH):
            batch = unmatched_domains[i:i + _SEARCH_BATCH]
            results = self._search(
                {"propertyName": "domain", "operator": "IN", "values": batch},
                properties=["domain"],
            )
            for record in results:
                domain = (record.get("properties") or {}).get("domain")
                if domain and domain in batch:
                    matched[domain] = record["id"]
            time.sleep(0.25)

        return matched

    def update_accounts(self, records: list[dict]) -> list[dict]:
        """Update companies in batches of 100 using the batch update API.

        Each record must include an 'Id' key.
        Returns a list of result dicts with keys: id, success, errors.
        """
        payload = [
            {"id": r["Id"], "properties": {k: v for k, v in r.items() if k != "Id"}}
            for r in records
        ]
        results: list[dict] = []
        for i in range(0, len(payload), _WRITE_BATCH):
            resp = self._post_with_retry(
                f"{_BASE_URL}/crm/v3/objects/companies/batch/update",
                {"inputs": payload[i:i + _WRITE_BATCH]},
            )
            for item in resp.json().get("results", []):
                results.append({"id": item["id"], "success": True, "errors": []})
        return results

    def create_accounts(self, records: list[dict]) -> list[dict]:
        """Create companies in batches of 100 using the batch create API.

        Each record should be a plain field dict (no 'Id' key).
        Returns a list of result dicts with keys: id, success, errors.
        """
        payload = [{"properties": r} for r in records]
        results: list[dict] = []
        for i in range(0, len(payload), _WRITE_BATCH):
            resp = self._post_with_retry(
                f"{_BASE_URL}/crm/v3/objects/companies/batch/create",
                {"inputs": payload[i:i + _WRITE_BATCH]},
            )
            for item in resp.json().get("results", []):
                results.append({"id": item["id"], "success": True, "errors": []})
        return results

    def _post_with_retry(self, url: str, body: dict, max_retries: int = 4) -> requests.Response:
        """POST with automatic 429 retry, respecting the Retry-After header."""
        for attempt in range(max_retries + 1):
            resp = self._session.post(url, json=body, timeout=30)
            if resp.status_code == 429 and attempt < max_retries:
                wait = int(resp.headers.get("Retry-After", "10"))
                print(f"\n  Rate limited — waiting {wait}s before retry {attempt + 1}/{max_retries}...", end=" ", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()  # unreachable, satisfies type checkers
        return resp

    def _search(self, filter_: dict, properties: list[str]) -> list[dict]:
        """Execute a single-filter company search and return all results."""
        body = {
            "filterGroups": [{"filters": [filter_]}],
            "properties": properties,
            "limit": 100,
        }
        resp = self._session.post(
            f"{_BASE_URL}/crm/v3/objects/companies/search",
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
