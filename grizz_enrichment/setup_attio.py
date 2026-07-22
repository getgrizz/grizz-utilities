"""Provision Grizz custom attributes on Attio (the `setup --crm attio` path).

Config-DRIVEN, unlike the Salesforce/HubSpot setups: it reads the slugs you
actually map in config.yaml (`attio:` for Companies, `attio_contacts:` for People)
and creates exactly those, so it can never clobber a workspace that already
renamed or pre-built its Grizz attributes.  Two consequences of Attio's design,
both handled for you:

  * Object-typed location — city/state/country collapse into ONE location
    attribute, so a mapping's dotted sub-fields (`grizz_location.locality`,
    `.region`, `.country_code`) provision a single `location`-typed attribute.
  * Native attributes are never (re)created — the `domains` dedup key on
    Companies and the `name`/`email_addresses`/`phone_numbers`/`company` fields on
    People already exist on every workspace, so setup skips them.

Setup only ever touches the ONE object you ask for (`--object company` or
`--object contacts`), so provisioning contact fields never writes to Companies.
"""

import os

import requests
import yaml

# Reuse the ONE source of truth for which slugs are phone numbers, so setup types
# them as Attio phone-number attributes (built-in cleansing/validation) exactly
# the same way the adapter E.164-normalizes them on write — company and contact
# phone fields share this set.
from grizz_enrichment.adapters.attio import _PHONE_SLUGS

_BASE_URL = "https://api.attio.com"
_TIMEOUT = 30

# Native attributes that must never be (re)created — they ship on every Attio
# workspace.  The configured native slugs (domain_field / *_field) are added to
# these per-object at runtime.
_NATIVE_COMPANY = {"name", "domains"}
_NATIVE_PEOPLE = {"name", "email_addresses", "phone_numbers", "company",
                  "primary_location", "job_title"}

# Which config section + Attio object each --object maps to.
_OBJECTS = {
    "company":  ("attio",          "companies", _NATIVE_COMPANY),
    "contacts": ("attio_contacts", "people",    _NATIVE_PEOPLE),
}


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


def _title(slug: str) -> str:
    """Human title from a slug: grizz_contact_title → 'Grizz Contact Title'."""
    return " ".join(w.capitalize() for w in slug.split("_")) or slug


def _plan_from_mapping(field_mapping: dict, skip: set[str],
                       field_types: dict | None = None) -> list[dict]:
    """Turn a config field_mapping into the attribute specs to ensure exist.

    The Attio TYPE of each attribute is CONFIG-DRIVEN — nothing is hardcoded:
      * dotted slugs (grizz_location.locality) collapse to ONE `location`-typed
        attribute per prefix (the dotted syntax *is* the type declaration);
      * any slug listed in the config's `field_types` map uses that type verbatim
        (e.g. `grizz_contact_direct_phone: phone-number` → built-in cleansing);
      * a slug in the built-in `_PHONE_SLUGS` defaults falls back to
        `phone-number` (so existing configs keep working without declaring types);
      * everything else is `text`.
    Native slugs in `skip` (the domain/`*_field` references) are dropped."""
    field_types = field_types or {}
    resolved: dict[str, str] = {}        # slug -> type, order-preserving, deduped
    for target in field_mapping.values():
        if not target or not isinstance(target, str):
            continue
        if "." in target:                # dotted sub-field → object-typed attribute
            slug, typ = target.split(".", 1)[0], "location"
        else:
            slug = target
            typ = (field_types.get(target)
                   or ("phone-number" if target in _PHONE_SLUGS else "text"))
        if slug in skip or slug in resolved:
            continue
        resolved[slug] = typ
    return [{"api_slug": s, "title": _title(s), "type": t} for s, t in resolved.items()]


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


def run_setup(dry_run: bool = False, object_: str = "company",
              config_path=None) -> dict:
    """Create the grizz_* attributes on ONE Attio object from your config.yaml.

    `object_` is 'company' (reads the `attio:` section → Companies object) or
    'contacts' (reads `attio_contacts:` → People object).  Idempotent — skips
    attributes that already exist.  Returns the standard
    {created, existed, errors, dry_run} summary used by the other setup modules.
    """
    from pathlib import Path
    key = "contacts" if object_ in ("contact", "contacts") else "company"
    section, object_slug, native = _OBJECTS[key]
    config_path = Path(config_path) if config_path else Path("config.yaml")

    attio_key = os.environ.get("ATTIO_API_KEY")
    if not attio_key:
        raise RuntimeError("ATTIO_API_KEY is not set.")
    if not config_path.exists():
        raise RuntimeError(f"Config file not found: {config_path}. Copy config.example.yaml "
                           f"to config.yaml and fill in the [{section}] section.")

    config = yaml.safe_load(config_path.read_text()) or {}
    sec = config.get(section) or {}
    field_mapping = sec.get("field_mapping") or {}
    field_types = sec.get("field_types") or {}   # config-driven Attio types
    if not field_mapping:
        raise RuntimeError(f"No '{section}: field_mapping' in {config_path} — nothing to set up. "
                           f"Copy the [{section}] section from config.example.yaml.")

    # Natives never created: the workspace-native set + the slugs config points at
    # natives (domain_field on Companies; the *_field references on People).
    skip = set(native)
    for k in ("domain_field", "name_field", "email_field", "phone_field", "company_ref"):
        if sec.get(k):
            skip.add(sec[k])

    specs = _plan_from_mapping(field_mapping, skip, field_types)

    session = _session(attio_key)
    me = session.get(f"{_BASE_URL}/v2/self", timeout=_TIMEOUT)
    if me.status_code == 200:
        info = me.json()
        print(f"Attio workspace: {info.get('workspace_name') or info.get('workspace_slug') or '?'}")
    print(f"Object: {object_slug}  (from config '{section}')")

    have = _existing_slugs(session, object_slug)
    missing = [s for s in specs if s["api_slug"] not in have]
    existed = len(specs) - len(missing)

    created = errors = would_create = 0
    print(f"[{object_slug}] {len(missing)} missing of {len(specs)} mapped attribute(s):")
    for spec in missing:
        label = f"{spec['api_slug']} ({spec['type']})"
        if dry_run:
            would_create += 1
            print(f"  would create: {label}")
            continue
        try:
            _create_attribute(session, object_slug, spec)
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
