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
import os
import sys
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

load_dotenv(override=True)

app = typer.Typer(
    help="Enrich your CRM with Grizz construction company data.",
    add_completion=False,
    no_args_is_help=False,
)
console = Console()


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
            console.print(f"  [yellow]No match found in Grizz.[/yellow]")
            summary.append((account_id, "no_match", domain))
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
        "no_match": "yellow",
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
        console.print(f"  [yellow]{unmatched} gid(s) not recognized by Grizz — skipped.[/yellow]")
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


@app.command()
def setup(
    crm: str = typer.Option("salesforce", help="CRM to set up"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without creating fields"),
):
    """Create all recommended Grizz custom fields in your CRM."""
    if crm == "salesforce":
        run_setup_salesforce(dry_run=dry_run)
    elif crm == "hubspot":
        run_setup_hubspot(dry_run=dry_run)
    elif crm == "attio":
        run_setup_attio(dry_run=dry_run)   # fetches the catalog from Grizz, then provisions over the Attio API
    else:
        console.print(f"[red]Setup not yet available for '{crm}'.[/red]")
        raise typer.Exit(1)


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

        dry_run = questionary.confirm(
            "Dry run (preview without creating fields)?",
            default=False,
        ).ask()
        console.print()
        if crm == "salesforce":
            run_setup_salesforce(dry_run=bool(dry_run))
        elif crm == "hubspot":
            run_setup_hubspot(dry_run=bool(dry_run))
        elif crm == "attio":
            run_setup_attio(dry_run=bool(dry_run))
        else:
            console.print(f"[red]Setup not yet available for '{crm}'.[/red]")
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
