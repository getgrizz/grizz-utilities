"""Provision Grizz custom attributes on Attio (the `setup --crm attio` path).

Sibling of setup_salesforce / setup_hubspot.  Two Attio-specific differences,
both forced by Attio's design:

  * The attribute catalog is fetched from Grizz at runtime (not hardcoded like
    the Salesforce/HubSpot field lists) so it can never drift from the API/MCP.
    This is why setup needs GRIZZ_API_KEY in addition to ATTIO_API_KEY.
  * Attio rejects unique CUSTOM attributes, so every grizz_* attribute is created
    is_unique=False; dedup keys on Attio's native unique attributes instead
    (companies → domains, people → email_addresses).
"""

import os

import requests

from grizz_enrichment import grizz_client

_BASE_URL = "https://api.attio.com"
_TIMEOUT = 30


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


def _existing_slugs(session: requests.Session, object_slug: str) -> set[str]:
    """All attribute api_slugs on an object, paginated (objects can have >100)."""
    slugs: set[str] = set()
    offset = 0
    while True:
        resp = session.get(
            f"{_BASE_URL}/v2/objects/{object_slug}/attributes",
            params={"limit": 100, "offset": offset}, timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"list attributes for '{object_slug}' failed "
                               f"({resp.status_code}): {resp.text[:200]}")
        batch = resp.json().get("data", [])
        slugs.update(a["api_slug"] for a in batch)
        if len(batch) < 100:
            return slugs
        offset += 100


def _create_attribute(session: requests.Session, object_slug: str, spec: dict) -> None:
    body = {"data": {
        "title": spec["title"], "description": None, "api_slug": spec["api_slug"],
        "type": spec["type"],
        "is_required": False, "is_unique": False, "is_multiselect": False,
        "config": {},
    }}
    resp = session.post(
        f"{_BASE_URL}/v2/objects/{object_slug}/attributes", json=body, timeout=_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"create '{spec['api_slug']}' on '{object_slug}' failed "
                           f"({resp.status_code}): {resp.text[:200]}")


def run_setup(dry_run: bool = False) -> dict:
    """Create the grizz_* attributes on the Attio Companies + People objects.

    Idempotent — skips attributes that already exist.  Returns the standard
    {created, existed, errors, dry_run} summary used by the other setup modules.
    """
    grizz_key = os.environ.get("GRIZZ_API_KEY")
    attio_key = os.environ.get("ATTIO_API_KEY")
    if not grizz_key:
        raise RuntimeError("GRIZZ_API_KEY is not set (needed to fetch the attribute catalog).")
    if not attio_key:
        raise RuntimeError("ATTIO_API_KEY is not set.")

    catalog = grizz_client.setup_instructions(grizz_key, "attio").get("attributes") or {}
    if not catalog:
        raise RuntimeError("Grizz returned no Attio attribute catalog.")

    session = _session(attio_key)
    me = session.get(f"{_BASE_URL}/v2/self", timeout=_TIMEOUT)
    if me.status_code == 200:
        info = me.json()
        print(f"Attio workspace: {info.get('workspace_name') or info.get('workspace_slug') or '?'}")

    created = existed = errors = would_create = 0
    for obj, specs in catalog.items():
        have = _existing_slugs(session, obj)
        missing = [s for s in specs if s["api_slug"] not in have]
        existed += len(specs) - len(missing)
        print(f"[{obj}] {len(missing)} missing of {len(specs)}:")
        for spec in missing:
            label = f"{spec['api_slug']} ({spec['type']})"
            if dry_run:
                would_create += 1
                print(f"  would create: {label}")
                continue
            try:
                _create_attribute(session, obj, spec)
                created += 1
                print(f"  created: {label}")
            except RuntimeError as e:
                errors += 1
                print(f"  FAILED:  {label} — {e}")

    if dry_run:
        print(f"\nDry run — re-run without --dry-run to create {would_create} attribute(s).")
    else:
        print(f"\nDone. created={created} existed={existed} errors={errors}")

    return {"created": created, "existed": existed, "errors": errors, "dry_run": dry_run}
