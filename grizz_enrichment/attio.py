"""Attio support for the Grizz CRM tool — HEADLESS, unlike the Salesforce/HubSpot
adapters.

Attio is integrated differently from the other CRMs on purpose:

  * Matching + field-mapping stay SERVER-SIDE.  We don't replicate Grizz's
    company resolution or the grizz_* slug mapping here; we call the Grizz
    `prepare-writes` endpoint, which returns Attio-ready `values` (the domains
    match-key + every grizz_* field + the grizz_last_sync stamp, phones already
    E.164).  One source of truth, shared with the Grizz MCP.
  * The grizz_* attribute catalog is NOT hardcoded here either.  `setup` fetches
    it from Grizz's setup-instructions endpoint (rendered from the canonical
    catalog, orgs/crm_attio.py), so this tool can never drift from the API/MCP.
  * Dedup is NATIVE.  Attio's `domains` attribute is unique, so we upsert with
    the assert endpoint (PUT …?matching_attribute=domains): existing accounts are
    matched + updated, new ones created — no client-side find/match needed.

So there is no AttioAdapter in ADAPTERS; Attio runs through `run_setup_attio`
(provision the grizz_* attributes) and `run_audience_attio` (bulk-sync an
audience).  Grizz never holds the Attio key — the operator supplies it at runtime
(ATTIO_API_KEY).
"""

from __future__ import annotations

import os

import requests

from grizz_enrichment import grizz_client

ATTIO_BASE = "https://api.attio.com"
PREPARE_BATCH = 200      # /companies/prepare-writes/ caps at 200 per call
TIMEOUT = 60

# Attio rejects unique CUSTOM attributes, so every grizz_* attribute is created
# is_unique=False; dedup keys on Attio's native unique attributes instead
# (companies → domains, people → email_addresses).


# ── Clients ─────────────────────────────────────────────────────────────────────

class AttioError(RuntimeError):
    pass


class Attio:
    def __init__(self, key: str):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})

    def whoami(self) -> dict:
        r = self.s.get(f"{ATTIO_BASE}/v2/self", timeout=TIMEOUT)
        return r.json() if r.status_code == 200 else {}

    def existing_slugs(self, object_slug: str) -> set[str]:
        """All attribute api_slugs on an object, paginated (objects can have >100)."""
        slugs: set[str] = set()
        offset = 0
        while True:
            r = self.s.get(f"{ATTIO_BASE}/v2/objects/{object_slug}/attributes",
                           params={"limit": 100, "offset": offset}, timeout=TIMEOUT)
            if r.status_code != 200:
                raise AttioError(f"list attributes for '{object_slug}' failed "
                                 f"({r.status_code}): {r.text[:200]}")
            batch = r.json().get("data", [])
            slugs.update(a["api_slug"] for a in batch)
            if len(batch) < 100:
                return slugs
            offset += 100

    def create_attribute(self, object_slug: str, api_slug: str, title: str,
                         attio_type: str) -> None:
        body = {"data": {
            "title": title, "description": None, "api_slug": api_slug,
            "type": attio_type,
            "is_required": False, "is_unique": False, "is_multiselect": False,
            "config": {},
        }}
        r = self.s.post(f"{ATTIO_BASE}/v2/objects/{object_slug}/attributes",
                        json=body, timeout=TIMEOUT)
        if r.status_code not in (200, 201):
            raise AttioError(f"create '{api_slug}' on '{object_slug}' failed "
                             f"({r.status_code}): {r.text[:200]}")

    def assert_company(self, values: dict) -> tuple[bool, str]:
        # Upsert keyed on the unique `domains` attribute (its value is inside `values`).
        r = self.s.put(f"{ATTIO_BASE}/v2/objects/companies/records",
                       params={"matching_attribute": "domains"},
                       json={"data": {"values": values}}, timeout=TIMEOUT)
        if r.status_code in (200, 201):
            return True, ""
        return False, f"{r.status_code}: {r.text[:200]}"


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── Operations ──────────────────────────────────────────────────────────────────

def run_setup_attio(grizz_key: str | None = None, attio_key: str | None = None,
                    dry_run: bool = False) -> None:
    """Provision the grizz_* attributes on the Attio Companies + People objects.

    Idempotent — skips attributes that already exist.  The catalog is fetched
    from Grizz (not hardcoded) so it can never drift from the API/MCP.  Attio's
    MCP cannot create attributes, so this REST path is the setup mechanism.
    """
    grizz_key = grizz_key or os.environ.get("GRIZZ_API_KEY")
    attio_key = attio_key or os.environ.get("ATTIO_API_KEY")
    if not grizz_key:
        raise AttioError("No Grizz key. Set GRIZZ_API_KEY.")
    if not attio_key:
        raise AttioError("No Attio key. Set ATTIO_API_KEY.")

    catalog = grizz_client.setup_instructions(grizz_key, "attio").get("attributes") or {}
    if not catalog:
        raise AttioError("Grizz returned no Attio attribute catalog.")

    attio = Attio(attio_key)
    me = attio.whoami()
    print(f"Attio workspace: {me.get('workspace_name') or me.get('workspace_slug') or '?'}")

    plan: dict[str, list[dict]] = {}
    total_missing = 0
    for obj, specs in catalog.items():
        have = attio.existing_slugs(obj)
        missing = [s for s in specs if s["api_slug"] not in have]
        plan[obj] = missing
        total_missing += len(missing)

    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"=== {mode} — {total_missing} attribute(s) to create ===")
    created = errors = 0
    for obj, missing in plan.items():
        print(f"[{obj}] {len(missing)} missing of {len(catalog[obj])}:")
        for spec in missing:
            label = f"{spec['api_slug']} ({spec['type']})"
            if dry_run:
                print(f"  would create: {label}")
                continue
            try:
                attio.create_attribute(obj, spec["api_slug"], spec["title"], spec["type"])
                created += 1
                print(f"  created: {label}")
            except AttioError as e:
                errors += 1
                print(f"  FAILED:  {label} — {e}")
    if dry_run:
        print(f"\nDry run — re-run without --dry-run to create {total_missing} attribute(s).")
    else:
        print(f"\nDone. created={created} errors={errors}")


def run_audience_attio(
    grizz_key: str | None = None,
    attio_key: str | None = None,
    audience_id: str = "",
    dry_run: bool = False,
) -> None:
    """Bulk-sync a Grizz audience into Attio, server-to-server.

    Fetch the audience → ask Grizz to prepare the Attio payloads (batched) →
    assert each onto Attio (upsert on domains).  Use an A/B/C-tier audience for a
    tier sync.  Token-free relative to the MCP path (no per-record echoes).
    """
    from grizz_enrichment.audience_client import fetch_audience

    grizz_key = grizz_key or os.environ.get("GRIZZ_API_KEY")
    attio_key = attio_key or os.environ.get("ATTIO_API_KEY")
    if not grizz_key:
        raise AttioError("No Grizz key. Set GRIZZ_API_KEY.")
    if not attio_key:
        raise AttioError("No Attio key. Set ATTIO_API_KEY.")
    if not audience_id:
        raise AttioError("audience_id is required.")

    attio = Attio(attio_key)
    me = attio.whoami()
    print(f"Attio workspace: {me.get('workspace_name') or me.get('workspace_slug') or '?'}")

    companies = fetch_audience(grizz_key, audience_id)
    refs = [{"gid_company": c["gid_company"]} for c in companies if c.get("gid_company")]
    print(f"Audience {audience_id}: {len(companies)} companies ({len(refs)} with a Grizz id).")

    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"=== {mode} ===")
    written = no_match = errors = 0
    for bi, batch in enumerate(_chunks(refs, PREPARE_BATCH), 1):
        prepared = grizz_client.prepare_writes(grizz_key, "attio", batch)
        for rec in prepared:
            if not rec.get("matched"):
                no_match += 1
                continue
            if dry_run:
                written += 1
                continue
            ok, err = attio.assert_company(rec.get("values") or {})
            if ok:
                written += 1
            else:
                errors += 1
                print(f"  FAILED {rec.get('domain', '?')}: {err}")
        print(f"  batch {bi}: written={written} no_grizz_match={no_match} errors={errors}")

    verb = "would write" if dry_run else "wrote"
    print(f"\nDone. {verb}={written} no_grizz_match={no_match} errors={errors}")
