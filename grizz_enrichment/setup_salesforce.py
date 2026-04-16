"""Create all recommended Grizz custom fields on the Salesforce Account object."""

import os

from simple_salesforce import Salesforce, SalesforceMalformedRequest
from rich.console import Console
from rich.table import Table

console = Console()

# All recommended Grizz fields.
# Each entry uses the Tooling API format: FullName + Metadata.
GRIZZ_FIELDS = [
    # ── Company Identity ───────────────────────────────────────────────────────
    {
        "FullName": "Account.Grizz_Company_ID__c",
        "Metadata": {"type": "Text", "label": "Grizz Company ID", "length": 255, "externalId": True, "required": False},
    },
    {
        "FullName": "Account.Grizz_Company_Name__c",
        "Metadata": {"type": "Text", "label": "Grizz Company Name", "length": 255, "required": False},
    },
    {
        "FullName": "Account.Grizz_Domain__c",
        "Metadata": {"type": "Text", "label": "Grizz Domain", "length": 255, "required": False},
    },
    {
        "FullName": "Account.Grizz_URL__c",
        "Metadata": {"type": "Url", "label": "Grizz URL", "required": False},
    },
    {
        "FullName": "Account.Grizz_LinkedIn_URL__c",
        "Metadata": {"type": "Url", "label": "Grizz LinkedIn URL", "required": False},
    },
    {
        "FullName": "Account.Grizz_Company_Description__c",
        "Metadata": {"type": "LongTextArea", "label": "Grizz Company Description", "length": 32768, "visibleLines": 4},
    },

    # ── Contact & Location ─────────────────────────────────────────────────────
    {
        "FullName": "Account.Grizz_HQ_Phone__c",
        "Metadata": {"type": "Phone", "label": "Grizz HQ Phone", "required": False},
    },
    {
        "FullName": "Account.Grizz_HQ_Email__c",
        "Metadata": {"type": "Email", "label": "Grizz HQ Email", "required": False},
    },
    {
        "FullName": "Account.Grizz_HQ_City__c",
        "Metadata": {"type": "Text", "label": "Grizz HQ City", "length": 255, "required": False},
    },
    {
        "FullName": "Account.Grizz_HQ_Region__c",
        "Metadata": {"type": "Text", "label": "Grizz HQ State / Region", "length": 255, "required": False},
    },
    {
        "FullName": "Account.Grizz_HQ_Country__c",
        "Metadata": {"type": "Text", "label": "Grizz HQ Country", "length": 255, "required": False},
    },

    # ── Firmographics ──────────────────────────────────────────────────────────
    {
        "FullName": "Account.Grizz_Employee_Range__c",
        "Metadata": {"type": "Text", "label": "Grizz Employee Range", "length": 50, "required": False},
    },
    {
        "FullName": "Account.Grizz_Revenue_Range__c",
        "Metadata": {"type": "Text", "label": "Grizz Revenue Range", "length": 50, "required": False},
    },
    {
        "FullName": "Account.Grizz_NAICS__c",
        "Metadata": {"type": "Text", "label": "Grizz NAICS", "length": 10, "required": False},
    },
    {
        "FullName": "Account.Grizz_Activity__c",
        "Metadata": {"type": "Text", "label": "Grizz Activity", "length": 255, "required": False},
    },

    # ── ERP Intelligence ───────────────────────────────────────────────────────
    {
        "FullName": "Account.Grizz_ERP__c",
        "Metadata": {"type": "Text", "label": "Grizz ERP", "length": 255, "required": False},
    },
    {
        "FullName": "Account.Grizz_ERP_Match_Type__c",
        "Metadata": {"type": "Text", "label": "Grizz ERP Match Type", "length": 255, "required": False},
    },
    {
        "FullName": "Account.Grizz_ERP_Keyword_Usage__c",
        "Metadata": {"type": "Text", "label": "Grizz ERP Keyword Usage", "length": 255, "required": False},
    },

    # ── ATS Intelligence ───────────────────────────────────────────────────────
    {
        "FullName": "Account.Grizz_ATS__c",
        "Metadata": {"type": "Text", "label": "Grizz ATS", "length": 255, "required": False},
    },
    {
        "FullName": "Account.Grizz_ATS_Match_Type__c",
        "Metadata": {"type": "Text", "label": "Grizz ATS Match Type", "length": 255, "required": False},
    },
    {
        "FullName": "Account.Grizz_ATS_Keyword_Usage__c",
        "Metadata": {"type": "Text", "label": "Grizz ATS Keyword Usage", "length": 255, "required": False},
    },

    # ── Other Technology Signals ───────────────────────────────────────────────
    {
        "FullName": "Account.Grizz_Other_Tech_Signals__c",
        "Metadata": {"type": "LongTextArea", "label": "Grizz Other Tech Signals", "length": 32768, "visibleLines": 4},
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
        "Description": "Grants read/edit access to all Grizz enrichment fields on Account.",
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


def _add_layout_section(sf, dry_run: bool = False) -> None:
    """Add a Grizz section to the Account page layout via retrieve→XML patch→deploy."""
    import io
    import time
    import xml.etree.ElementTree as ET
    import zipfile

    layout_name = "Account-Account Layout"
    console.print(f"Updating page layout '{layout_name}'...", end=" ")

    NS = "http://soap.sforce.com/2006/04/metadata"
    ET.register_namespace("", NS)

    try:
        if dry_run:
            console.print("[cyan]would add section.[/cyan]")
            return

        # ── Retrieve ─────────────────────────────────────────────────────────
        async_id, state = sf.mdapi.retrieve(
            "",
            unpackaged={"Layout": [layout_name]},
        )
        if not async_id:
            console.print("[yellow]skipped (retrieve did not return an ID)[/yellow]")
            return

        for _ in range(30):
            time.sleep(2)
            state, error_msg, _ = sf.mdapi.check_retrieve_status(async_id)
            if state in ("Succeeded", "Failed", "Canceled"):
                break

        if state != "Succeeded":
            console.print(f"[yellow]skipped (retrieve {state}: {error_msg})[/yellow]")
            return

        # ── Unpack ZIP ───────────────────────────────────────────────────────
        _state, _err, _msgs, zip_bytes = sf.mdapi.retrieve_zip(async_id)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            layout_path = next((n for n in zf.namelist() if n.endswith(".layout")), None)
            if not layout_path:
                console.print("[yellow]skipped (no .layout file in zip)[/yellow]")
                return
            layout_xml = zf.read(layout_path)
            pkg_xml = zf.read("package.xml")

        # ── Parse + patch layout XML ─────────────────────────────────────────
        root = ET.fromstring(layout_xml)

        expected_fields = {f["FullName"].split(".")[-1] for f in GRIZZ_FIELDS}

        for section in root.findall(f"{{{NS}}}layoutSections"):
            label_el = section.find(f"{{{NS}}}label")
            if label_el is not None and label_el.text == "Grizz":
                # Check whether the section actually contains all expected fields
                present = {
                    item.find(f"{{{NS}}}field").text
                    for item in section.findall(f".//{{{NS}}}layoutItems")
                    if item.find(f"{{{NS}}}field") is not None
                }
                if expected_fields.issubset(present):
                    console.print("[dim]already exists.[/dim]")
                    return
                # Section exists but is stale/incomplete — remove and re-add
                root.remove(section)
                break

        field_names = [f["FullName"].split(".")[-1] for f in GRIZZ_FIELDS]
        mid = (len(field_names) + 1) // 2
        col1 = field_names[:mid]
        col2 = field_names[mid:]

        def make_column(fields):
            col_el = ET.Element(f"{{{NS}}}layoutColumns")
            for field in fields:
                item_el = ET.SubElement(col_el, f"{{{NS}}}layoutItems")
                ET.SubElement(item_el, f"{{{NS}}}behavior").text = "Edit"
                ET.SubElement(item_el, f"{{{NS}}}field").text = field
            return col_el

        section_el = ET.Element(f"{{{NS}}}layoutSections")
        ET.SubElement(section_el, f"{{{NS}}}customLabel").text = "true"
        ET.SubElement(section_el, f"{{{NS}}}detailHeading").text = "true"
        ET.SubElement(section_el, f"{{{NS}}}editHeading").text = "true"
        ET.SubElement(section_el, f"{{{NS}}}label").text = "Grizz"
        ET.SubElement(section_el, f"{{{NS}}}style").text = "TwoColumnsTopToBottom"
        section_el.append(make_column(col1))
        section_el.append(make_column(col2))

        # Insert adjacent to existing layoutSections (schema requires contiguous grouping)
        children = list(root)
        last_section_idx = max(
            (i for i, c in enumerate(children) if c.tag == f"{{{NS}}}layoutSections"),
            default=-1,
        )
        if last_section_idx >= 0:
            root.insert(last_section_idx + 1, section_el)
        else:
            root.append(section_el)

        new_layout_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode", xml_declaration=False)
        )

        # ── Re-pack + deploy ─────────────────────────────────────────────────
        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.writestr(layout_path, new_layout_xml.encode("utf-8"))
            zout.writestr("package.xml", pkg_xml)
        out_buf.seek(0)

        is_sandbox = os.environ.get("SALESFORCE_DOMAIN", "login") == "test"
        deploy_id, _state = sf.mdapi.deploy(out_buf, sandbox=is_sandbox, ignoreWarnings=True)
        if not deploy_id:
            console.print("[yellow]skipped (deploy did not return an ID)[/yellow]")
            return

        for _ in range(30):
            time.sleep(2)
            state, _detail, deploy_detail, _ = sf.mdapi.check_deploy_status(deploy_id)
            if state not in ("Pending", "InProgress", "Queued"):
                break

        if state == "Succeeded":
            console.print("[green]done.[/green]")
        else:
            errors = (deploy_detail or {}).get("errors", [])
            msg = errors[0].get("message", state) if errors else state
            console.print(f"[yellow]skipped (deploy {state}: {msg})[/yellow]")

    except Exception as e:
        console.print(f"[yellow]skipped ({e})[/yellow]")


def run_setup(dry_run: bool = False) -> None:
    """Connect to Salesforce and create all Grizz custom fields on Account."""

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
    table = Table(title="Salesforce Field Setup", show_lines=False)
    table.add_column("Label")
    table.add_column("API Name", style="dim")
    table.add_column("Result")

    outcome_colors  = {"created": "green", "exists": "dim", "error": "red", "dry_run": "cyan"}
    outcome_labels  = {"created": "created", "exists": "already exists", "error": "error", "dry_run": "would create"}

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

    _add_layout_section(sf, dry_run=dry_run)

    console.print(
        "\n[dim]Note: fields have been added to the classic Account page layout. "
        "To show them on your Lightning record page, go to "
        "Setup → Object Manager → Account → Lightning Record Pages, "
        "edit your Account page, and drag the Grizz fields into a new section.[/dim]"
    )
