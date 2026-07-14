"""Create all recommended Grizz custom fields on the Salesforce Contact object.

Kept in lock-step with the MCP's grizz_enrichment/setup_salesforce_contacts.py so
CLI setup and the `setup_crm_contacts` MCP tool provision the SAME fields.
"""

import os

from simple_salesforce import Salesforce, SalesforceMalformedRequest
from rich.console import Console
from rich.table import Table

console = Console()

GRIZZ_FIELDS = [
    {
        "FullName": "Contact.Grizz_Contact_Provider_ID__c",
        "Metadata": {"type": "Text", "label": "Grizz Contact Provider ID", "length": 255, "externalId": True, "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_LinkedIn_URL__c",
        "Metadata": {"type": "Url", "label": "Grizz Contact LinkedIn URL", "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Seniority__c",
        "Metadata": {"type": "Text", "label": "Grizz Contact Seniority", "length": 100, "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Persona__c",
        "Metadata": {"type": "Text", "label": "Grizz Contact Persona", "length": 100, "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Email__c",
        "Metadata": {"type": "Email", "label": "Grizz Contact Email", "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Phone__c",
        "Metadata": {"type": "Phone", "label": "Grizz Contact Phone", "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Title__c",
        "Metadata": {"type": "Text", "label": "Grizz Contact Title", "length": 255, "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_HQ_Phone__c",
        "Metadata": {"type": "Phone", "label": "Grizz Contact HQ Phone", "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_HQ_Email__c",
        "Metadata": {"type": "Email", "label": "Grizz Contact HQ Email", "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Job_Function__c",
        "Metadata": {"type": "Text", "label": "Grizz Contact Job Function", "length": 255, "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_City__c",
        "Metadata": {"type": "Text", "label": "Grizz Contact City", "length": 100, "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_State__c",
        "Metadata": {"type": "Text", "label": "Grizz Contact State", "length": 100, "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Country__c",
        "Metadata": {"type": "Text", "label": "Grizz Contact Country", "length": 100, "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Skills__c",
        "Metadata": {"type": "TextArea", "label": "Grizz Contact Skills", "required": False},
    },
    {
        "FullName": "Contact.Grizz_Contact_Last_Sync__c",
        "Metadata": {"type": "DateTime", "label": "Grizz Contact Last Sync"},
    },
]

PERMISSION_SET_NAME = "Grizz_Enrichment"
PERMISSION_SET_LABEL = "Grizz Enrichment"


def _ensure_permission_set(sf) -> str:
    """Return the Id of the Grizz Enrichment permission set, creating it if needed."""
    result = sf.query(
        f"SELECT Id FROM PermissionSet WHERE Name = '{PERMISSION_SET_NAME}' LIMIT 1"
    )
    if result["records"]:
        return result["records"][0]["Id"]

    created = sf.PermissionSet.create({
        "Name": PERMISSION_SET_NAME,
        "Label": PERMISSION_SET_LABEL,
        "Description": "Grants read/edit access to all Grizz enrichment fields on Account and Contact.",
    })
    return created["id"]


def _assign_permission_set(sf, pset_id: str) -> None:
    """Assign the permission set to all active System Administrator users."""
    profile = sf.query("SELECT Id FROM Profile WHERE Name = 'System Administrator' LIMIT 1")
    if not profile["records"]:
        return
    profile_id = profile["records"][0]["Id"]

    users = sf.query(
        f"SELECT Id FROM User WHERE IsActive = true AND ProfileId = '{profile_id}'"
    )
    for user in users["records"]:
        user_id = user["Id"]
        existing = sf.query(
            f"SELECT Id FROM PermissionSetAssignment "
            f"WHERE PermissionSetId = '{pset_id}' AND AssigneeId = '{user_id}' LIMIT 1"
        )
        if not existing["records"]:
            sf.PermissionSetAssignment.create({
                "PermissionSetId": pset_id,
                "AssigneeId": user_id,
            })


def _set_fls(sf, full_name: str, pset_id: str) -> None:
    """Grant read+edit on a field via the Grizz permission set."""
    sobject_type, field_name = full_name.split(".")
    existing = sf.query(
        f"SELECT Id FROM FieldPermissions "
        f"WHERE ParentId = '{pset_id}' AND Field = '{full_name}' LIMIT 1"
    )
    if existing["records"]:
        sf.FieldPermissions.update(existing["records"][0]["Id"], {
            "PermissionsRead": True,
            "PermissionsEdit": True,
        })
    else:
        sf.FieldPermissions.create({
            "ParentId": pset_id,
            "SobjectType": sobject_type,
            "Field": full_name,
            "PermissionsRead": True,
            "PermissionsEdit": True,
        })


def run_setup(dry_run: bool = False) -> None:
    """Connect to Salesforce and create all Grizz custom fields on Contact."""

    # ── Connect ──────────────────────────────────────────────────────────────
    console.print("Connecting to Salesforce...", end=" ")
    try:
        session_id = os.environ.get("SALESFORCE_SESSION_ID")
        instance_url = os.environ.get("SALESFORCE_INSTANCE_URL")

        if session_id and instance_url:
            sf = Salesforce(session_id=session_id, instance_url=instance_url)
        else:
            sf = Salesforce(
                username=os.environ["SALESFORCE_USERNAME"],
                password=os.environ["SALESFORCE_PASSWORD"],
                security_token=os.environ["SALESFORCE_SECURITY_TOKEN"],
                domain=os.environ.get("SALESFORCE_DOMAIN", "login"),
            )
    except KeyError as e:
        console.print(f"\n[red]Missing environment variable: {e}[/red]")
        return
    except Exception as e:
        console.print(f"\n[red]Connection failed: {e}[/red]")
        return
    console.print("[green]connected.[/green]")

    if dry_run:
        console.print("[yellow]Dry run — no fields will be created.[/yellow]\n")
    else:
        console.print()

    # ── Ensure permission set exists ──────────────────────────────────────────
    pset_id = None
    if not dry_run:
        try:
            console.print("Setting up permission set...", end=" ")
            pset_id = _ensure_permission_set(sf)
            _assign_permission_set(sf, pset_id)
            console.print("[green]done.[/green]")
        except Exception as e:
            console.print(f"[yellow]skipped ({e})[/yellow]")
            pset_id = None

    # ── Create fields ────────────────────────────────────────────────────────
    results = []  # (label, api_name, outcome, detail)

    for field in GRIZZ_FIELDS:
        full_name = field["FullName"]
        api_name = full_name.split(".")[-1]
        label = field["Metadata"]["label"]

        if dry_run:
            results.append((label, api_name, "dry_run", ""))
            continue

        try:
            sf.toolingexecute("sobjects/CustomField", method="POST", data=field)
            if pset_id:
                _set_fls(sf, full_name, pset_id)
            results.append((label, api_name, "created", ""))
        except SalesforceMalformedRequest as e:
            msg = str(e)
            if "already exists" in msg.lower() or "duplicate" in msg.lower():
                if pset_id:
                    _set_fls(sf, full_name, pset_id)
                results.append((label, api_name, "exists", ""))
            else:
                results.append((label, api_name, "error", msg))
        except Exception as e:
            results.append((label, api_name, "error", str(e)))

    # ── Summary ──────────────────────────────────────────────────────────────
    table = Table(title="Salesforce Contact Field Setup", show_lines=False)
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
        console.print(f"\n[bold]Would create {len(results)} field(s).[/bold]")
    else:
        parts = []
        if created: parts.append(f"[green]{created} created[/green]")
        if existed: parts.append(f"{existed} already existed")
        if errors:  parts.append(f"[red]{errors} failed[/red]")
        console.print(f"\n[bold]Done:[/bold] {', '.join(parts)}.")

    console.print(
        "\n[dim]Note: fields have been added to the Contact object. "
        "To show them on Contact records, go to "
        "Setup → Object Manager → Contact → Page Layouts, "
        "edit your layout, and drag the Grizz fields into a new section.[/dim]"
    )
