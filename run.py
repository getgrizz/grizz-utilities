#!/usr/bin/env python3
"""
Grizz CRM Enrichment Tool

Interactive usage:
    python run.py

Headless usage:
    python run.py enrich --crm salesforce --input accounts.csv
    python run.py enrich --crm salesforce --input accounts.csv --dry-run
"""

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import questionary
import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from grizz_enrichment import __version__, grizz_client
from grizz_enrichment.adapters import ADAPTERS
from grizz_enrichment.audience_client import fetch_audience, submit as submit_audience
from grizz_enrichment.grizz_client import enrich as grizz_enrich
from grizz_enrichment.mapper import apply_mapping
from grizz_enrichment.setup_salesforce import run_setup as run_setup_salesforce
from grizz_enrichment.setup_hubspot import run_setup as run_setup_hubspot
from grizz_enrichment.setup_attio import run_setup as run_setup_attio
from grizz_enrichment.setup_salesforce_contacts import run_setup as run_setup_salesforce_contacts
from grizz_enrichment.setup_hubspot_contacts import run_setup as run_setup_hubspot_contacts

load_dotenv(override=True)

app = typer.Typer(
    help="Enrich your CRM with Grizz construction company data.",
    add_completion=False,
    no_args_is_help=False,
)
console = Console()

people_app = typer.Typer(
    help="People discovery + contact enrichment flows (contacts, not companies).",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(people_app, name="people")


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        console.print(f"[red]Config file not found: {config_path}[/red]")
        console.print(
            "Copy [bold]config.example.yaml[/bold] to [bold]config.yaml[/bold] "
            "and fill in your field mappings."
        )
        raise typer.Exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


def read_account_ids(input_file: Path) -> list[str]:
    """Read a CSV and return a list of account IDs from the first 'Id' column found."""
    with open(input_file, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        console.print("[red]CSV file is empty.[/red]")
        raise typer.Exit(1)

    id_col = next((k for k in rows[0].keys() if k.strip().lower() in ("id", "account id")), None)
    if not id_col:
        console.print("[red]CSV must contain an 'Id' or 'Account ID' column.[/red]")
        raise typer.Exit(1)

    ids = [row[id_col].strip() for row in rows if row[id_col].strip()]
    if not ids:
        console.print("[red]No account IDs found in CSV.[/red]")
        raise typer.Exit(1)

    return ids


# ── Core enrichment logic ──────────────────────────────────────────────────────

def run_enrich(crm: str, input_file: Path, config_path: Path, dry_run: bool) -> None:
    """Fetch domains from CRM, enrich via Grizz, write results back."""

    # ── Config ──────────────────────────────────────────────────────────────
    config = load_config(config_path)
    crm_config = config.get(crm)
    if not crm_config:
        console.print(f"[red]No '{crm}' section found in {config_path}.[/red]")
        raise typer.Exit(1)

    domain_field: str = crm_config["domain_field"]
    field_mapping: dict = crm_config["field_mapping"]

    # ── API key ──────────────────────────────────────────────────────────────
    grizz_api_key = os.environ.get("GRIZZ_API_KEY")
    if not grizz_api_key:
        console.print(
            "[red]GRIZZ_API_KEY is not set.[/red] "
            "Add it to your [bold].env[/bold] file."
        )
        raise typer.Exit(1)

    # ── CRM connection ───────────────────────────────────────────────────────
    adapter = ADAPTERS[crm]()
    console.print(f"Connecting to {crm.title()}...", end=" ")
    try:
        adapter.connect()
    except KeyError as e:
        console.print(
            f"\n[red]Missing environment variable: {e}[/red] "
            f"Check your [bold].env[/bold] file."
        )
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"\n[red]Connection failed: {e}[/red]")
        raise typer.Exit(1)
    console.print("[green]connected.[/green]")

    # ── Input ────────────────────────────────────────────────────────────────
    account_ids = read_account_ids(input_file)
    console.print(f"Loaded [bold]{len(account_ids)}[/bold] account(s) from {input_file}.")

    if dry_run:
        console.print("[yellow]Dry run mode — CRM will not be updated.[/yellow]")

    console.print()

    # ── Process ──────────────────────────────────────────────────────────────
    summary: list[tuple[str, str, str]] = []  # (account_id, outcome, detail)

    for i, account_id in enumerate(account_ids, 1):
        prefix = f"[{i}/{len(account_ids)}] {account_id}"
        console.print(f"{prefix}")

        # 1. Get domain from CRM
        try:
            domain = adapter.get_domain(account_id, domain_field)
        except Exception as e:
            console.print(f"  [red]Error fetching domain: {e}[/red]")
            summary.append((account_id, "error", str(e)))
            continue

        if not domain:
            console.print(f"  [yellow]No domain on record — skipped.[/yellow]")
            summary.append((account_id, "skipped", "no domain"))
            continue

        console.print(f"  Domain: {domain}")

        # 2. Enrich via Grizz
        try:
            poll_count = [0]

            def on_status(status: str) -> None:
                poll_count[0] += 1
                console.print(f"  Polling ({poll_count[0]})... {status}", end="\r")

            grizz_data = grizz_enrich(grizz_api_key, domain, on_status=on_status)
            console.print()  # end the polling line
        except Exception as e:
            console.print(f"\n  [red]Grizz error: {e}[/red]")
            summary.append((account_id, "error", str(e)))
            continue

        if grizz_data is None:
            console.print(f"  [yellow]No data available for this domain.[/yellow]")
            summary.append((account_id, "no_data", domain))
            continue

        # 3. Map fields
        updates = apply_mapping(grizz_data, field_mapping)
        if not updates:
            console.print(f"  [yellow]No mapped fields returned — nothing to update.[/yellow]")
            summary.append((account_id, "no_updates", domain))
            continue

        console.print(f"  Fields: {', '.join(updates.keys())}")

        # 4. Update CRM (or preview)
        if dry_run:
            console.print(f"  [dim]Would update: {updates}[/dim]")
            summary.append((account_id, "dry_run", domain))
        else:
            try:
                adapter.update_record(account_id, updates)
                console.print(f"  [green]Updated.[/green]")
                summary.append((account_id, "success", domain))
            except Exception as e:
                console.print(f"  [red]Update failed: {e}[/red]")
                summary.append((account_id, "error", str(e)))

    # ── Summary ──────────────────────────────────────────────────────────────
    console.print()
    table = Table(title="Summary", show_lines=False)
    table.add_column("Account ID", style="dim")
    table.add_column("Result")
    table.add_column("Detail")

    outcome_colors = {
        "success": "green",
        "dry_run": "cyan",
        "no_data": "yellow",
        "skipped": "yellow",
        "no_updates": "yellow",
        "error": "red",
    }

    for account_id, outcome, detail in summary:
        color = outcome_colors.get(outcome, "white")
        table.add_row(account_id, f"[{color}]{outcome}[/{color}]", detail)

    console.print(table)

    success_count = sum(1 for _, o, _ in summary if o in ("success", "dry_run"))
    console.print(
        f"\n[bold]Done:[/bold] {success_count}/{len(account_ids)} "
        f"{'previewed' if dry_run else 'updated'}."
    )


# ── Audience → CRM logic ───────────────────────────────────────────────────────

def _run_update_batches(
    adapter,
    records: list[dict],
    batch_size: int,
    console: Console,
    interactive: bool = True,
) -> tuple[int, list[dict]]:
    """Send records to the CRM in batches. Returns (updated_count, failed_records).

    On completion, if any batches failed the user is prompted to retry once
    (or, when non-interactive, the failed records are retried once automatically).
    """
    updated = 0
    failed_records: list[dict] = []
    total = len(records)

    for batch_start in range(0, total, batch_size):
        batch = records[batch_start:batch_start + batch_size]
        batch_end = min(batch_start + batch_size, total)
        console.print(f"  Updating records {batch_start + 1}–{batch_end} of {total}...", end=" ")
        try:
            results = adapter.update_accounts(batch)
            batch_ok = sum(1 for r in results if r.get("success"))
            batch_fail = len(results) - batch_ok
            updated += batch_ok
            if batch_fail:
                console.print(f"[green]{batch_ok} updated[/green], [red]{batch_fail} failed.[/red]")
                for r in results:
                    if not r.get("success"):
                        console.print(f"    [red]{r.get('errors')}[/red]")
                failed_records.extend(batch)
            else:
                console.print(f"[green]{batch_ok} updated.[/green]")
        except Exception as e:
            console.print(f"[red]batch failed: {e}[/red]")
            failed_records.extend(batch)

    if failed_records:
        console.print(
            f"\n  [yellow]{len(failed_records)} record(s) failed.[/yellow] "
            f"This is usually a temporary rate-limit issue."
        )
        if interactive:
            retry = questionary.confirm(
                f"Retry the {len(failed_records)} failed record(s) now?",
                default=True,
            ).ask()
        else:
            retry = True
            console.print("  [dim]Non-interactive: retrying failed record(s) once automatically.[/dim]")
        if retry:
            console.print(f"  Retrying {len(failed_records)} record(s)...")
            retry_updated = 0
            still_failed: list[dict] = []
            retry_total = len(failed_records)
            for batch_start in range(0, retry_total, batch_size):
                batch = failed_records[batch_start:batch_start + batch_size]
                batch_end = min(batch_start + batch_size, retry_total)
                console.print(f"  Retrying records {batch_start + 1}–{batch_end} of {retry_total}...", end=" ")
                try:
                    results = adapter.update_accounts(batch)
                    batch_ok = sum(1 for r in results if r.get("success"))
                    retry_updated += batch_ok
                    if batch_ok < len(batch):
                        console.print(f"[green]{batch_ok} updated[/green], [red]{len(batch) - batch_ok} still failed.[/red]")
                        still_failed.extend(batch)
                    else:
                        console.print(f"[green]{batch_ok} updated.[/green]")
                except Exception as e:
                    console.print(f"[red]retry batch failed: {e}[/red]")
                    still_failed.extend(batch)
            updated += retry_updated
            if still_failed:
                console.print(f"  [red]{len(still_failed)} record(s) could not be updated after retry.[/red]")
            failed_records = still_failed

    return updated, failed_records

_AUDIENCE_CSV_COLUMNS = [
    "grizz_id", "gid_company", "grizz_url", "company_name", "domain", "linkedin_url",
    "phone", "email",
    "hq_city", "hq_region", "hq_country",
    "employee_range", "revenue_range",
    "naics", "grizz_activity",
    "erp_tech_stack", "erp_match_type", "erp_keyword_usage",
    "ats_tech_stack", "ats_match_type", "ats_keyword_usage",
    "other_tech_signals",
]


def _first_naics(value: str | None) -> str | None:
    """Return just the first 6-character NAICS code from a potentially comma-separated string."""
    if not value:
        return None
    return value.split(",")[0].strip()[:6] or None


def _save_audience_csv(audience_id: str, companies: list[dict]) -> Path:
    """Write audience results to csv_out/Audience <id>.csv. Returns the path."""
    out_dir = Path("csv_out")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"Audience {audience_id}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_AUDIENCE_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(companies)
    return out_path


def _read_gids(path: Path) -> list[str]:
    """Read gid_company values from a file: a CSV with a gid_company/gid column,
    or a plain one-per-line list.  Grizz company gids start with 'GC'."""
    import csv
    rows = [r for r in csv.reader(path.read_text().splitlines()) if r and r[0].strip()]
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    col, start = 0, 0
    for name in ("gid_company", "gid"):
        if name in header:
            col, start = header.index(name), 1
            break
    else:
        # No recognized header — skip a stray header row if the first cell isn't a gid.
        if not rows[0][col].strip().upper().startswith("GC"):
            start = 1
    return [r[col].strip() for r in rows[start:] if len(r) > col and r[col].strip()]


def _companies_from_gids(api_key: str, gids: list[str]) -> list[dict]:
    """Resolve gid_company values to Grizz company dicts via the read-only,
    no-credit lookup-batch endpoint, normalized to the audience-result shape
    that the downstream sync expects (hq_phone/hq_email -> phone/email)."""
    companies: list[dict] = []
    unmatched = 0
    for i in range(0, len(gids), 5000):                      # lookup-batch caps at 5000
        batch = gids[i:i + 5000]
        for m in grizz_client.lookup_batch(api_key, [{"gid_company": g} for g in batch]):
            company = m.get("company")
            if not m.get("matched") or not company:
                unmatched += 1
                continue
            c = dict(company)
            c["phone"] = c.pop("hq_phone", "") or ""
            c["email"] = c.pop("hq_email", "") or ""
            companies.append(c)
    if unmatched:
        console.print(f"  [yellow]{unmatched} compan(ies) returned no data — skipped.[/yellow]")
    return companies


def run_audience(
    crm: str,
    config_path: Path,
    dry_run: bool,
    audience_id: str | None = None,
    prompt: str | None = None,
    batch_size: int = 200,
    gids: list[str] | None = None,
    assume_yes: bool = False,
) -> None:
    """Push a Grizz company list into the CRM.

    Source of the list is one of: an audience id, a prompt (builds a new
    audience), or an explicit list of gid_company values (`gids`) — e.g. a
    filtered selection handed off from the Grizz MCP.  Steps 2-5 (match,
    update, create, report) are identical regardless of source.

    When non-interactive (`assume_yes`, or no TTY — e.g. an agent/MCP hand-off or
    CI), unmatched companies are created without prompting and failed batches are
    retried once automatically, so the run never blocks on a missing terminal.
    """
    non_interactive = assume_yes or not sys.stdin.isatty()

    # ── Config ──────────────────────────────────────────────────────────────
    config = load_config(config_path)
    crm_config = config.get(crm)
    if not crm_config:
        console.print(f"[red]No '{crm}' section found in {config_path}.[/red]")
        raise typer.Exit(1)

    field_mapping: dict = crm_config["field_mapping"]

    # ── API key ──────────────────────────────────────────────────────────────
    grizz_api_key = os.environ.get("GRIZZ_API_KEY")
    if not grizz_api_key:
        console.print(
            "[red]GRIZZ_API_KEY is not set.[/red] "
            "Add it to your [bold].env[/bold] file."
        )
        raise typer.Exit(1)

    # ── Resolve the company list (explicit gids, or an audience) ─────────────
    if gids:
        console.print(f"Resolving {len(gids)} companies from Grizz...", end=" ")
        try:
            companies = _companies_from_gids(grizz_api_key, gids)
        except Exception as e:
            console.print(f"\n[red]Error resolving companies: {e}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]{len(companies)} resolved.[/green]")
        csv_label = "gid-selection"
    else:
        # Submit prompt if no audience_id given
        if not audience_id:
            if not prompt:
                console.print("[red]Provide --audience-id, --prompt, or --gids.[/red]")
                raise typer.Exit(1)
            console.print(f"Submitting audience request...", end=" ")
            try:
                audience_id = submit_audience(grizz_api_key, prompt)
            except Exception as e:
                console.print(f"\n[red]Failed to submit audience: {e}[/red]")
                raise typer.Exit(1)
            console.print(f"[green]submitted.[/green] ID: [bold]{audience_id}[/bold]")

        console.print(f"Fetching audience [bold]{audience_id}[/bold]...", end=" ")
        try:
            poll_count = [0]

            def on_status(s: str) -> None:
                poll_count[0] += 1
                console.print(f"  Polling ({poll_count[0]})... {s}", end="\r")

            companies = fetch_audience(grizz_api_key, audience_id, on_status=on_status)
            console.print(f"[green]{len(companies)} companies.[/green]")
        except Exception as e:
            console.print(f"\n[red]Error fetching audience: {e}[/red]")
            raise typer.Exit(1)
        csv_label = audience_id

    if not companies:
        console.print("[yellow]No companies to sync.[/yellow]")
        raise typer.Exit(0)

    # ── Save CSV immediately ─────────────────────────────────────────────────
    try:
        csv_path = _save_audience_csv(csv_label, companies)
        console.print(f"Saved to [bold]{csv_path}[/bold]")
    except Exception as e:
        console.print(f"[red]Could not save CSV: {e}[/red]")
        raise typer.Exit(1)

    console.print()

    # ── CRM connection ───────────────────────────────────────────────────────
    adapter = ADAPTERS[crm]()
    console.print(f"Connecting to {crm.title()}...", end=" ")
    try:
        adapter.connect()
    except KeyError as e:
        console.print(f"\n[red]Missing environment variable: {e}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"\n[red]Connection failed: {e}[/red]")
        raise typer.Exit(1)
    console.print("[green]connected.[/green]")

    if dry_run:
        console.print("[yellow]Dry run mode — CRM will not be updated.[/yellow]")

    console.print()

    # ── Match companies to existing CRM accounts (bulk) ──────────────────────
    matched: list[tuple[dict, str]] = []    # (company, account_id)
    unmatched: list[dict] = []

    console.print(f"Matching {len(companies)} companies to existing accounts...")
    try:
        match_map = adapter.find_accounts_bulk(companies, field_mapping["grizz_id"])
    except Exception as e:
        console.print(f"  [red]Bulk lookup failed: {e}[/red]")
        match_map = {}

    for company in companies:
        grizz_id = str(company.get("grizz_id") or company.get("company_id") or "")
        domain = company.get("domain") or ""
        account_id = match_map.get(grizz_id) or match_map.get(domain)
        if account_id:
            matched.append((company, account_id))
        else:
            unmatched.append(company)

    console.print(
        f"  [green]{len(matched)} matched[/green]  "
        f"[yellow]{len(unmatched)} unmatched[/yellow]"
    )
    console.print()

    # ── Update matched accounts (batched) ────────────────────────────────────
    updated = 0
    if matched:
        console.print(f"Updating {len(matched)} matched account(s)...")

        # Build all update records first
        records_to_update = []
        for company, account_id in matched:
            grizz_id = company.get("grizz_id") or company.get("company_id")
            grizz_data = {
                "grizz_id":    grizz_id,
                "gid_company": company.get("gid_company"),
                "grizz_url":   company.get("grizz_url") or (f"https://getgrizz.com/company/{grizz_id}" if grizz_id else None),
                "company_name": company.get("company_name"),
                "domain":       company.get("domain"),
                "linkedin_url": company.get("linkedin_url"),
                "phone":        company.get("phone"),
                "email":        company.get("email"),
                "city":         company.get("hq_city"),
                "state_province_region": company.get("hq_region"),
                "country":      company.get("hq_country"),
                "employee_range": company.get("employee_range"),
                "naics_code":   _first_naics(company.get("naics")),
                "grizz_activity": company.get("grizz_activity"),
                "revenue_range": company.get("revenue_range"),
                "erp_tech_stack":    company.get("erp_tech_stack"),
                "erp_match_type":    company.get("erp_match_type"),
                "erp_keyword_usage": company.get("erp_keyword_usage"),
                "ats_tech_stack":    company.get("ats_tech_stack"),
                "ats_match_type":    company.get("ats_match_type"),
                "ats_keyword_usage": company.get("ats_keyword_usage"),
                "other_tech_signals": company.get("other_tech_signals"),
            }
            mapped = apply_mapping(grizz_data, field_mapping)
            if not mapped:
                continue
            records_to_update.append({"Id": account_id, **mapped})

        if dry_run:
            for r in records_to_update:
                console.print(f"  [dim]{r['Id']}: would update {[k for k in r if k != 'Id']}[/dim]")
        else:
            updated, records_to_update = _run_update_batches(
                adapter, records_to_update, batch_size, console,
                interactive=not non_interactive,
            )
        console.print()

    # ── Prompt to create unmatched accounts ─────────────────────────────────
    created = 0
    if unmatched:
        if non_interactive:
            create = True
            console.print(
                f"{len(unmatched)} unmatched compan(ies) — creating new records "
                f"(non-interactive)."
            )
        else:
            create = questionary.confirm(
                f"{len(unmatched)} companies could not be matched to an existing account "
                f"in your CRM. Create new company records?",
                default=False,
            ).ask()

        if create:
            console.print()

            # Build all records first
            records_to_create = []
            for company in unmatched:
                grizz_id = company.get("grizz_id") or company.get("company_id")
                grizz_data = {
                    "grizz_id":    grizz_id,
                    "gid_company": company.get("gid_company"),
                    "grizz_url":   f"https://getgrizz.com/company/{grizz_id}" if grizz_id else None,
                    "company_name": company.get("company_name"),
                    "domain":       company.get("domain"),
                    "linkedin_url": company.get("linkedin_url"),
                    "phone":        company.get("phone"),
                    "email":        company.get("email"),
                    "city":         company.get("hq_city"),
                    "state_province_region": company.get("hq_region"),
                    "country":      company.get("hq_country"),
                    "employee_range": company.get("employee_range"),
                    "naics_code":   _first_naics(company.get("naics")),
                    "grizz_activity": company.get("grizz_activity"),
                    "revenue_range": company.get("revenue_range"),
                    "erp_tech_stack":    company.get("erp_tech_stack"),
                    "erp_match_type":     company.get("erp_match_type"),
                    "erp_keyword_usage": company.get("erp_keyword_usage"),
                    "ats_tech_stack":    company.get("ats_tech_stack"),
                    "ats_match_type":     company.get("ats_match_type"),
                    "ats_keyword_usage": company.get("ats_keyword_usage"),
                    "other_tech_signals": company.get("other_tech_signals"),
                }
                mapped = apply_mapping(grizz_data, field_mapping)
                if crm == "salesforce":
                    if company.get("company_name"):
                        mapped.setdefault("Name", company["company_name"])
                    if company.get("domain"):
                        mapped.setdefault("Website", f"https://{company['domain']}")
                elif crm == "hubspot":
                    if company.get("company_name"):
                        mapped.setdefault("name", company["company_name"])
                    if company.get("domain"):
                        mapped.setdefault("domain", company["domain"])
                elif crm == "attio":
                    # Native attributes for brand-new records: company name and
                    # the unique `domains` match-key (the adapter wraps it in a list).
                    if company.get("company_name"):
                        mapped.setdefault("name", company["company_name"])
                    if company.get("domain"):
                        mapped.setdefault("domains", company["domain"])
                records_to_create.append(mapped)

            if dry_run:
                for r in records_to_create:
                    console.print(f"  [dim]would create: {r}[/dim]")
            else:
                # Send in batches
                total = len(records_to_create)
                for batch_start in range(0, total, batch_size):
                    batch = records_to_create[batch_start:batch_start + batch_size]
                    batch_end = min(batch_start + batch_size, total)
                    console.print(f"  Creating records {batch_start + 1}–{batch_end} of {total}...", end=" ")
                    try:
                        results = adapter.create_accounts(batch)
                        batch_ok = sum(1 for r in results if r.get("success"))
                        batch_fail = len(results) - batch_ok
                        created += batch_ok
                        if batch_fail:
                            console.print(f"[green]{batch_ok} created[/green], [red]{batch_fail} failed.[/red]")
                            for r in results:
                                if not r.get("success"):
                                    console.print(f"    [red]{r.get('errors')}[/red]")
                        else:
                            console.print(f"[green]{batch_ok} created.[/green]")
                    except Exception as e:
                        console.print(f"[red]batch failed: {e}[/red]")

    # ── Summary ──────────────────────────────────────────────────────────────
    console.print()
    console.print(f"[bold]Done.[/bold]")
    console.print(f"  CSV saved:      [bold]{csv_path}[/bold]")
    console.print(f"  Matched:        {len(matched)}" + ("" if dry_run else f" ({updated} updated)"))
    console.print(f"  Unmatched:      {len(unmatched)}")
    if unmatched and create:
        console.print(f"  Created:        {created}")


# ── CLI commands ───────────────────────────────────────────────────────────────

# ── People discovery ────────────────────────────────────────────────────────────

_DISCOVERY_PROMPT = "give me relevant contacts on these accounts"

# Input-CSV column aliases (matched case/space/underscore-insensitively).
_DISCOVERY_COL_ALIASES = {
    "record_id":     ("record_id", "id", "account id", "crm_record_id", "crm id"),
    "name":          ("name", "company_name", "crm_name", "account name", "company"),
    "gid_company":   ("gid_company", "gid", "grizz_company_gid", "company_gid"),
    "domain":        ("domain", "grizz_domain", "website", "company_domain"),
    "domain_source": ("domain_source",),
}

_DISCOVERY_CSV_FIELDS = [
    "gid", "first_name", "last_name", "title", "persona", "seniority",
    "department", "company", "company_gid", "crm_company_record_id",
    "crm_name", "company_domain", "city", "state", "country",
    "linkedin_url", "fallback", "email_entitled", "phone_entitled",
]


def _norm_header(h: str) -> str:
    return (h or "").strip().lower().replace("-", " ").replace("_", " ")


def _read_discovery_rows(path: Path) -> list[dict]:
    """Read the discovery input CSV into normalized rows.  Each row carries
    record_id (required) plus a gid_company and/or domain to key discovery on."""
    with open(path, newline="", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    if not raw:
        console.print("[red]Input CSV is empty.[/red]")
        raise typer.Exit(1)

    headers = list(raw[0].keys())
    norm = {h: _norm_header(h) for h in headers}

    def find(canon: str) -> Optional[str]:
        wants = {_norm_header(a) for a in _DISCOVERY_COL_ALIASES[canon]}
        return next((h for h in headers if norm[h] in wants), None)

    cols = {c: find(c) for c in _DISCOVERY_COL_ALIASES}
    if not cols["record_id"]:
        console.print("[red]Input CSV needs a record_id column "
                      "(aliases: id, account id, crm_record_id).[/red]")
        raise typer.Exit(1)
    if not cols["gid_company"] and not cols["domain"]:
        console.print("[red]Input CSV needs a gid_company or domain column.[/red]")
        raise typer.Exit(1)

    def cell(r: dict, c: str) -> str:
        return (r.get(cols[c]) or "").strip() if cols[c] else ""

    rows = []
    for r in raw:
        rid = cell(r, "record_id")
        if not rid:
            continue
        rows.append({
            "record_id": rid,
            "name": cell(r, "name"),
            "gid_company": cell(r, "gid_company"),
            "domain": cell(r, "domain"),
            "domain_source": cell(r, "domain_source"),
        })
    return rows


def _resolve_gids(api_key: str, rows: list[dict]) -> tuple[int, list[dict]]:
    """Fill gid_company for rows that only have a domain, via a free cascade
    lookup — so every company keys on a Grizz company and round-trips cleanly
    (this also collapses franchise/redirect domains onto the canonical company).
    Mutates rows in place; returns (resolved_count, still_unresolved_rows)."""
    need = [r for r in rows if not r["gid_company"] and r["domain"]]
    if not need:
        return 0, []
    resolved = 0
    for i in range(0, len(need), 5000):
        chunk = need[i:i + 5000]
        matches = grizz_client.lookup_batch(
            api_key, [{"domain": r["domain"]} for r in chunk])
        by_domain = {}
        for m in matches:
            d = ((m or {}).get("input") or {}).get("domain")
            if d:
                by_domain[d.lower()] = m
        for r in chunk:
            comp = (by_domain.get(r["domain"].lower()) or {}).get("company") or {}
            gid = comp.get("gid_company")
            if gid:
                r["gid_company"] = gid
                if not r["name"]:
                    r["name"] = comp.get("company_name") or ""
                if not r["domain_source"]:
                    r["domain_source"] = "grizz"
                resolved += 1
    return resolved, [r for r in need if not r["gid_company"]]


def _fetch_all_members(api_key: str, audience_id: str, per_page: int = 200) -> list[dict]:
    """Page through every member of a completed people audience."""
    out: list[dict] = []
    page = 1
    while True:
        resp = grizz_client.get_people_audience_members(
            api_key, audience_id, page=page, per_page=per_page)
        results = resp.get("results")
        if not results:
            break
        out.extend(results)
        if len(out) >= resp.get("total", len(out)):
            break
        page += 1
    return out


def _write_discovery_csv(path: Path, contacts: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_DISCOVERY_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for c in contacts:
            w.writerow(c)


def run_people_discover(input_file: Path, out_dir: Path, prompt: str,
                        max_per_company: int, batch_size: int,
                        poll_interval: int, poll_timeout: int, resume: bool) -> None:
    """Discover contacts for a company list and emit the JSON files
    log_people_batch.py ingests (contacts.json + checked.json) + a review CSV.
    Discovery is free — no email/phone is fetched and no credits are spent."""
    api_key = os.environ.get("GRIZZ_API_KEY")
    if not api_key:
        console.print("[red]GRIZZ_API_KEY is not set.[/red] Add it to your [bold].env[/bold] file.")
        raise typer.Exit(1)

    rows = _read_discovery_rows(input_file)
    console.print(f"Loaded [bold]{len(rows)}[/bold] company row(s) from {input_file}.")

    resolved, unresolved = _resolve_gids(api_key, rows)
    if resolved:
        console.print(f"Resolved [bold]{resolved}[/bold] domain-only row(s) to a Grizz company.")
    if unresolved:
        console.print(f"[yellow]{len(unresolved)} row(s) have no gid and no resolvable domain — "
                      f"recorded as checked with 0 contacts.[/yellow]")

    # gid -> input rows (duplicate CRM records can resolve to one company)
    by_gid: dict[str, list[dict]] = {}
    for r in rows:
        if r["gid_company"]:
            by_gid.setdefault(r["gid_company"], []).append(r)

    out_dir.mkdir(parents=True, exist_ok=True)
    contacts_path = out_dir / "contacts.json"
    checked_path = out_dir / "checked.json"
    csv_path = out_dir / "contacts_review.csv"
    state_path = out_dir / "discover_progress.json"

    done_gids: set[str] = set()
    contacts: list[dict] = []
    if resume and state_path.exists():
        done_gids = set(json.loads(state_path.read_text()).get("done_gids", []))
        if contacts_path.exists():
            contacts = json.loads(contacts_path.read_text())
        console.print(f"[cyan]Resuming — {len(done_gids)} compan(ies) already discovered.[/cyan]")

    remaining = [g for g in by_gid if g not in done_gids]
    console.print(f"Discovering contacts for [bold]{len(remaining)}[/bold] compan(ies) "
                  f"in batches of {batch_size}.\n")

    unmapped = 0
    failed_batches = 0
    total_batches = (len(remaining) + batch_size - 1) // batch_size
    for bi in range(0, len(remaining), batch_size):
        batch = remaining[bi:bi + batch_size]
        n = bi // batch_size + 1
        console.print(f"[{n}/{total_batches}] audience for {len(batch)} compan(ies)...", end=" ")
        try:
            aud = grizz_client.create_people_audience(
                api_key, prompt=prompt, company_gids=batch,
                max_per_company=max_per_company)
        except Exception as e:
            console.print(f"[red]create failed: {e}[/red]")
            failed_batches += 1
            continue

        aid = aud.get("id")
        status = aud.get("status")
        waited = 0
        while status not in ("COMPLETE", "FAILED") and waited < poll_timeout:
            time.sleep(poll_interval)
            waited += poll_interval
            try:
                status = grizz_client.get_people_audience(api_key, aid).get("status")
            except Exception as e:
                console.print(f"[yellow](poll error: {e})[/yellow]", end=" ")

        if status != "COMPLETE":
            console.print(f"[red]status={status} after {waited}s — retry with --resume.[/red]")
            failed_batches += 1
            continue

        batch_contacts = 0
        for m in _fetch_all_members(api_key, aid):
            owners = by_gid.get(m.get("company_gid"))
            if not owners:
                unmapped += 1
                continue
            owner = owners[0]  # attribute to the first CRM record when duplicates share a gid
            row = dict(m)
            row["crm_company_record_id"] = owner["record_id"]
            row["crm_name"] = owner["name"] or m.get("company") or ""
            row["company_domain"] = owner["domain"] or ""
            contacts.append(row)
            batch_contacts += 1

        done_gids.update(batch)
        console.print(f"[green]COMPLETE[/green] — {batch_contacts} contact(s).")
        # persist after every batch so the run is crash-safe / resumable
        contacts_path.write_text(json.dumps(contacts, indent=2))
        state_path.write_text(json.dumps({"done_gids": sorted(done_gids)}, indent=2))

    # checked.json — one roster row per input company (found or not)
    checked = [{
        "record_id": r["record_id"],
        "company_name": r["name"],
        "domain_used": r["domain"],
        "domain_source": r["domain_source"] or ("grizz" if r["gid_company"] else ""),
    } for r in rows]
    checked_path.write_text(json.dumps(checked, indent=2))
    _write_discovery_csv(csv_path, contacts)

    console.print()
    console.print(f"[bold green]Done.[/bold green] {len(contacts)} contact(s) across "
                  f"{len(done_gids)} compan(ies); {len(checked)} companies recorded as checked.")
    if unmapped:
        console.print(f"[yellow]{unmapped} contact(s) resolved to a company_gid not in the input "
                      f"(parent/dedup) — dropped as unattributable.[/yellow]")
    if failed_batches:
        console.print(f"[yellow]{failed_batches} batch(es) failed/timed out — re-run with --resume.[/yellow]")
    console.print(f"\nWrote:\n  {contacts_path}  (per-contact, keyed to each CRM record)"
                  f"\n  {checked_path}   (coverage roster — every company checked, found or not)"
                  f"\n  {csv_path}  (human review)")
    console.print("\ncontacts.json + checked.json are the discovery hand-off; feed them to "
                  "your contact-log loader before the paid email/phone enrich step.")


@people_app.command("discover")
def people_discover(
    input: Path = typer.Option(..., "--input", help="CSV of companies. Needs a record_id column plus gid_company and/or domain."),
    out_dir: Path = typer.Option(Path("people_out"), "--out-dir", help="Directory for contacts.json + checked.json + review CSV."),
    prompt: str = typer.Option(_DISCOVERY_PROMPT, "--prompt", help="Discovery prompt (the org's saved ICP personas expand it server-side)."),
    max_per_company: int = typer.Option(3, "--max-per-company", help="Max contacts per company."),
    batch_size: int = typer.Option(50, "--batch-size", help="Companies per audience (<=50 recommended)."),
    poll_interval: int = typer.Option(5, "--poll-interval", help="Seconds between status polls."),
    poll_timeout: int = typer.Option(600, "--poll-timeout", help="Max seconds to wait per batch before skipping (retry with --resume)."),
    resume: bool = typer.Option(False, "--resume", help="Skip companies already discovered in a prior run (reads out-dir progress)."),
):
    """Discover contacts (people) for a company list — free; no credits spent.

    Keys discovery on each company's Grizz gid_company (domain-only rows are
    resolved to one first).  Emits the two JSON files log_people_batch.py
    ingests (contacts.json + checked.json) plus a review CSV.  Finding people
    is free; email/phone enrichment is a separate, paid step.
    """
    if not input.exists():
        console.print(f"[red]Input file not found: {input}[/red]")
        raise typer.Exit(1)
    batch_size = max(1, min(batch_size, 200))
    run_people_discover(input, out_dir, prompt, max_per_company,
                        batch_size, poll_interval, poll_timeout, resume)


# ── People sync (enrich + push contacts to CRM) ─────────────────────────────────

def _crm_contact_credentials(crm: str) -> dict:
    """Gather the customer-CRM credentials the server-side contact endpoints
    expect (they ride in the request body, not the auth header — that's the
    Grizz API key).  Sourced from the same env vars the CRM adapters use."""
    if crm == "hubspot":
        token = os.environ.get("HUBSPOT_API_KEY")
        if not token:
            console.print("[red]HUBSPOT_API_KEY is not set.[/red] "
                          "Add your HubSpot private-app token to [bold].env[/bold].")
            raise typer.Exit(1)
        return {"hubspot_key": token}
    if crm == "salesforce":
        inst = os.environ.get("SALESFORCE_INSTANCE_URL")
        sess = os.environ.get("SALESFORCE_SESSION_ID")
        if not (inst and sess):
            console.print("[red]SALESFORCE_INSTANCE_URL and SALESFORCE_SESSION_ID must be set "
                          "in [bold].env[/bold] for contact sync.[/red]")
            raise typer.Exit(1)
        return {"sf_instance_url": inst, "sf_session_id": sess}
    console.print(f"[red]Contact sync is not supported for CRM '{crm}'.[/red]")
    raise typer.Exit(1)


def _read_person_gids(path: Path) -> list[str]:
    """Read Grizz person gids from a `people discover` contacts.json, a CSV with
    a gid/gid_person column, or a plain one-gid-per-line file."""
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        gids = [(c.get("gid") or c.get("gid_person") or "").strip()
                for c in data if isinstance(c, dict)]
        return [g for g in gids if g]
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    gid_keys = ("gid", "gid_person", "person_gid")
    if any(h in gid_keys for h in header):
        idx = next(i for i, h in enumerate(header) if h in gid_keys)
        return [r[idx].strip() for r in rows[1:] if len(r) > idx and r[idx].strip()]
    return [r[0].strip() for r in rows if r and r[0].strip()]


def _poll_until_terminal(fn, poll_interval: int, poll_timeout: int,
                         terminal=("COMPLETE", "FAILED")) -> dict | None:
    """Poll a status-returning callable until it reaches a terminal status or
    the timeout elapses.  Returns the last response (or None on immediate error)."""
    waited = 0
    try:
        resp = fn()
    except Exception as e:
        console.print(f"[yellow](poll error: {e})[/yellow]", end=" ")
        resp = None
    while (resp or {}).get("status") not in terminal and waited < poll_timeout:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            resp = fn()
        except Exception as e:
            console.print(f"[yellow](poll error: {e})[/yellow]", end=" ")
    return resp


def _run_enrich(api_key: str, gids: list[str], enrich_email: bool, enrich_phone: bool,
                batch_size: int, poll_interval: int, poll_timeout: int) -> None:
    """Enrich email/phone in chunks, polling each chunk to FULL completion.

    Never reports a chunk as done while contacts are still pending — a mid-flight
    enrich that printed "done" is exactly what tempted the re-runs that
    double-charged phone credits.  On a chunk that doesn't settle in the poll
    window it says so plainly and tells you to re-run (re-enriching an
    already-entitled contact is free)."""
    want = ", ".join(f for f, on in (("email", enrich_email), ("phone", enrich_phone)) if on)
    n_batches = (len(gids) + batch_size - 1) // batch_size
    console.print(f"Enriching [bold]{want}[/bold] for {len(gids)} contact(s) in "
                  f"{n_batches} chunk(s) of {batch_size} (spends credits).")

    tot = {"found_email": 0, "found_phone": 0, "failed": 0, "pending": 0}
    incomplete = 0
    for bi in range(0, len(gids), batch_size):
        chunk = gids[bi:bi + batch_size]
        n = bi // batch_size + 1
        console.print(f"[{n}/{n_batches}] enriching {len(chunk)}...", end=" ")
        sub = grizz_client.enrich_contacts_batch(api_key, chunk,
                                                 include_email=enrich_email,
                                                 include_phone=enrich_phone)
        final = _poll_until_terminal(
            lambda: grizz_client.check_person_enrich_batch(api_key, sub.get("request_id")),
            poll_interval, poll_timeout) or {}
        fe = final.get("found_email", 0) or 0
        fp = final.get("found_phone", 0) or 0
        failed = final.get("failed", 0) or 0
        pending = final.get("pending", 0) or 0
        # `settled` is the server's ground-truth "is this final?" flag; fall
        # back to status for older servers.  found_email/found_phone are a live
        # snapshot that undercounts until the chunk settles, so only fold them
        # into the running totals for SETTLED chunks — otherwise the summary
        # mixes real yields with partial snapshots (the "reported 40, real 82"
        # undercount).  failed always counts (it's terminal per-row).
        settled = bool(final.get("settled")) or final.get("status") == "COMPLETE"
        tot["failed"] += failed
        if settled:
            # Every child settled (pending is 0).  failed rows are recoverable.
            tot["found_email"] += fe
            tot["found_phone"] += fp
            msg = f"[green]complete[/green] — found_email={fe} found_phone={fp}"
            if failed:
                msg += f" [yellow]failed={failed} (recoverable)[/yellow]"
            console.print(msg)
        else:
            # Poll window elapsed with children still in flight — NOT done.
            # Do NOT add its partial fe/fp to the totals; the real yield for
            # this chunk is unknown until it settles on a re-run of this command.
            incomplete += 1
            tot["pending"] += pending
            console.print(f"[yellow]still running — pending={pending} failed={failed} "
                          f"found so far (partial, not counted)={fe}e/{fp}p; "
                          f"not complete, re-run to settle.[/yellow]")

    settled_note = "" if not incomplete else f" (from {n_batches - incomplete}/{n_batches} settled chunks)"
    console.print(f"\nEnrich summary: found_email={tot['found_email']} "
                  f"found_phone={tot['found_phone']}{settled_note} "
                  f"failed={tot['failed']} pending={tot['pending']}.")
    if incomplete or tot["pending"] or tot["failed"]:
        console.print(
            "[yellow]Enrichment did NOT fully settle[/yellow] — "
            f"{tot['pending']} pending, {tot['failed']} failed (both recoverable). "
            "Re-run the same command later to finish; already-entitled contacts "
            "are not re-charged. Proceeding to push whatever is entitled so far.")


def run_people_sync(crm: str, contacts_path: Path, enrich_email: bool, enrich_phone: bool,
                    enrich_batch_size: int, poll_interval: int, poll_timeout: int,
                    dry_run: bool) -> None:
    """Enrich (optional) + push discovered contacts to the CRM via the same
    server-side create-crm endpoint the MCP uses — which matches each contact
    against its parent account's existing CRM contacts and UPDATES in place
    rather than duplicating (create-or-update)."""
    api_key = os.environ.get("GRIZZ_API_KEY")
    if not api_key:
        console.print("[red]GRIZZ_API_KEY is not set.[/red] Add it to your [bold].env[/bold] file.")
        raise typer.Exit(1)
    crm = crm.strip().lower()
    credentials = _crm_contact_credentials(crm)   # validates env before any work

    gids = list(dict.fromkeys(_read_person_gids(contacts_path)))  # de-dup, keep order
    if not gids:
        console.print(f"[red]No person gids found in {contacts_path}.[/red]")
        raise typer.Exit(1)
    src = "HUBSPOT_API_KEY" if crm == "hubspot" else "SALESFORCE_SESSION_ID/INSTANCE_URL"
    console.print(f"Loaded [bold]{len(gids)}[/bold] contact(s) from {contacts_path}; "
                  f"CRM=[bold]{crm}[/bold] (creds from {src}).")

    # ── optional enrichment (spends credits) ─────────────────────────────────
    if enrich_email or enrich_phone:
        _run_enrich(api_key, gids, enrich_email, enrich_phone,
                    enrich_batch_size, poll_interval, poll_timeout)

    records = [{"gid_person": g} for g in gids]
    if dry_run:
        console.print(f"[yellow]Dry run — {len(records)} record(s) resolved; "
                      f"not writing to {crm}.[/yellow]")
        return

    # ── push in batches (server dedups + creates/updates per account) ────────
    BATCH = 100
    agg = {k: 0 for k in ("created", "updated", "no_grizz_match",
                          "no_parent_match", "parent_linked", "errors")}
    n_batches = (len(records) + BATCH - 1) // BATCH
    for bi in range(0, len(records), BATCH):
        chunk = records[bi:bi + BATCH]
        n = bi // BATCH + 1
        console.print(f"[{n}/{n_batches}] pushing {len(chunk)} contact(s)...", end=" ")
        sub = grizz_client.create_in_crm_contacts(api_key, crm, credentials, chunk)
        rid = sub.get("request_id")
        final = _poll_until_terminal(
            lambda: grizz_client.check_crm_write_request(api_key, rid),
            poll_interval, poll_timeout)
        if not final or final.get("status") != "COMPLETE":
            console.print(f"[red]{(final or {}).get('status', 'timeout')}[/red] "
                          f"{(final or {}).get('error_message', '')}")
            agg["errors"] += len(chunk)
            continue
        for k in agg:
            agg[k] += final.get(k, 0) or 0
        console.print(f"[green]created={final.get('created', 0)} "
                      f"updated={final.get('updated', 0)}[/green]")

    console.print(f"\n[bold green]Done.[/bold green] "
                  f"created={agg['created']}  updated={agg['updated']}  "
                  f"no_grizz_match={agg['no_grizz_match']}  "
                  f"no_parent_match={agg['no_parent_match']}  "
                  f"parent_linked={agg['parent_linked']}  errors={agg['errors']}")


@people_app.command("sync")
def people_sync(
    crm: str = typer.Option(..., "--crm", help="hubspot or salesforce"),
    contacts: Path = typer.Option(..., "--contacts", help="contacts.json from `people discover`, or a CSV/txt of person gids"),
    enrich_email: bool = typer.Option(False, "--enrich-email", help="Enrich work email before sync (spends credits)"),
    enrich_phone: bool = typer.Option(False, "--enrich-phone", help="Enrich phone before sync (spends credits)"),
    enrich_batch_size: int = typer.Option(50, "--enrich-batch-size", help="Contacts per enrich chunk (<=50 keeps each chunk inside the poll window)."),
    poll_interval: int = typer.Option(5, "--poll-interval", help="Seconds between status polls."),
    poll_timeout: int = typer.Option(900, "--poll-timeout", help="Soft per-chunk settle wait (seconds). NOT a correctness/charging boundary — a chunk that doesn't settle is reported as unsettled and safely finished by re-running the same command (already-entitled contacts are never re-charged). Raise it if chunks routinely don't settle (the upstream provider retries under rate limit can exceed the default); don't lower it below ~600 or you'll churn re-runs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve + count only; do not write to the CRM."),
):
    """Push discovered contacts to your CRM — server-side create-OR-update.

    Each contact is matched against its parent account's existing CRM contacts
    (gid_person → email → linkedin → name) and updated in place when found,
    created otherwise — so a re-sync or an email-less existing contact isn't
    duplicated.  The CRM key rides in the request body as `credentials` (from
    .env); only the Grizz key is a Bearer header.  Discovery is free; email/phone
    enrichment (--enrich-email/--enrich-phone) is a separate, paid step run in
    chunks — it reports each chunk as complete only once every contact settles,
    never mid-flight.
    """
    if not contacts.exists():
        console.print(f"[red]Contacts file not found: {contacts}[/red]")
        raise typer.Exit(1)
    enrich_batch_size = max(1, min(enrich_batch_size, 100))
    run_people_sync(crm, contacts, enrich_email, enrich_phone, enrich_batch_size,
                    poll_interval, poll_timeout, dry_run)


@app.command()
def enrich(
    crm: str = typer.Option(..., help=f"CRM to use. Available: {', '.join(ADAPTERS)}"),
    input: Path = typer.Option(..., help="CSV file containing account IDs (requires an 'Id' column)"),
    config: Path = typer.Option(Path("config.yaml"), help="Path to your config.yaml"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without updating the CRM"),
):
    """Enrich CRM accounts from a CSV of account IDs using the Grizz API."""
    if crm not in ADAPTERS:
        console.print(f"[red]Unknown CRM '{crm}'. Available: {', '.join(ADAPTERS)}[/red]")
        raise typer.Exit(1)
    run_enrich(crm, input, config, dry_run)


# ── Interactive menu ───────────────────────────────────────────────────────────

@app.command()
def audience(
    crm: str = typer.Option("salesforce", help=f"CRM to use. Available: {', '.join(ADAPTERS)}"),
    audience_id: Optional[str] = typer.Option(None, "--audience-id", help="Existing Grizz audience ID"),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Prompt to create a new audience"),
    gids: Optional[Path] = typer.Option(None, "--gids", help="File of gid_company values (one per line or a CSV with a gid_company column) — e.g. a filtered selection from the Grizz MCP"),
    config: Path = typer.Option(Path("config.yaml"), help="Path to your config.yaml"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing to CRM"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non-interactive: create unmatched companies and retry failures without prompting (auto-enabled when not a TTY)"),
    batch_size: int = typer.Option(200, "--batch-size", help="Records per API call when creating accounts (max 200)"),
):
    """Push a Grizz company list (audience, prompt, or an explicit gid list) into your CRM."""
    if crm not in ADAPTERS:
        console.print(f"[red]Unknown CRM '{crm}'. Available: {', '.join(ADAPTERS)}[/red]")
        raise typer.Exit(1)
    sources = sum(bool(x) for x in (audience_id, prompt, gids))
    if sources == 0:
        console.print("[red]Provide one of --audience-id, --prompt, or --gids.[/red]")
        raise typer.Exit(1)
    if sources > 1:
        console.print("[red]Provide only one of --audience-id, --prompt, or --gids.[/red]")
        raise typer.Exit(1)

    gid_list = None
    if gids:
        if not gids.exists():
            console.print(f"[red]gids file not found: {gids}[/red]")
            raise typer.Exit(1)
        gid_list = _read_gids(gids)
        if not gid_list:
            console.print(f"[red]No gid_company values found in {gids}.[/red]")
            raise typer.Exit(1)

    batch_size = max(1, min(batch_size, 200))
    run_audience(crm, config, dry_run, audience_id=audience_id, prompt=prompt,
                 batch_size=batch_size, gids=gid_list, assume_yes=yes)


def _run_setup(crm: str, contacts: bool, dry_run: bool) -> None:
    """Route to the company or contact setup for the chosen CRM.  The contact
    setups provision exactly the grizz_contact_* properties create-crm writes —
    identical to what the `setup_crm_contacts` MCP tool creates."""
    if crm == "salesforce":
        (run_setup_salesforce_contacts if contacts else run_setup_salesforce)(dry_run=dry_run)
    elif crm == "hubspot":
        (run_setup_hubspot_contacts if contacts else run_setup_hubspot)(dry_run=dry_run)
    elif crm == "attio":
        # Attio provisions company + people attributes together from the Grizz
        # catalog, so one setup run covers both objects.
        if contacts:
            console.print("[dim]Attio provisions company + contact attributes together — "
                          "running the full catalog setup.[/dim]")
        run_setup_attio(dry_run=dry_run)
    else:
        console.print(f"[red]Setup not yet available for '{crm}'.[/red]")
        raise typer.Exit(1)


@app.command()
def setup(
    crm: str = typer.Option("salesforce", help="CRM to set up"),
    object_: str = typer.Option("company", "--object", help="Which records to set up: 'company' or 'contacts'"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without creating fields"),
):
    """Create all recommended Grizz custom fields in your CRM (company or contact records)."""
    obj = object_.strip().lower()
    if obj not in ("company", "companies", "contact", "contacts"):
        console.print(f"[red]--object must be 'company' or 'contacts' (got '{object_}').[/red]")
        raise typer.Exit(1)
    _run_setup(crm, contacts=obj in ("contact", "contacts"), dry_run=dry_run)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return

    console.print(f"\n[bold]Grizz CRM Enrichment Tool[/bold]  v{__version__}\n")

    action = questionary.select(
        "What would you like to do?",
        choices=[
            "Set up CRM fields",
            "Enrich accounts from CSV",
            "Run audience → push to CRM",
            questionary.Choice("Create CRM campaign  (coming soon)", disabled="coming soon"),
            "Exit",
        ],
    ).ask()

    if not action or action == "Exit":
        raise typer.Exit(0)

    if action == "Set up CRM fields":
        crm = questionary.select(
            "Select your CRM:",
            choices=list(ADAPTERS.keys()),
        ).ask()
        if not crm:
            raise typer.Exit(0)

        which = questionary.select(
            "Which records?",
            choices=["Company / Account fields", "Contact fields"],
        ).ask()
        if not which:
            raise typer.Exit(0)

        dry_run = questionary.confirm(
            "Dry run (preview without creating fields)?",
            default=False,
        ).ask()
        console.print()
        _run_setup(crm, contacts=(which == "Contact fields"), dry_run=bool(dry_run))
        return

    if action == "Enrich accounts from CSV":
        crm = questionary.select(
            "Select your CRM:",
            choices=list(ADAPTERS.keys()),
        ).ask()
        if not crm:
            raise typer.Exit(0)

        input_path = questionary.path(
            "Path to CSV file of account IDs:",
            validate=lambda p: Path(p).exists() or "File not found",
        ).ask()
        if not input_path:
            raise typer.Exit(0)

        config_path = questionary.text(
            "Config file:",
            default="config.yaml",
        ).ask()
        if not config_path:
            raise typer.Exit(0)

        dry_run = questionary.confirm(
            "Dry run (preview without updating CRM)?",
            default=False,
        ).ask()

        console.print()
        run_enrich(crm, Path(input_path), Path(config_path), bool(dry_run))

    if action == "Run audience → push to CRM":
        crm = questionary.select(
            "Select your CRM:",
            choices=list(ADAPTERS.keys()),
        ).ask()
        if not crm:
            raise typer.Exit(0)

        audience_id = questionary.text(
            "Audience ID (leave blank to create a new one):",
        ).ask()

        prompt = None
        if not audience_id:
            prompt = questionary.text("Describe your target audience:").ask()
            if not prompt:
                raise typer.Exit(0)

        config_path = questionary.text("Config file:", default="config.yaml").ask()
        if not config_path:
            raise typer.Exit(0)

        dry_run = questionary.confirm(
            "Dry run (preview without writing to CRM)?",
            default=False,
        ).ask()

        console.print()
        run_audience(
            crm,
            Path(config_path),
            bool(dry_run),
            audience_id=audience_id or None,
            prompt=prompt,
        )


if __name__ == "__main__":
    app()
