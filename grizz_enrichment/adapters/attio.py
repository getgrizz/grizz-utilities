"""Attio CRM adapter.

Functions like the Salesforce/HubSpot adapters: it receives already-mapped
{attio_slug: value} dicts (mapping happens client-side in run.py via config.yaml)
and does connect / find / update / create against the Attio REST API.

Attio-specific shaping is kept INSIDE this adapter, the same way HubSpot wraps
records in {"properties": ...}:
  * writes use Attio's {"data": {"values": {...}}} envelope;
  * the native `domains` attribute is multi-value, so it is sent as a list;
  * phone-number attributes must be E.164 or Attio rejects the whole record,
    so phones are normalized (and dropped if they can't be).
"""

import os

import requests

from .base import CRMAdapter

_BASE_URL = "https://api.attio.com"
_OBJECT = "companies"
_QUERY_BATCH = 100   # values per $in query
_QUERY_PAGE = 500    # records per query page
_URL_PREFIXES = ("https://", "http://", "www.")

# Attio slugs that need special value shaping on write.
_DOMAINS_SLUG = "domains"
_PHONE_SLUGS = frozenset({"grizz_phone", "grizz_contact_hq_phone", "grizz_contact_phone"})


def _clean_domain(raw: str) -> str | None:
    """Strip protocol and www from a URL to get a bare domain."""
    domain = raw.strip().lower()
    for prefix in _URL_PREFIXES:
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.rstrip("/").split("/")[0]
    return domain or None


def _to_e164(raw) -> str | None:
    """Best-effort US/NANP E.164 normalization; None if it can't be made valid."""
    if not raw:
        return None
    s = str(raw).strip()
    if s.startswith("+"):
        digits = "".join(ch for ch in s[1:] if ch.isdigit())
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def _first(value):
    """Pull the scalar out of an Attio multi-value list element ({'value': ...})."""
    if isinstance(value, list) and value:
        return value[0].get("value")
    return None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class AttioAdapter(CRMAdapter):

    def __init__(self):
        self._session: requests.Session | None = None

    def connect(self) -> None:
        """Connect using ATTIO_API_KEY."""
        token = os.environ.get("ATTIO_API_KEY")
        if not token:
            raise RuntimeError("ATTIO_API_KEY is not set.")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        resp = self._session.get(f"{_BASE_URL}/v2/self", timeout=10)
        if resp.status_code == 401:
            raise RuntimeError("ATTIO_API_KEY is invalid or lacks required scopes.")
        resp.raise_for_status()

    # ── reads ────────────────────────────────────────────────────────────────

    def _query(self, filter_: dict) -> list[dict]:
        """Run a records query, paging through all results."""
        out: list[dict] = []
        offset = 0
        while True:
            resp = self._session.post(
                f"{_BASE_URL}/v2/objects/{_OBJECT}/records/query",
                json={"filter": filter_, "limit": _QUERY_PAGE, "offset": offset},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json().get("data", [])
            out.extend(batch)
            if len(batch) < _QUERY_PAGE:
                return out
            offset += _QUERY_PAGE

    def get_domain(self, record_id: str, domain_field: str) -> str | None:
        """Return the company's primary domain (domain_field is 'domains')."""
        resp = self._session.get(
            f"{_BASE_URL}/v2/objects/{_OBJECT}/records/{record_id}", timeout=10,
        )
        resp.raise_for_status()
        domains = resp.json().get("data", {}).get("values", {}).get(domain_field, [])
        for d in domains:
            root = d.get("root_domain") or d.get("domain")
            if root:
                return root
        return None

    def find_account(self, domain: str | None, grizz_id: str | None) -> str | None:
        """Look up one company by grizz_company_id, then by domain."""
        if grizz_id:
            data = self._query({"grizz_company_id": str(grizz_id)})
            if data:
                return data[0]["id"]["record_id"]
        if domain:
            data = self._query({"domains": {"root_domain": _clean_domain(domain)}})
            if data:
                return data[0]["id"]["record_id"]
        return None

    def find_accounts_bulk(self, companies: list[dict], grizz_id_field: str) -> dict[str, str]:
        """Match companies to Attio records by grizz_id_field, then by domain.

        Mirrors the HubSpot adapter: returns {grizz_id: record_id} for id matches
        and {domain: record_id} for domain-only matches.
        """
        matched: dict[str, str] = {}

        grizz_ids = [str(c["grizz_id"]) for c in companies if c.get("grizz_id")]
        for batch in _chunks(grizz_ids, _QUERY_BATCH):
            for rec in self._query({grizz_id_field: {"$in": batch}}):
                gid = _first(rec.get("values", {}).get(grizz_id_field))
                if gid:
                    matched[str(gid)] = rec["id"]["record_id"]

        matched_ids = set(matched)
        unmatched_domains = [
            _clean_domain(c["domain"]) for c in companies
            if c.get("domain") and str(c.get("grizz_id", "")) not in matched_ids
        ]
        unmatched_domains = [d for d in unmatched_domains if d]
        for batch in _chunks(unmatched_domains, _QUERY_BATCH):
            ors = [{"domains": {"root_domain": d}} for d in batch]
            for rec in self._query({"$or": ors}):
                for dom in rec.get("values", {}).get("domains", []):
                    root = dom.get("root_domain")
                    if root in batch:
                        matched[root] = rec["id"]["record_id"]
        return matched

    # ── writes ───────────────────────────────────────────────────────────────

    @staticmethod
    def _shape(fields: dict) -> dict:
        """Turn a mapped {slug: value} dict into an Attio `values` payload."""
        values: dict = {}
        for slug, value in fields.items():
            if slug == "Id":
                continue
            if slug == _DOMAINS_SLUG:
                values[slug] = value if isinstance(value, list) else [value]
            elif slug in _PHONE_SLUGS:
                e164 = _to_e164(value)
                if e164:                       # Attio rejects the record otherwise
                    values[slug] = e164
            else:
                values[slug] = value
        return values

    def update_record(self, record_id: str, fields: dict) -> None:
        """Patch a single company record."""
        resp = self._session.patch(
            f"{_BASE_URL}/v2/objects/{_OBJECT}/records/{record_id}",
            json={"data": {"values": self._shape(fields)}}, timeout=15,
        )
        resp.raise_for_status()

    def update_accounts(self, records: list[dict]) -> list[dict]:
        """Update companies one record at a time (Attio has no batch write API).

        Each record must include an 'Id' key.
        """
        results: list[dict] = []
        for r in records:
            record_id = r["Id"]
            try:
                self.update_record(record_id, r)
                results.append({"id": record_id, "success": True, "errors": []})
            except requests.HTTPError as e:
                body = e.response.text[:200] if e.response is not None else str(e)
                results.append({"id": record_id, "success": False, "errors": [body]})
        return results

    def create_accounts(self, records: list[dict]) -> list[dict]:
        """Create companies one record at a time (Attio has no batch write API)."""
        results: list[dict] = []
        for r in records:
            try:
                resp = self._session.post(
                    f"{_BASE_URL}/v2/objects/{_OBJECT}/records",
                    json={"data": {"values": self._shape(r)}}, timeout=15,
                )
                resp.raise_for_status()
                new_id = resp.json().get("data", {}).get("id", {}).get("record_id", "")
                results.append({"id": new_id, "success": True, "errors": []})
            except requests.HTTPError as e:
                body = e.response.text[:200] if e.response is not None else str(e)
                results.append({"id": "", "success": False, "errors": [body]})
        return results
