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

Two objects share this file: `AttioAdapter` (Companies — the setup/enrich/audience
flow) and `AttioContactAdapter` (People — the `people sync --crm attio` flow).
Everything object-agnostic (connect, retrying HTTP, the {slug: value} → Attio
`values` shaping, the concurrent write pool) lives on `_AttioBase`; each subclass
adds only its object's find / create / update semantics.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from .base import CRMAdapter

_BASE_URL = "https://api.attio.com"
_GID_SLUG = "grizz_gid"              # gid_company — the canonical driving id (api.md §10)
_LAST_SYNC_SLUG = "grizz_last_sync"  # stamped (now) on every write so freshness tracks
_QUERY_BATCH = 100   # values per $in query
_QUERY_PAGE = 500    # records per query page
_WRITE_CONCURRENCY = 12  # parallel writes — Attio caps writes at 25/s; 429 bursts back off (Retry-After)
_READ_TIMEOUT = 30   # seconds, GET/query
_WRITE_TIMEOUT = 60  # seconds, PUT/PATCH/POST (Attio writes can be slow under load)
_MAX_RETRIES = 4     # transient-failure retries (timeout / connection / 429 / 5xx)
_URL_PREFIXES = ("https://", "http://", "www.")

# Attio slugs that need special value shaping on write.
_DOMAINS_SLUG = "domains"
_PHONE_SLUGS = frozenset({"grizz_phone", "grizz_hq_phone",
                          "grizz_contact_hq_phone", "grizz_contact_phone"})


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


# ── Object-typed location support ─────────────────────────────────────────────
# Attio location attributes are object-typed: config maps source fields to
# sub-fields via dotted slugs (e.g. grizz_location.locality / .region /
# .country_code), which _shape groups back into one object value.
_LOCATION_SUBFIELDS = ("line_1", "line_2", "line_3", "line_4",
                       "locality", "region", "postcode", "country_code")

# Country name → ISO-3166 alpha-2 (Attio requires alpha-2). 2-letter inputs pass
# through; anything unresolved makes _build_location omit the field.
_COUNTRY_ISO2 = {
    "united states": "US", "united states of america": "US", "usa": "US",
    "u.s.": "US", "u.s.a.": "US", "america": "US",
    "canada": "CA", "mexico": "MX",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB", "ireland": "IE",
    "australia": "AU", "new zealand": "NZ",
    "germany": "DE", "france": "FR", "spain": "ES", "italy": "IT",
    "portugal": "PT", "netherlands": "NL", "belgium": "BE", "switzerland": "CH",
    "austria": "AT", "sweden": "SE", "norway": "NO", "denmark": "DK",
    "finland": "FI", "poland": "PL", "czech republic": "CZ", "czechia": "CZ",
    "india": "IN", "china": "CN", "japan": "JP", "south korea": "KR",
    "korea": "KR", "singapore": "SG", "philippines": "PH", "indonesia": "ID",
    "malaysia": "MY", "thailand": "TH", "vietnam": "VN",
    "brazil": "BR", "argentina": "AR", "chile": "CL", "colombia": "CO",
    "peru": "PE", "south africa": "ZA", "israel": "IL",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA",
}


def _iso2(country) -> str | None:
    """Normalize a country name/code to ISO-3166 alpha-2 (Attio requires it).
    Passes through a 2-letter code; looks a full name up in _COUNTRY_ISO2;
    returns None when it can't be resolved."""
    if not country:
        return None
    s = str(country).strip()
    if not s:
        return None
    up = s.upper()
    if len(up) == 2 and up.isalpha():
        return up
    return _COUNTRY_ISO2.get(s.lower())


def _build_location(parts: dict) -> dict | None:
    """Assemble an Attio location object from its dotted-slug sub-fields.

    Returns None — so the caller OMITS the attribute rather than 400-ing the
    whole record — when the country can't be resolved to alpha-2, or there's no
    locality/region to place. All sub-fields are sent (empty where unknown);
    latitude/longitude are null."""
    cc = _iso2(parts.get("country_code") or parts.get("country"))
    if not cc:
        return None
    if not (parts.get("locality") or parts.get("region")):
        return None
    loc = {sub: (parts.get(sub) or "") for sub in _LOCATION_SUBFIELDS}
    loc["country_code"] = cc
    loc["latitude"] = None
    loc["longitude"] = None
    return loc


def _first(value):
    """Pull the scalar out of an Attio multi-value list element ({'value': ...})."""
    if isinstance(value, list) and value:
        return value[0].get("value")
    return None


def _retry_after_seconds(resp, default: int = 5, cap: int = 30) -> int:
    """Seconds to wait from a 429's Retry-After header. Per RFC 7231 it may be an
    integer count OR an HTTP-date — Attio returns the date form, which a plain
    int() can't parse (that crash failed whole batches). Handles both; capped."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return default
    try:
        return max(1, min(int(raw), cap))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(1, min(int(delta), cap))
    except Exception:
        return default


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class _AttioBase:
    """Object-agnostic Attio plumbing shared by the Companies and People adapters:
    connection + pooling, retrying HTTP, records query, {slug: value} → Attio
    `values` shaping (incl. object-typed location assembly), the fragile-field
    400-retry, and the concurrent per-record write pool.  Subclasses set
    ``_OBJECT`` and add their object's find / create / update methods."""

    _OBJECT = ""                       # attio object slug, set by subclass
    _LAST_SYNC = _LAST_SYNC_SLUG       # slug stamped on every write (opt-in via env)
    _FRAGILE_LIST_SLUGS: frozenset = frozenset()  # list-typed slugs safe to drop on 400

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
        # Size the connection pool to the write concurrency, else threads above the
        # default pool_maxsize (10) queue on connections and don't actually run in
        # parallel.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=_WRITE_CONCURRENCY, pool_maxsize=_WRITE_CONCURRENCY,
        )
        self._session.mount("https://", adapter)
        resp = self._session.get(f"{_BASE_URL}/v2/self", timeout=_READ_TIMEOUT)
        if resp.status_code == 401:
            raise RuntimeError("ATTIO_API_KEY is invalid or lacks required scopes.")
        resp.raise_for_status()

    def _request(self, method: str, url: str, *, timeout: int, **kwargs) -> requests.Response:
        """HTTP with retry/backoff on transient failures (timeout, connection drop,
        429, 5xx).  Non-transient 4xx responses are returned for the caller to
        raise_for_status; transient exceptions are re-raised only after exhausting
        retries.  Attio has no batch write API, so each record is its own request —
        making single-request resilience the right place to absorb blips."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, timeout=timeout, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if attempt < _MAX_RETRIES:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                time.sleep(_retry_after_seconds(resp))
                continue
            if resp.status_code >= 500 and attempt < _MAX_RETRIES:
                time.sleep(min(2 ** attempt, 10))
                continue
            return resp
        raise last_exc  # pragma: no cover

    # ── reads ────────────────────────────────────────────────────────────────

    def _query(self, filter_: dict) -> list[dict]:
        """Run a records query on this adapter's object, paging through all results."""
        out: list[dict] = []
        offset = 0
        while True:
            resp = self._request(
                "POST", f"{_BASE_URL}/v2/objects/{self._OBJECT}/records/query",
                timeout=_READ_TIMEOUT,
                json={"filter": filter_, "limit": _QUERY_PAGE, "offset": offset},
            )
            resp.raise_for_status()
            batch = resp.json().get("data", [])
            out.extend(batch)
            if len(batch) < _QUERY_PAGE:
                return out
            offset += _QUERY_PAGE

    # ── writes ────────────────────────────────────────────────────────────────

    def _shape(self, fields: dict) -> dict:
        """Turn a mapped {slug: value} dict into an Attio `values` payload.

        Dotted slugs (e.g. grizz_location.locality) are grouped by their object
        slug and assembled into one object value via _build_location. The
        last-sync stamp is OPT-IN via GRIZZ_STAMP_LAST_SYNC=1: some Attio
        workspaces have no last-sync attribute, and stamping an unknown slug 400s
        the whole record."""
        values: dict = {}
        obj_parts: dict[str, dict] = {}   # obj_slug -> {sub: value}
        for slug, value in fields.items():
            if slug == "Id":
                continue
            if "." in slug:
                obj_slug, sub = slug.split(".", 1)
                obj_parts.setdefault(obj_slug, {})[sub] = value
            elif slug == _DOMAINS_SLUG:
                values[slug] = value if isinstance(value, list) else [value]
            elif slug in _PHONE_SLUGS:
                e164 = _to_e164(value)
                if e164:                       # drop if not normalizable (Attio rejects malformed)
                    values[slug] = e164
            else:
                values[slug] = value
        for obj_slug, parts in obj_parts.items():
            loc = _build_location(parts)
            if loc is not None:                # omit rather than 400 on an unresolvable object
                values[obj_slug] = loc
        if os.getenv("GRIZZ_STAMP_LAST_SYNC") == "1" and self._LAST_SYNC:
            values[self._LAST_SYNC] = datetime.now(timezone.utc).isoformat()
        return values

    @staticmethod
    def _err(e: Exception) -> str:
        resp = getattr(e, "response", None)
        return resp.text[:200] if resp is not None else f"{type(e).__name__}: {e}"

    def _write(self, method: str, url: str, fields: dict) -> requests.Response:
        """Shape + send a write. If Attio rejects a fragile typed value (400) —
        a phone that isn't E.164, an object-typed value like a location, or a
        list-typed native attribute named in _FRAGILE_LIST_SLUGS — drop the
        offending field(s) named in the error and retry once, so every OTHER
        field on the record still lands. A bad phone/location never costs the
        whole record."""
        values = self._shape(fields)
        resp = self._request(method, url, timeout=_WRITE_TIMEOUT, json={"data": {"values": values}})
        if resp.status_code == 400:
            fragile = [s for s, v in values.items()
                       if (s in _PHONE_SLUGS or isinstance(v, dict)
                           or s in self._FRAGILE_LIST_SLUGS) and s in resp.text]
            if fragile:
                for s in fragile:
                    values.pop(s, None)
                resp = self._request(method, url, timeout=_WRITE_TIMEOUT,
                                     json={"data": {"values": values}})
        resp.raise_for_status()
        return resp

    def _parallel(self, records: list[dict], fn) -> list[dict]:
        """Run a per-record write fn concurrently (Attio has no batch-write API),
        preserving input order.  Each fn isolates its own errors, so the pool
        never aborts on one bad record; transient 429/timeout backoff (in
        _request) keeps the pool busy rather than failing fast."""
        if not records:
            return []
        workers = min(_WRITE_CONCURRENCY, len(records))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(fn, records))


class AttioAdapter(_AttioBase, CRMAdapter):

    _OBJECT = "companies"

    # ── reads ────────────────────────────────────────────────────────────────

    def get_domain(self, record_id: str, domain_field: str) -> str | None:
        """Return the company's primary domain (domain_field is 'domains')."""
        resp = self._request(
            "GET", f"{_BASE_URL}/v2/objects/{self._OBJECT}/records/{record_id}",
            timeout=_READ_TIMEOUT,
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
        """Match companies to Attio records, DRIVEN BY gid_company (grizz_gid — the
        canonical id, api.md §10), then falling back to the native `domains`
        attribute for records that don't carry a gid yet (e.g. created before gids
        existed; the domain fallback prevents duplicates, and our write stamps the
        gid so they match directly next time).

        `grizz_id_field` (the legacy integer-id slug) is accepted for interface
        compatibility but is no longer the primary match key.  The returned map is
        keyed by what run_audience looks up — the legacy grizz_id value and the raw
        domain — even though matching is gid-driven.
        """
        matched: dict[str, str] = {}

        def _keys(c: dict) -> list[str]:
            ks = []
            if c.get("grizz_id"):
                ks.append(str(c["grizz_id"]))
            if c.get("domain"):
                ks.append(c["domain"])
            return ks

        # 1) primary: gid_company -> Attio grizz_gid
        by_gid = {str(c["gid_company"]): c for c in companies if c.get("gid_company")}
        gids_hit: set[str] = set()
        for batch in _chunks(list(by_gid), _QUERY_BATCH):
            for rec in self._query({_GID_SLUG: {"$in": batch}}):
                recgid = _first(rec.get("values", {}).get(_GID_SLUG))
                c = by_gid.get(str(recgid))
                if c:
                    gids_hit.add(str(recgid))
                    for k in _keys(c):
                        matched[k] = rec["id"]["record_id"]

        # 2) fallback: native domains for companies not matched by gid
        rem = [c for g, c in by_gid.items() if g not in gids_hit]
        rem += [c for c in companies if not c.get("gid_company")]
        dom_to_company = {
            d: c for c in rem
            if c.get("domain") and (d := _clean_domain(c["domain"]))
        }
        for batch in _chunks(list(dom_to_company), _QUERY_BATCH):
            ors = [{"domains": {"root_domain": d}} for d in batch]
            for rec in self._query({"$or": ors}):
                for dom in rec.get("values", {}).get("domains", []):
                    c = dom_to_company.get(dom.get("root_domain"))
                    if c:
                        for k in _keys(c):
                            matched[k] = rec["id"]["record_id"]
        return matched

    def gid_to_record_ids(self, gids: list[str]) -> dict[str, str]:
        """Map gid_company → Attio company record_id, for linking people to their
        parent company (the People adapter's native company reference)."""
        out: dict[str, str] = {}
        uniq = list(dict.fromkeys(str(g) for g in gids if g))
        for batch in _chunks(uniq, _QUERY_BATCH):
            for rec in self._query({_GID_SLUG: {"$in": batch}}):
                g = _first(rec.get("values", {}).get(_GID_SLUG))
                if g:
                    out[str(g)] = rec["id"]["record_id"]
        return out

    # ── writes ───────────────────────────────────────────────────────────────

    def update_record(self, record_id: str, fields: dict) -> None:
        """Patch a single company record."""
        resp = self._write(
            "PATCH", f"{_BASE_URL}/v2/objects/{self._OBJECT}/records/{record_id}", fields,
        )
        resp.raise_for_status()

    def _update_one(self, r: dict) -> dict:
        record_id = r["Id"]
        try:
            self.update_record(record_id, r)
            return {"id": record_id, "success": True, "errors": []}
        except Exception as e:  # isolate ANY per-record failure, never abort the batch
            return {"id": record_id, "success": False, "errors": [self._err(e)]}

    def _create_one(self, r: dict) -> dict:
        try:
            resp = self._write("POST", f"{_BASE_URL}/v2/objects/{self._OBJECT}/records", r)
            new_id = resp.json().get("data", {}).get("id", {}).get("record_id", "")
            return {"id": new_id, "success": True, "errors": []}
        except Exception as e:  # isolate ANY per-record failure, never abort the batch
            return {"id": "", "success": False, "errors": [self._err(e)]}

    def update_accounts(self, records: list[dict]) -> list[dict]:
        """Update companies concurrently (Attio has no batch write API).

        Each record must include an 'Id' key.  Per-record failures (including
        timeouts/connection drops, after retries) are isolated — one bad record
        never fails the rest.
        """
        return self._parallel(records, self._update_one)

    def create_accounts(self, records: list[dict]) -> list[dict]:
        """Create companies concurrently.  Per-record failures are isolated."""
        return self._parallel(records, self._create_one)


class AttioContactAdapter(_AttioBase):
    """People-object twin of AttioAdapter for `people sync --crm attio`.

    Same client-side model as the company path — config.yaml maps Grizz person
    fields to Attio People attributes, and the shared _shape collapses
    city/state/country into ONE object-typed location attribute (e.g.
    grizz_contact_location) exactly like companies collapse into grizz_location.

    Beyond the mapped grizz_* attributes, People carry a handful of NATIVE typed
    attributes that need their own shaping (personal-name, email/phone lists, the
    Companies record-reference).  Their slugs are workspace-configurable, so they
    come from config.yaml via use_native_slugs() rather than being hardcoded.
    Dedup is on the person's Grizz gid (a configured id attribute) first, then the
    native unique email attribute — mirroring the company gid → domains cascade.
    """

    _OBJECT = "people"
    _LAST_SYNC = "grizz_contact_last_sync"

    # config-driven slugs, populated by use_native_slugs()
    _name_slug = "name"
    _email_slug = "email_addresses"
    _phone_slug = "phone_numbers"
    _company_ref_slug = "company"
    _person_id_slug: str | None = None   # the grizz person-gid attribute, if mapped

    def use_native_slugs(self, *, name: str, email: str, phone: str,
                         company_ref: str, person_id: str | None,
                         last_sync: str | None = None) -> None:
        """Point the adapter at this workspace's native People attribute slugs
        (from config.yaml) and the grizz person-id attribute used for dedup."""
        self._name_slug = name
        self._email_slug = email
        self._phone_slug = phone
        self._company_ref_slug = company_ref
        self._person_id_slug = person_id
        if last_sync:
            self._LAST_SYNC = last_sync
        # phone + company-ref are non-essential typed lists — safe to drop on a
        # 400 so the rest of the record still lands. email is the dedup key and
        # name is a dict (already covered by _write), so neither is auto-dropped.
        self._FRAGILE_LIST_SLUGS = frozenset({self._phone_slug, self._company_ref_slug})

    # ── reads ────────────────────────────────────────────────────────────────

    def find_people_bulk(self, people: list[dict]) -> dict[str, str]:
        """Match discovered people to existing Attio person records — by the
        Grizz person gid (the configured id attribute) first, then falling back to
        the native unique email attribute.  Returns a map keyed by BOTH the gid
        and the lowercased email, so run.py can look a record up by either."""
        matched: dict[str, str] = {}

        by_gid = {str(p["gid"]): p for p in people if p.get("gid")} if self._person_id_slug else {}
        gids_hit: set[str] = set()

        # 1) primary: gid_person -> the configured grizz person-id attribute
        if self._person_id_slug and by_gid:
            for batch in _chunks(list(by_gid), _QUERY_BATCH):
                for rec in self._query({self._person_id_slug: {"$in": batch}}):
                    recgid = _first(rec.get("values", {}).get(self._person_id_slug))
                    p = by_gid.get(str(recgid))
                    if p:
                        gids_hit.add(str(recgid))
                        rid = rec["id"]["record_id"]
                        matched[str(p["gid"])] = rid
                        if p.get("email"):
                            matched[p["email"].lower()] = rid

        # 2) fallback: native email for people not matched by gid
        rem = [p for g, p in by_gid.items() if g not in gids_hit]
        rem += [p for p in people if not p.get("gid")]
        email_to_p = {e: p for p in rem if (e := (p.get("email") or "").strip().lower())}
        for batch in _chunks(list(email_to_p), _QUERY_BATCH):
            ors = [{self._email_slug: {"email_address": e}} for e in batch]
            for rec in self._query({"$or": ors}):
                rid = rec["id"]["record_id"]
                for ev in rec.get("values", {}).get(self._email_slug, []):
                    ea = (ev.get("email_address") or "").strip().lower()
                    p = email_to_p.get(ea)
                    if p:
                        matched[ea] = rid
                        if p.get("gid"):
                            matched[str(p["gid"])] = rid
        return matched

    # ── writes ────────────────────────────────────────────────────────────────

    def _shape(self, fields: dict) -> dict:
        """Shape a person record: the mapped grizz_* attributes go through the
        shared shaper (collapsing the object-typed location), then the NATIVE
        People attributes carried under the '_native' key are added in their
        Attio-specific shapes (personal-name object, email/phone lists, the
        Companies record-reference)."""
        native = fields.get("_native") or {}
        grizz = {k: v for k, v in fields.items() if k not in ("Id", "_native")}
        values = super()._shape(grizz)

        name = native.get("name")
        if name:
            values[self._name_slug] = name
        email = native.get("email")
        if email:
            values[self._email_slug] = [email]
        phone = _to_e164(native.get("phone"))
        if phone:
            values[self._phone_slug] = [{"original_phone_number": phone}]
        company_rid = native.get("company_record_id")
        if company_rid:
            values[self._company_ref_slug] = [
                {"target_object": "companies", "target_record_id": company_rid}
            ]
        return values

    def _update_one(self, r: dict) -> dict:
        record_id = r["Id"]
        try:
            resp = self._write(
                "PATCH", f"{_BASE_URL}/v2/objects/{self._OBJECT}/records/{record_id}", r)
            resp.raise_for_status()
            return {"id": record_id, "success": True, "errors": []}
        except Exception as e:  # isolate ANY per-record failure, never abort the batch
            return {"id": record_id, "success": False, "errors": [self._err(e)]}

    def _create_one(self, r: dict) -> dict:
        try:
            resp = self._write("POST", f"{_BASE_URL}/v2/objects/{self._OBJECT}/records", r)
            new_id = resp.json().get("data", {}).get("id", {}).get("record_id", "")
            return {"id": new_id, "success": True, "errors": []}
        except Exception as e:  # isolate ANY per-record failure, never abort the batch
            return {"id": "", "success": False, "errors": [self._err(e)]}

    def update_people(self, records: list[dict]) -> list[dict]:
        """Update person records concurrently.  Each needs an 'Id'; per-record
        failures are isolated."""
        return self._parallel(records, self._update_one)

    def create_people(self, records: list[dict]) -> list[dict]:
        """Create person records concurrently.  Per-record failures are isolated."""
        return self._parallel(records, self._create_one)
