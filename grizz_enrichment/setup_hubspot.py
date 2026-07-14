"""Create all recommended Grizz custom properties on HubSpot Company records."""

import os

import requests
from rich.console import Console
from rich.table import Table

console = Console()

_BASE_URL = "https://api.hubapi.com"

# All recommended Grizz custom properties.
# HubSpot property types: string, number, date, datetime, enumeration, bool
GRIZZ_PROPERTIES = [
    # ── Company Identity ───────────────────────────────────────────────────────
    {
        "name":        "grizz_company_id",
        "label":       "Grizz Company ID",
        "type":        "string",
        "fieldType":   "text",
        "groupName":   "grizz",
        # NOT hasUniqueValue: client CRMs hold duplicate company records that
        # legitimately resolve to one Grizz company, so many records share a
        # grizz_company_id. HubSpot enforces hasUniqueValue at write time, so a
        # unique constraint 409s every write after the first in a duplicate
        # cluster — and because record writes are atomic, that also blocks the
        # record's other grizz_* fields, breaking enrichment of exactly the
        # duplicate population we care about. Uniqueness isn't needed to match
        # (search works regardless).
        "description": "Grizz company identifier.",
    },
    {
        "name":        "grizz_gid",
        "label":       "Grizz GID",
        "type":        "string",
        "fieldType":   "text",
        "groupName":   "grizz",
        "description": "Grizz canonical company identifier (GC + sha256 of domain). Stable cross-system key.",
    },
    {
        "name":      "grizz_company_name",
        "label":     "Grizz Company Name",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_domain",
        "label":     "Grizz Domain",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_url",
        "label":     "Grizz URL",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_linkedin_url",
        "label":     "Grizz LinkedIn URL",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_company_description",
        "label":     "Grizz Company Description",
        "type":      "string",
        "fieldType": "textarea",
        "groupName": "grizz",
    },

    # ── Contact & Location ─────────────────────────────────────────────────────
    {
        "name":      "grizz_hq_phone",
        "label":     "Grizz HQ Phone",
        "type":      "string",
        "fieldType": "phonenumber",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_hq_email",
        "label":     "Grizz HQ Email",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_hq_city",
        "label":     "Grizz HQ City",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_hq_region",
        "label":     "Grizz HQ State / Region",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_hq_country",
        "label":     "Grizz HQ Country",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },

    # ── Firmographics ──────────────────────────────────────────────────────────
    {
        "name":      "grizz_employee_range",
        "label":     "Grizz Employee Range",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_revenue_range",
        "label":     "Grizz Revenue Range",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_naics",
        "label":     "Grizz NAICS",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_activity",
        "label":     "Grizz Activity",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },

    # ── ERP Intelligence ───────────────────────────────────────────────────────
    {
        "name":      "grizz_erp",
        "label":     "Grizz ERP",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_erp_match_type",
        "label":     "Grizz ERP Match Type",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_erp_keyword_usage",
        "label":     "Grizz ERP Keyword Usage",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },

    # ── ATS Intelligence ───────────────────────────────────────────────────────
    {
        "name":      "grizz_ats",
        "label":     "Grizz ATS",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_ats_match_type",
        "label":     "Grizz ATS Match Type",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },
    {
        "name":      "grizz_ats_keyword_usage",
        "label":     "Grizz ATS Keyword Usage",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz",
    },

    # ── Other Technology Signals ───────────────────────────────────────────────
    {
        "name":      "grizz_other_tech_signals",
        "label":     "Grizz Other Tech Signals",
        "type":      "string",
        "fieldType": "textarea",
        "groupName": "grizz",
    },

    # ── Sync timestamp ─────────────────────────────────────────────────────────
    # Written by every Grizz CRM-sync operation.  Customers + Grizz's
    # dashboard use this to distinguish "never touched" from "Grizz looked
    # and had nothing to add" from "data is stale; time to refresh."
    {
        "name":      "grizz_last_sync",
        "label":     "Grizz Last Sync",
        "type":      "datetime",
        "fieldType": "date",
        "groupName": "grizz",
    },
]

_GROUP_NAME  = "grizz"
_GROUP_LABEL = "Grizz"


def _ensure_property_group(session: requests.Session) -> None:
    """Create the 'grizz' property group if it doesn't already exist."""
    resp = session.get(f"{_BASE_URL}/crm/v3/properties/companies/groups/{_GROUP_NAME}", timeout=10)
    if resp.status_code == 200:
        return  # already exists

    resp = session.post(
        f"{_BASE_URL}/crm/v3/properties/companies/groups",
        json={"name": _GROUP_NAME, "label": _GROUP_LABEL},
        timeout=10,
    )
    resp.raise_for_status()


def run_setup(dry_run: bool = False) -> dict:
    """Connect to HubSpot and create all Grizz custom properties on Company records.

    Returns a structured summary so callers (MCP, scripts) can tell the user
    exactly what changed instead of a generic "complete" message:

        {
          "created":   ["grizz_last_sync", ...],   # newly created this run
          "existed":   ["grizz_company_id", ...],  # already in CRM, skipped
          "errors":    [{"field": "...", "error": "..."}],
          "dry_run":   bool,
        }
    """
    empty: dict = {"created": [], "existed": [], "errors": [], "dry_run": dry_run}

    # ── Connect ──────────────────────────────────────────────────────────────
    console.print("Connecting to HubSpot...", end=" ")
    token = os.environ.get("HUBSPOT_API_KEY")
    if not token:
        console.print("\n[red]HUBSPOT_API_KEY is not set.[/red]")
        return {**empty, "errors": [{"field": "", "error": "HUBSPOT_API_KEY not set"}]}

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })

    resp = session.get(f"{_BASE_URL}/crm/v3/objects/companies", params={"limit": 1}, timeout=10)
    if resp.status_code == 401:
        console.print("\n[red]HUBSPOT_API_KEY is invalid or lacks required scopes.[/red]")
        return {**empty, "errors": [{"field": "", "error": "auth failed (401)"}]}
    try:
        resp.raise_for_status()
    except Exception as e:
        console.print(f"\n[red]Connection failed: {e}[/red]")
        return {**empty, "errors": [{"field": "", "error": f"connection failed: {e}"}]}
    console.print("[green]connected.[/green]")

    if dry_run:
        console.print("[yellow]Dry run — no properties will be created.[/yellow]\n")
    else:
        console.print()

    # ── Ensure property group exists ──────────────────────────────────────────
    if not dry_run:
        try:
            console.print("Setting up property group...", end=" ")
            _ensure_property_group(session)
            console.print("[green]done.[/green]")
        except Exception as e:
            console.print(f"[yellow]skipped ({e})[/yellow]")

    # ── Create properties ────────────────────────────────────────────────────
    results = []  # (label, api_name, outcome, detail)

    for prop in GRIZZ_PROPERTIES:
        label    = prop["label"]
        api_name = prop["name"]

        if dry_run:
            results.append((label, api_name, "dry_run", ""))
            continue

        try:
            resp = session.post(
                f"{_BASE_URL}/crm/v3/properties/companies",
                json=prop,
                timeout=10,
            )
            if resp.status_code == 409:
                results.append((label, api_name, "exists", ""))
            else:
                resp.raise_for_status()
                results.append((label, api_name, "created", ""))
        except Exception as e:
            results.append((label, api_name, "error", str(e)))

    # ── Summary ──────────────────────────────────────────────────────────────
    table = Table(title="HubSpot Property Setup", show_lines=False)
    table.add_column("Label")
    table.add_column("API Name", style="dim")
    table.add_column("Result")

    outcome_colors = {"created": "green", "exists": "dim", "error": "red", "dry_run": "cyan"}
    outcome_labels = {"created": "created", "exists": "already exists", "error": "error", "dry_run": "would create"}

    for label, api_name, outcome, detail in results:
        color = outcome_colors.get(outcome, "white")
        text  = outcome_labels.get(outcome, outcome)
        if detail:
            text += f": {detail}"
        table.add_row(label, api_name, f"[{color}]{text}[/{color}]")

    console.print(table)

    created = sum(1 for _, _, o, _ in results if o == "created")
    existed = sum(1 for _, _, o, _ in results if o == "exists")
    errors  = sum(1 for _, _, o, _ in results if o == "error")

    if dry_run:
        console.print(f"\n[bold]Would create {len(results)} property/properties.[/bold]")
    else:
        parts = []
        if created: parts.append(f"[green]{created} created[/green]")
        if existed: parts.append(f"{existed} already existed")
        if errors:  parts.append(f"[red]{errors} failed[/red]")
        console.print(f"\n[bold]Done:[/bold] {', '.join(parts)}.")

    return {
        "created":   [api for _, api, o, _ in results if o == "created"],
        "existed":   [api for _, api, o, _ in results if o == "exists"],
        "errors":    [{"field": api, "error": d} for _, api, o, d in results if o == "error"],
        "dry_run":   dry_run,
    }

    console.print(
        "\n[dim]Note: properties have been created in the 'Grizz' group but won't appear "
        "on Company records automatically. To add them to the record view:\n"
        "  1. Open any Company record in HubSpot\n"
        "  2. Scroll to the bottom of the properties panel and click 'Edit properties'\n"
        "  3. Search for 'Grizz' and add the fields you want\n"
        "On Enterprise, you can set a default layout for all users at:\n"
        "  Settings → Data Management → Record Customization[/dim]"
    )
