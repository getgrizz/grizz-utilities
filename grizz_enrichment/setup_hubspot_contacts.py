"""Create all recommended Grizz custom properties on HubSpot Contact records.

Kept in lock-step with the MCP's grizz_enrichment/setup_hubspot_contacts.py so
CLI setup and the `setup_crm_contacts` MCP tool provision the SAME properties —
create-crm writes exactly these, so they must match what the portal has.
"""

import os

import requests
from rich.console import Console
from rich.table import Table

console = Console()

_BASE_URL = "https://api.hubapi.com"

GRIZZ_PROPERTIES = [
    {
        "name":          "grizz_contact_provider_id",
        "label":         "Grizz Contact Provider ID",
        "type":          "string",
        "fieldType":     "text",
        "groupName":     "grizz_contacts",
        "description":   "Grizz Contact ID. Used to dedup contacts on subsequent syncs.",
        "hasUniqueValue": True,
    },
    {
        "name":      "grizz_contact_linkedin_url",
        "label":     "Grizz Contact LinkedIn URL",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
    },
    {
        "name":      "grizz_contact_seniority",
        "label":     "Grizz Contact Seniority",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
    },
    {
        "name":      "grizz_contact_persona",
        "label":     "Grizz Contact Persona",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
        "description": "Grizz canonical persona label for this contact (e.g. CFO, Controller).",
    },
    {
        "name":      "grizz_contact_email",
        "label":     "Grizz Contact Email",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
        "description": "Email address as Grizz has it for this contact.",
    },
    {
        "name":      "grizz_contact_phone",
        "label":     "Grizz Contact Phone",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
        "description": "Phone number as Grizz has it for this contact.",
    },
    {
        "name":      "grizz_contact_title",
        "label":     "Grizz Contact Title",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
        "description": "Job title as Grizz has it for this contact.",
    },
    {
        "name":      "grizz_contact_hq_phone",
        "label":     "Grizz Contact HQ Phone",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
        "description": "Company headquarters phone number from Grizz company data.",
    },
    {
        "name":      "grizz_contact_hq_email",
        "label":     "Grizz Contact HQ Email",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
        "description": "Company headquarters email address from Grizz company data.",
    },
    {
        "name":      "grizz_contact_job_function",
        "label":     "Grizz Contact Job Function",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
        "description": "Grizz canonical department for this contact (Finance / Operations / etc.).",
    },
    {
        "name":      "grizz_contact_city",
        "label":     "Grizz Contact City",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
    },
    {
        "name":      "grizz_contact_state",
        "label":     "Grizz Contact State",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
    },
    {
        "name":      "grizz_contact_country",
        "label":     "Grizz Contact Country",
        "type":      "string",
        "fieldType": "text",
        "groupName": "grizz_contacts",
    },
    {
        "name":      "grizz_contact_skills",
        "label":     "Grizz Contact Skills",
        "type":      "string",
        "fieldType": "textarea",
        "groupName": "grizz_contacts",
        "description": "Professional skills associated with this contact.",
    },
    {
        "name":      "grizz_contact_last_sync",
        "label":     "Grizz Contact Last Sync",
        "type":      "datetime",
        "fieldType": "date",
        "groupName": "grizz_contacts",
        "description": "Timestamp when Grizz last touched this contact.",
    },
]

_GROUP_NAME  = "grizz_contacts"
_GROUP_LABEL = "Grizz Contacts"


def _ensure_property_group(session: requests.Session) -> None:
    """Create the 'grizz_contacts' property group if it doesn't already exist."""
    resp = session.get(f"{_BASE_URL}/crm/v3/properties/contacts/groups/{_GROUP_NAME}", timeout=10)
    if resp.status_code == 200:
        return

    resp = session.post(
        f"{_BASE_URL}/crm/v3/properties/contacts/groups",
        json={"name": _GROUP_NAME, "label": _GROUP_LABEL},
        timeout=10,
    )
    resp.raise_for_status()


def run_setup(dry_run: bool = False) -> None:
    """Connect to HubSpot and create all Grizz custom properties on Contact records."""

    # ── Connect ──────────────────────────────────────────────────────────────
    console.print("Connecting to HubSpot...", end=" ")
    token = os.environ.get("HUBSPOT_API_KEY")
    if not token:
        console.print("\n[red]HUBSPOT_API_KEY is not set.[/red]")
        return

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })

    resp = session.get(f"{_BASE_URL}/crm/v3/objects/contacts", params={"limit": 1}, timeout=10)
    if resp.status_code == 401:
        console.print("\n[red]HUBSPOT_API_KEY is invalid or lacks required scopes.[/red]")
        return
    try:
        resp.raise_for_status()
    except Exception as e:
        console.print(f"\n[red]Connection failed: {e}[/red]")
        return
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
                f"{_BASE_URL}/crm/v3/properties/contacts",
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
    table = Table(title="HubSpot Contact Property Setup", show_lines=False)
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

    console.print(
        "\n[dim]Note: properties have been created in the 'Grizz Contacts' group but won't appear "
        "on Contact records automatically. To add them to the record view:\n"
        "  1. Open any Contact record in HubSpot\n"
        "  2. Scroll to the bottom of the properties panel and click 'Edit properties'\n"
        "  3. Search for 'Grizz' and add the fields you want[/dim]"
    )
