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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import questionary
import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from grizz_enrichment import __version__, grizz_client
from grizz_enrichment.adapters import ADAPTERS, CONTACT_ADAPTERS
from grizz_enrichment.adapters.attio import AttioAdapter, AttioContactAdapter
from grizz_enrichment.audience_client import fetch_audience, submit as submit_audience
from grizz_enrichment.domain_utils import clean_domain
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

def run_enrich(crm: str, input_file: Path, config_path: Path, dry_run: bool,
               concurrency: int = 12) -> None:
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
    if crm == "attio":
        adapter.set_phone_slugs(_attio_phone_slugs(crm_config))

    # ── Input ────────────────────────────────────────────────────────────────
    account_ids = read_account_ids(input_file)
    console.print(f"Loaded [bold]{len(account_ids)}[/bold] account(s) from {input_file}.")

    if dry_run:
        console.print("[yellow]Dry run mode — CRM will not be updated.[/yellow]")

    console.print()

    # ── Process ──────────────────────────────────────────────────────────────
    # Attio (and the other CRMs here) have no batch-write API, so throughput
    # comes from concurrency; the adapters' requests.Session is thread-safe.
    # Dry-run stays single-threaded for a readable, ordered preview.
    total = len(account_ids)

    def _process(account_id: str) -> tuple[str, str, str]:
        """Enrich one account end-to-end (get domain → Grizz → write).
        Returns (account_id, outcome, detail); isolates its own errors so a
        single bad record never aborts the pool."""
        try:
            domain = adapter.get_domain(account_id, domain_field)
        except Exception as e:
            return (account_id, "error", str(e))
        if not domain:
            return (account_id, "skipped", "no domain")
        try:
            # No per-poll on_status spinner — interleaved \r writes are unreadable
            # across threads.
            grizz_data = grizz_enrich(grizz_api_key, domain)
        except Exception as e:
            return (account_id, "error", str(e))
        if grizz_data is None:
            return (account_id, "no_data", domain)
        updates = apply_mapping(grizz_data, field_mapping)
        if not updates:
            return (account_id, "no_updates", domain)
        if dry_run:
            return (account_id, "dry_run", f"{domain}: {', '.join(updates.keys())}")
        try:
            adapter.update_record(account_id, updates)
            return (account_id, "success", domain)
        except Exception as e:
            return (account_id, "error", str(e))

    summary: list[tuple[str, str, str]] = []  # (account_id, outcome, detail)

    if dry_run or concurrency <= 1:
        for i, account_id in enumerate(account_ids, 1):
            result = _process(account_id)
            summary.append(result)
            _, outcome, detail = result
            console.print(f"[{i}/{total}] {account_id} — {outcome}"
                          + (f" ({detail})" if detail else ""))
    else:
        console.print(f"Enriching {total} account(s) with concurrency={concurrency}...")
        done = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_process, aid) for aid in account_ids]
            for fut in as_completed(futures):
                summary.append(fut.result())
                done += 1
                if done % 25 == 0 or done == total:
                    console.print(f"  ...{done}/{total} processed")

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


def _grizz_data(company: dict) -> dict:
    """Normalize a Grizz company payload to the flat grizz_* shape `apply_mapping`
    expects.  Accepts either payload dialect: the audience-result shape
    (phone/email) and the raw lookup-batch shape (hq_phone/hq_email), so an
    audience push and a lookup push write the same fields from the same code."""
    grizz_id = company.get("grizz_id") or company.get("company_id")
    return {
        "grizz_id":     grizz_id,
        "gid_company":  company.get("gid_company"),
        "grizz_url":    company.get("grizz_url") or (f"https://getgrizz.com/company/{grizz_id}" if grizz_id else None),
        "company_name": company.get("company_name"),
        "domain":       company.get("domain"),
        "linkedin_url": company.get("linkedin_url"),
        "company_description": company.get("company_description"),
        "phone":        company.get("phone") or company.get("hq_phone"),
        "email":        company.get("email") or company.get("hq_email"),
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
    """Read gid_company values from a file: the JSONL that `lookup --out` writes,
    a CSV with a gid_company/gid column, or a plain one-per-line list.  Grizz
    company gids start with 'GC'.

    Raises ValueError rather than guessing.  Feeding this a file it cannot parse
    used to yield plausible-looking junk (csv.reader splits a JSON line on the
    commas inside the object), which then resolved to zero companies with a exit
    code 0 — a push that silently did nothing.  Every rejection here is loud.
    """
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return []

    if lines[0].lstrip().startswith("{"):
        # JSONL — what `lookup --out` emits.  Misses (no gid_company) are dropped
        # by design: there is nothing to resolve.  Unparseable lines are not.
        gids = []
        for n, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path} line {n} is not valid JSON ({e}). A JSONL gid file "
                    f"needs one JSON object per line, as `lookup --out` writes."
                ) from e
            if not isinstance(row, dict):
                raise ValueError(f"{path} line {n} is not a JSON object.")
            gid = str(row.get("gid_company") or row.get("gid") or "").strip()
            if gid:
                gids.append(gid)
        return _validated_gids(gids, path)

    rows = [r for r in csv.reader(lines) if r and r[0].strip()]
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
    return _validated_gids(
        [r[col].strip() for r in rows[start:] if len(r) > col and r[col].strip()], path)


def _validated_gids(gids: list[str], path: Path) -> list[str]:
    """Drop values that are not Grizz company gids, loudly.  Downstream these
    would come back as `returned no data — skipped`, which reads like a Grizz
    coverage gap rather than a malformed input file."""
    good = [g for g in gids if g.upper().startswith("GC")]
    bad = [g for g in gids if not g.upper().startswith("GC")]
    if bad and not good:
        raise ValueError(
            f"No Grizz company gids in {path} — every value read looks malformed "
            f"(e.g. {bad[0]!r}). Grizz company gids start with 'GC'."
        )
    if bad:
        console.print(
            f"  [yellow]{len(bad)} value(s) in {path} are not Grizz company gids "
            f"and were skipped (e.g. {bad[0]!r}).[/yellow]"
        )
    return good


_LOOKUP_ID_COLS = ("crm_record_id", "record_id", "id")


def _read_lookup_rows(path: Path) -> list[dict]:
    """Read lookup inputs from a JSONL, CSV, or plain one-per-line file.

    Returns rows of {record_id, domain, gid_company} — record_id is optional and
    only used to echo the caller's own key back into the results, so a CRM
    backlog round-trips without a join on the far side.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line in text.splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            rows.append({
                "record_id":   next((str(r[c]) for c in _LOOKUP_ID_COLS if r.get(c)), ""),
                "domain":      (r.get("domain") or "").strip(),
                "gid_company": (r.get("gid_company") or "").strip(),
            })
        return rows

    raw = [r for r in csv.reader(text.splitlines()) if r and any(c.strip() for c in r)]
    if not raw:
        return []
    header = [h.strip().lower() for h in raw[0]]
    if "domain" in header or "gid_company" in header:
        idx = {name: header.index(name) for name in
               ("domain", "gid_company", *_LOOKUP_ID_COLS) if name in header}

        def cell(r: list[str], name: str) -> str:
            i = idx.get(name)
            return (r[i].strip() if i is not None and len(r) > i else "")

        return [{
            "record_id":   next((cell(r, c) for c in _LOOKUP_ID_COLS if cell(r, c)), ""),
            "domain":      cell(r, "domain"),
            "gid_company": cell(r, "gid_company"),
        } for r in raw[1:]]

    # No recognized header — treat column 0 as a bare domain list, skipping a
    # stray header cell that obviously isn't one.
    start = 1 if raw[0][0].strip().lower() in ("domain", "domains", "website", "url") else 0
    return [{"record_id": "", "domain": r[0].strip(), "gid_company": ""}
            for r in raw[start:] if r[0].strip()]


def run_lookup(input_path: Path, out: Path | None, companies_out: Path | None) -> None:
    """DB-only cascade lookup over a company list — no scrape, no credits.

    The read-only counterpart to `enrich`: it answers "which of these does Grizz
    already know?" without creating EnrichmentRequest rows or charging anything,
    so it is the correct first pass over a large CRM backlog.  Only the misses
    are worth spending an `enrich` on afterwards.
    """
    api_key = os.environ.get("GRIZZ_API_KEY")
    if not api_key:
        console.print("[red]GRIZZ_API_KEY is not set.[/red] Add it to your [bold].env[/bold] file.")
        raise typer.Exit(1)
    if not input_path.exists():
        console.print(f"[red]Input file not found: {input_path}[/red]")
        raise typer.Exit(1)

    rows = _read_lookup_rows(input_path)
    if not rows:
        console.print(f"[red]No lookup inputs found in {input_path}.[/red]")
        raise typer.Exit(1)

    # Deduplicate on the cascade key so a large backlog costs one call per 5000
    # DISTINCT companies, not per row.
    lookups: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["gid_company"], clean_domain(r["domain"]) or "")
        if key == ("", "") or key in seen:
            continue
        seen.add(key)
        lookups.append({k: v for k, v in
                        (("gid_company", r["gid_company"]), ("domain", r["domain"])) if v})

    console.print(f"Rows: {len(rows)}  |  distinct companies to look up: {len(lookups)}")
    t0 = time.time()
    try:
        matches = grizz_client.lookup_batch(api_key, lookups)
    except Exception as e:
        console.print(f"[red]Lookup failed: {e}[/red]")
        raise typer.Exit(1)

    by_key: dict[tuple[str, str], dict] = {}
    for m in matches:
        inp = (m or {}).get("input") or {}
        # The echo carries explicit nulls for unused cascade keys, so `.get(k, "")`
        # yields None (the key exists) and would never join back to a row key.
        by_key[(inp.get("gid_company") or "", clean_domain(inp.get("domain")) or "")] = m

    hits = 0
    via: dict[str, int] = {}
    companies: dict[str, dict] = {}
    results: list[dict] = []
    for r in rows:
        d = clean_domain(r["domain"]) or ""
        m = by_key.get((r["gid_company"], d)) or {}
        c = m.get("company") or {}
        if m.get("matched") and c:
            hits += 1
            via[m.get("match_via") or "?"] = via.get(m.get("match_via") or "?", 0) + 1
            companies.setdefault(c.get("gid_company") or d, c)
        results.append({
            "record_id": r["record_id"], "domain": r["domain"], "clean_domain": d,
            "matched": bool(m.get("matched")), "match_via": m.get("match_via"),
            "gid_company": c.get("gid_company"), "grizz_id": c.get("grizz_id"),
            "company_name": c.get("company_name"), "naics": c.get("naics"),
            "grizz_activity": c.get("grizz_activity"),
        })

    misses = len(rows) - hits
    table = Table(title="Grizz DB lookup", show_header=True, header_style="bold")
    table.add_column("Result")
    table.add_column("Rows", justify="right")
    table.add_column("%", justify="right")
    table.add_row("matched", str(hits), f"{100 * hits / len(rows):.1f}%")
    table.add_row("no match", str(misses), f"{100 * misses / len(rows):.1f}%")
    console.print(table)
    console.print(f"Distinct companies matched: {len(companies)}  |  "
                  f"match_via: {via or '—'}  |  {time.time() - t0:.0f}s")

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for row in results:
                fh.write(json.dumps(row) + "\n")
        console.print(f"  per-record results -> [bold]{out}[/bold]")
    if companies_out:
        companies_out.parent.mkdir(parents=True, exist_ok=True)
        companies_out.write_text(json.dumps(companies, indent=2), encoding="utf-8")
        console.print(f"  matched company payloads -> [bold]{companies_out}[/bold]")
    if not out and not companies_out:
        console.print("[dim]Tip: pass --out / --companies to persist the results.[/dim]")


def _companies_from_gids(api_key: str, gids: list[str]) -> list[dict]:
    """Resolve gid_company values to Grizz company dicts via the read-only,
    no-credit lookup-batch endpoint, normalized to the audience-result shape
    that the downstream sync expects (hq_phone/hq_email -> phone/email)."""
    companies: list[dict] = []
    unmatched = 0
    for m in grizz_client.lookup_batch(api_key, [{"gid_company": g} for g in gids]):
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


def _read_lookup_results(path: Path) -> list[dict]:
    """Read the per-record JSONL that `lookup --out` writes.  Loud on anything else."""
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{path} line {n} is not valid JSON ({e}). `write --results` takes "
                f"the JSONL that `lookup --out` writes, one JSON object per line."
            ) from e
        if not isinstance(row, dict):
            raise ValueError(f"{path} line {n} is not a JSON object.")
        rows.append(row)
    return rows


def run_write(
    crm: str,
    config_path: Path,
    results_path: Path,
    companies_path: Path | None,
    dry_run: bool,
    batch_size: int,
) -> None:
    """Write lookup hits back to the CRM records they came from — keyed by id.

    The sink for `lookup`: it consumes `--out` (and optionally `--companies`) and
    updates each record by the `crm_record_id` the caller supplied, so the write
    lands on exactly the record that was looked up.  That is the difference from
    `audience --gids`, which re-matches by domain string search and can therefore
    miss a live account (`www.foo.com` vs `foo.com`) and create a duplicate.

    Update-only by design.  A row in a lookup result is a record that already
    exists in the CRM — there is nothing here to create, so nothing here can
    duplicate.  Only grizz_* fields are written; native fields are never touched.
    """
    config = load_config(config_path)
    crm_config = config.get(crm)
    if not crm_config:
        console.print(f"[red]No '{crm}' section found in {config_path}.[/red]")
        raise typer.Exit(1)
    field_mapping: dict = crm_config["field_mapping"]

    api_key = os.environ.get("GRIZZ_API_KEY")
    if not api_key:
        console.print("[red]GRIZZ_API_KEY is not set.[/red] Add it to your [bold].env[/bold] file.")
        raise typer.Exit(1)
    if not results_path.exists():
        console.print(f"[red]Results file not found: {results_path}[/red]")
        raise typer.Exit(1)

    try:
        results = _read_lookup_results(results_path)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if not results:
        console.print(f"[red]No rows found in {results_path}.[/red]")
        raise typer.Exit(1)

    hits = [r for r in results if r.get("matched")]
    no_id = [r for r in hits if not str(r.get("record_id") or "").strip()]
    writable, seen_ids = [], set()
    dupes = 0
    for r in hits:
        rid = str(r.get("record_id") or "").strip()
        if not rid:
            continue
        if rid in seen_ids:
            dupes += 1
            continue
        seen_ids.add(rid)
        writable.append(r)

    console.print(f"Read [bold]{len(results)}[/bold] lookup result(s): "
                  f"[green]{len(hits)} matched[/green], "
                  f"{len(results) - len(hits)} no match.")
    if no_id:
        console.print(
            f"  [yellow]{len(no_id)} matched row(s) carry no record_id and cannot be "
            f"written by id — re-run `lookup` with a crm_record_id on each input "
            f"row, or push those gids with `audience --gids`.[/yellow]"
        )
    if dupes:
        console.print(f"  [dim]{dupes} duplicate record_id row(s) collapsed.[/dim]")
    if not writable:
        console.print("[red]Nothing to write.[/red]")
        raise typer.Exit(1)

    # ── Company payloads ────────────────────────────────────────────────────
    # `lookup --companies` keys on gid_company; without that file the payloads are
    # re-fetched from the same read-only endpoint `lookup` used — free, no credits.
    payloads: dict[str, dict] = {}

    def index(company: dict) -> None:
        for key in (company.get("gid_company"), clean_domain(company.get("domain"))):
            if key:
                payloads.setdefault(key, company)

    if companies_path:
        if not companies_path.exists():
            console.print(f"[red]Companies file not found: {companies_path}[/red]")
            raise typer.Exit(1)
        try:
            loaded = json.loads(companies_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            console.print(f"[red]{companies_path} is not valid JSON ({e}).[/red]")
            raise typer.Exit(1)
        if not isinstance(loaded, dict):
            console.print(f"[red]{companies_path} must be the JSON object "
                          f"`lookup --companies` writes (keyed by gid_company).[/red]")
            raise typer.Exit(1)
        for key, company in loaded.items():
            if isinstance(company, dict):
                payloads.setdefault(key, company)
                index(company)
    else:
        gids = sorted({str(r.get("gid_company")).strip() for r in writable
                       if r.get("gid_company")})
        console.print(f"No --companies file: resolving {len(gids)} company payload(s) "
                      f"from Grizz (read-only, no credits)...", end=" ")
        try:
            for company in _companies_from_gids(api_key, gids):
                index(company)
        except Exception as e:
            console.print(f"\n[red]Could not resolve company payloads: {e}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]{len(payloads)} key(s).[/green]")

    # ── Build the id-keyed update records ───────────────────────────────────
    records, unresolved = [], []
    for r in writable:
        company = (payloads.get(str(r.get("gid_company") or "").strip())
                   or payloads.get(r.get("clean_domain") or clean_domain(r.get("domain")) or ""))
        if not company:
            unresolved.append(r)
            continue
        mapped = apply_mapping(_grizz_data(company), field_mapping)
        if not mapped:
            unresolved.append(r)
            continue
        records.append({"Id": str(r["record_id"]).strip(), **mapped})

    if unresolved:
        console.print(
            f"  [yellow]{len(unresolved)} matched row(s) had no company payload to "
            f"write (e.g. {unresolved[0].get('domain') or unresolved[0].get('record_id')}) "
            f"— skipped.[/yellow]"
        )
    if not records:
        console.print("[red]Nothing to write after mapping — check the field_mapping "
                      f"in {config_path}.[/red]")
        raise typer.Exit(1)

    console.print(f"\nWriting [bold]{len(records)}[/bold] record(s) to {crm.title()} "
                  f"by record id.")
    if dry_run:
        console.print("[yellow]Dry run mode — CRM will not be updated.[/yellow]")
        for r in records[:10]:
            console.print(f"  [dim]{r['Id']}: would update "
                          f"{[k for k in r if k != 'Id']}[/dim]")
        if len(records) > 10:
            console.print(f"  [dim]... and {len(records) - 10} more.[/dim]")
        return

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
    if crm == "attio":
        adapter.set_phone_slugs(_attio_phone_slugs(crm_config))

    updated, failed = _run_update_batches(
        adapter, records, batch_size, console, interactive=sys.stdin.isatty(),
    )

    table = Table(title="Lookup write", show_header=True, header_style="bold")
    table.add_column("Result")
    table.add_column("Records", justify="right")
    table.add_row("updated", str(updated))
    table.add_row("failed", str(len(failed)))
    table.add_row("skipped (no record_id)", str(len(no_id)))
    table.add_row("skipped (no payload)", str(len(unresolved)))
    console.print()
    console.print(table)
    if failed:
        raise typer.Exit(1)


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
    CI), failed batches are retried once automatically so the run never blocks on
    a missing terminal.  Creating unmatched companies is deliberately NOT part of
    that: it requires an explicit `--yes`, never a merely-absent TTY.  A company
    lands in `unmatched` when the CRM lookup *failed to find* it, which is not the
    same claim as "it isn't there" — an exact-string domain search misses
    `www.foo.com` when Grizz holds `foo.com`.  Auto-creating on that would write
    duplicates of live accounts, unprompted, in exactly the runs nobody is
    watching, so the destructive branch fails closed.
    """
    non_interactive = assume_yes or not sys.stdin.isatty()
    # Never inferred from the absence of a terminal — only an explicit flag.
    create_unmatched = assume_yes

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
    if crm == "attio":
        adapter.set_phone_slugs(_attio_phone_slugs(crm_config))

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
        # Never fall through to an empty match map.  Every company would then look
        # unmatched and the run would create duplicates of records that already
        # exist — the failure mode is far worse than not running at all.
        console.print(f"  [red]Bulk lookup failed: {e}[/red]")
        console.print(
            "  [red]Aborting — with no match data every company would be treated as\n"
            "  new and re-created as a duplicate. If this was a rate limit (429),\n"
            "  check that no other push is running against this CRM: pushes must be\n"
            "  run one at a time. Wait for the limit to clear and re-run.[/red]"
        )
        raise typer.Exit(1)

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
            mapped = apply_mapping(_grizz_data(company), field_mapping)
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
            create = create_unmatched
            if create:
                console.print(
                    f"{len(unmatched)} unmatched compan(ies) — creating new records "
                    f"(--yes)."
                )
            else:
                console.print(
                    f"[yellow]{len(unmatched)} unmatched compan(ies) — not created "
                    f"(no terminal to confirm at). Re-run with --yes if they really "
                    f"are new; check for domain mismatches first.[/yellow]"
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
                mapped = apply_mapping(_grizz_data(company), field_mapping)
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
    # Key both sides on the CLEANED domain: the client normalizes before sending,
    # so the echoed `input.domain` is clean while `r["domain"]` is the raw CSV
    # value.  Keying on the raw value would miss precisely the dirty domains
    # (URLs, `www.`, trailing paths) that normalization exists to rescue.
    matches = grizz_client.lookup_batch(
        api_key, [{"domain": r["domain"]} for r in need])
    by_domain = {}
    for m in matches:
        d = clean_domain(((m or {}).get("input") or {}).get("domain"))
        if d:
            by_domain[d] = m
    for r in need:
        comp = (by_domain.get(clean_domain(r["domain"]) or "") or {}).get("company") or {}
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
        console.print("[yellow]Those companies were NOT searched. checked.json lists every input "
                      "company, so treat it as the roster — not as proof of coverage.[/yellow]")
    console.print(f"\nWrote:\n  {contacts_path}  (per-contact, keyed to each CRM record)"
                  f"\n  {checked_path}   (coverage roster — every company checked, found or not)"
                  f"\n  {csv_path}  (human review)")
    console.print("\ncontacts.json + checked.json are the discovery hand-off; feed them to "
                  "your contact-log loader before the paid email/phone enrich step.")

    # Exit non-zero when any batch didn't finish. Previously this printed a
    # warning and exited 0, so an automated caller saw success: "Done.", a full
    # checked.json roster, and no contacts — indistinguishable from "these
    # companies genuinely have no contacts". Fifteen consecutive chunks were
    # recorded as processed that way. Partial output is still written and
    # --resume still works; only the exit status changes.
    if failed_batches:
        raise typer.Exit(1)


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


# ── Attio contact sync (client-side, mirrors the company audience path) ─────────

def _attio_phone_slugs(crm_config: dict) -> set[str]:
    """Config-declared phone-number slugs (`field_types: <slug>: phone-number`),
    so the Attio adapter E.164-normalizes ANY slug the config types as a phone —
    no slug list is hardcoded in the package."""
    field_types = (crm_config or {}).get("field_types") or {}
    return {slug for slug, typ in field_types.items()
            if str(typ).strip().lower() == "phone-number"}


def _read_person_records(path: Path) -> list[dict]:
    """Read FULL person records (not just gids) from a `people discover`
    contacts.json.  The client-side Attio contact write puts name / title /
    location / linkedin on the record, so it needs the whole member dict — a
    bare gid list carries none of that."""
    if path.suffix.lower() != ".json":
        console.print("[red]Attio contact sync needs the rich contacts.json from "
                      "`people discover`[/red] (name/title/location live there), "
                      "not a gid list.")
        raise typer.Exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        console.print(f"[red]{path} is not a contacts.json array.[/red]")
        raise typer.Exit(1)
    out: list[dict] = []
    seen: set[str] = set()
    for c in data:
        if not isinstance(c, dict):
            continue
        gid = (c.get("gid") or c.get("gid_person") or "").strip()
        key = gid or (c.get("linkedin_url") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _person_name(p: dict) -> dict | None:
    """Build Attio's personal-name object from a discovered person, or None when
    there's no name to write."""
    first = (p.get("first_name") or "").strip()
    last = (p.get("last_name") or "").strip()
    full = " ".join(x for x in (first, last) if x).strip()
    if not full:
        return None
    return {"first_name": first, "last_name": last, "full_name": full}


def _enrich_people_map(api_key: str, gids: list[str], enrich_email: bool, enrich_phone: bool,
                       batch_size: int, poll_interval: int, poll_timeout: int) -> dict:
    """Enrich email/phone in chunks and RETURN {gid: {'email':.., 'phone':..}} so
    the client-side Attio write can put fresh email/phone on the person record.

    Same settle-aware, chunked polling as the server-path enrich — only values
    from settled chunks are returned; an unsettled chunk is called out for a
    re-run (already-entitled contacts are never re-charged)."""
    want = ", ".join(f for f, on in (("email", enrich_email), ("phone", enrich_phone)) if on)
    n_batches = (len(gids) + batch_size - 1) // batch_size
    console.print(f"Enriching [bold]{want}[/bold] for {len(gids)} contact(s) in "
                  f"{n_batches} chunk(s) of {batch_size} (spends credits).")
    out: dict[str, dict] = {}
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
        settled = bool(final.get("settled")) or final.get("status") == "COMPLETE"
        for r in (final.get("results") or []):
            g = r.get("gid")
            if not g:
                continue
            got = out.setdefault(str(g), {})
            if r.get("email"):
                got["email"] = r["email"]
            if r.get("phone"):
                got["phone"] = r["phone"]
        if settled:
            console.print(f"[green]complete[/green] — found_email={final.get('found_email', 0)} "
                          f"found_phone={final.get('found_phone', 0)}")
        else:
            incomplete += 1
            console.print("[yellow]still running — re-run to settle.[/yellow]")
    if incomplete:
        console.print(f"[yellow]{incomplete} chunk(s) did not settle[/yellow] — re-run the same "
                      "command later; already-entitled contacts are free.")
    return out


def _attio_person_input(p: dict) -> dict:
    """Shape a discovered person into the prepare-writes input record."""
    return {
        "gid_person":    p.get("gid"),
        "primary_email": p.get("email"),
        "linkedin_url":  p.get("linkedin_url"),
        "first_name":    p.get("first_name"),
        "last_name":     p.get("last_name"),
        "title":         p.get("title"),
        "gid_company":   p.get("company_gid"),
    }


def _attio_parent_rid(p: dict, parent_by_gid: dict) -> str:
    """The Attio company record id a contact links to (explicit crm id, else
    resolved from its gid_company); '' if unresolved."""
    return ((p.get("crm_company_record_id") or "").strip()
            or parent_by_gid.get(str(p.get("company_gid"))) or "")


def _match_attio_people(adapter, api_key: str, records: list[dict],
                        parent_by_gid: dict, fuzzy_threshold: float) -> list[tuple]:
    """Decide per person whether to update an existing Attio contact or create a
    new one, mirroring the server's dedup in two layers:

      1. GLOBAL strong keys (grizz_person_id, email) — safe across account moves
         and prevents re-run duplicates.
      2. ACCOUNT-SCOPED cascade via the server's prepare-writes endpoint (the SAME
         grizz_person_id → email → linkedin → fuzzy-name logic HubSpot/Salesforce
         use) for anyone the strong keys didn't catch — matching is bounded to the
         parent company's own contacts, so people at different companies can't
         collide on a name.

    Returns a list of (kind, record_id, match_via, confidence) 1:1 with `records`;
    kind is 'update' | 'create' | 'review' (a fuzzy match below fuzzy_threshold —
    surfaced for manual review, never silently merged)."""
    n = len(records)
    result: list = [None] * n

    # Phase 1 — global strong-key match (grizz_person_id, native email)
    try:
        strong = adapter.find_people_bulk(records)
    except Exception as e:
        console.print(f"  [yellow]strong-key lookup failed ({e}); relying on the "
                      f"account-scoped cascade.[/yellow]")
        strong = {}

    groups: dict[str, list[int]] = {}
    for i, p in enumerate(records):
        gid = str(p.get("gid") or "")
        email = (p.get("email") or "").strip().lower()
        rid = (strong.get(gid) if gid else None) or (strong.get(email) if email else None)
        if rid:
            result[i] = ("update", rid, "strong", 1.0)
        elif _attio_parent_rid(p, parent_by_gid):
            groups.setdefault(_attio_parent_rid(p, parent_by_gid), []).append(i)
        else:
            result[i] = ("create", None, None, None)   # no strong match, no parent

    # Phase 2 — account-scoped prepare-writes for the rest
    for prid, idxs in groups.items():
        try:
            cands = adapter.fetch_people_by_company(prid)
        except Exception as e:
            console.print(f"  [yellow]could not read a company's existing contacts "
                          f"({e}); its unmatched contacts will be created new.[/yellow]")
            for i in idxs:
                result[i] = ("create", None, None, None)
            continue
        for j in range(0, len(idxs), 200):      # prepare-writes caps at 200/call
            chunk = idxs[j:j + 200]
            try:
                resp = grizz_client.prepare_contact_writes(
                    api_key, "attio", [_attio_person_input(records[i]) for i in chunk], cands)
                rows = resp.get("records", [])
            except Exception as e:
                console.print(f"  [yellow]prepare-writes failed ({e}); that account's "
                              f"contacts will be created new.[/yellow]")
                rows = []
            for k, i in enumerate(chunk):
                r = rows[k] if k < len(rows) else {}
                cm = r.get("crm_match") if r.get("matched") else None
                if cm and cm.get("record_id"):
                    via = cm.get("match_via")
                    conf = cm.get("confidence") or 0.0
                    if via == "fuzzy" and conf < fuzzy_threshold:
                        result[i] = ("review", cm["record_id"], via, conf)
                    else:
                        result[i] = ("update", cm["record_id"], via, conf)
                else:
                    result[i] = ("create", None, None, None)
    return result


def _resolve_attio_parent_companies(records: list[dict]) -> dict:
    """Resolve company_gid → Attio company record_id for people whose input row
    didn't already carry a crm_company_record_id, so the person can still be
    linked to its parent company.  Best-effort — a company not in Attio just
    means that person syncs without the link."""
    need = [str(r.get("company_gid")) for r in records
            if r.get("company_gid") and not (r.get("crm_company_record_id") or "").strip()]
    if not need:
        return {}
    ca = AttioAdapter()
    try:
        ca.connect()
        return ca.gid_to_record_ids(need)
    except Exception as e:
        console.print(f"  [yellow]Could not resolve parent companies ({e}); "
                      "affected contacts sync without the company link.[/yellow]")
        return {}


def _write_people_batches(adapter, records: list[dict], kind: str, batch_size: int) -> int:
    """Write person records (create or update) in batches, reporting per batch.
    The adapter parallelizes each batch internally (Attio has no batch-write
    API); batching here just keeps the console output readable."""
    if not records:
        return 0
    fn = adapter.update_people if kind == "update" else adapter.create_people
    total = len(records)
    ok = 0
    for bs in range(0, total, batch_size):
        batch = records[bs:bs + batch_size]
        be = min(bs + batch_size, total)
        console.print(f"  {kind} {bs + 1}–{be} of {total}...", end=" ")
        results = fn(batch)
        b_ok = sum(1 for r in results if r.get("success"))
        ok += b_ok
        b_fail = len(results) - b_ok
        if b_fail:
            console.print(f"[green]{b_ok} ok[/green], [red]{b_fail} failed.[/red]")
            for r in results:
                if not r.get("success"):
                    console.print(f"    [red]{r.get('errors')}[/red]")
        else:
            console.print(f"[green]{b_ok} ok.[/green]")
    return ok


def run_attio_people_sync(config_path: Path, contacts_path: Path,
                          enrich_email: bool, enrich_phone: bool, enrich_batch_size: int,
                          poll_interval: int, poll_timeout: int,
                          batch_size: int, dry_run: bool, assume_yes: bool,
                          fuzzy_threshold: float = 0.9) -> None:
    """Push discovered contacts into Attio via the SAME client-side model as the
    company `audience` path: a local Attio REST adapter, config.yaml-driven, that
    collapses city/state/country into ONE object-typed location attribute.

    Dedup mirrors the HubSpot/Salesforce server path: a global strong-key match
    (grizz_person_id, email) plus an ACCOUNT-SCOPED cascade run through the
    server's prepare-writes endpoint — the SAME grizz_person_id → email → linkedin
    → fuzzy-name logic — so an existing contact is updated in place, not
    duplicated. Per the Grizz write principle it writes ONLY the grizz_contact_*
    attributes on an existing contact; the native name/email/phone and the
    parent-company link are seeded ONLY when creating a new person. Fuzzy matches
    below `fuzzy_threshold` are surfaced for review, never silently merged."""
    config = load_config(config_path)
    cc = config.get("attio_contacts")
    if not cc or not cc.get("field_mapping"):
        console.print("[red]No 'attio_contacts' section in your config.[/red] Copy it from "
                      "[bold]config.example.yaml[/bold] and set your People attribute slugs.")
        raise typer.Exit(1)
    field_mapping: dict = cc["field_mapping"]
    native = {
        "name":        cc.get("name_field", "name"),
        "email":       cc.get("email_field", "email_addresses"),
        "phone":       cc.get("phone_field", "phone_numbers"),
        "company_ref": cc.get("company_ref", "company"),
    }
    person_id_slug = field_mapping.get("gid")

    # Grizz key is required — dedup matching runs server-side (prepare-writes).
    api_key = os.environ.get("GRIZZ_API_KEY")
    if not api_key:
        console.print("[red]GRIZZ_API_KEY is not set.[/red] Needed for contact dedup "
                      "matching (and enrichment). Add it to your [bold].env[/bold].")
        raise typer.Exit(1)

    records = _read_person_records(contacts_path)
    if not records:
        console.print(f"[red]No contacts found in {contacts_path}.[/red]")
        raise typer.Exit(1)
    console.print(f"Loaded [bold]{len(records)}[/bold] contact(s) from {contacts_path}; "
                  f"CRM=[bold]attio[/bold] (client-side write via ATTIO_API_KEY).")

    # ── optional enrichment (spends credits) ─────────────────────────────────
    if enrich_email or enrich_phone:
        gids = [str(r.get("gid")) for r in records if r.get("gid")]
        enriched = _enrich_people_map(api_key, gids, enrich_email, enrich_phone,
                                      enrich_batch_size, poll_interval, poll_timeout)
        for r in records:
            got = enriched.get(str(r.get("gid"))) or {}
            if got.get("email"):
                r["email"] = got["email"]
            if got.get("phone"):
                r["phone"] = got["phone"]

    # ── connect the People adapter ───────────────────────────────────────────
    adapter = AttioContactAdapter()
    console.print("Connecting to Attio...", end=" ")
    try:
        adapter.connect()
    except Exception as e:
        console.print(f"\n[red]Connection failed: {e}[/red]")
        raise typer.Exit(1)
    adapter.use_native_slugs(name=native["name"], email=native["email"],
                             phone=native["phone"], company_ref=native["company_ref"],
                             person_id=person_id_slug, last_sync=cc.get("last_sync_field"),
                             phone_slugs=_attio_phone_slugs(cc),
                             linkedin=field_mapping.get("linkedin_url"))
    console.print("[green]connected.[/green]")

    # ── resolve parent-company record ids (for scoping + the company link) ───
    parent_by_gid = _resolve_attio_parent_companies(records)

    # ── match: global strong keys + account-scoped cascade (prepare-writes) ──
    console.print(f"Matching {len(records)} contact(s) — strong keys + account-scoped "
                  f"cascade (grizz_person_id → email → linkedin → name)...")
    decisions = _match_attio_people(adapter, api_key, records, parent_by_gid, fuzzy_threshold)

    to_update: list[dict] = []
    to_create: list[dict] = []
    review: list[tuple] = []
    linked = 0
    via_counts: dict[str, int] = {}
    for i, p in enumerate(records):
        kind, rid, via, conf = decisions[i]
        mapped = apply_mapping(p, field_mapping)
        if kind == "update":
            # Existing contact — write ONLY grizz_* attributes (never touch native
            # name/email/phone/company on a record Grizz didn't create).
            to_update.append({"Id": rid, **mapped})
            via_counts[via] = via_counts.get(via, 0) + 1
        elif kind == "review":
            review.append((p, rid, conf))   # possible (fuzzy) dup — do NOT merge
        else:
            # New record — seed the native fields too (name/email/phone + parent link).
            company_rid = _attio_parent_rid(p, parent_by_gid)
            if company_rid:
                linked += 1
            to_create.append({
                "_native": {
                    "name":              _person_name(p),
                    "email":             p.get("email"),
                    "phone":             p.get("phone"),
                    "company_record_id": company_rid or None,
                },
                **mapped,
            })

    via_note = ", ".join(f"{k}:{v}" for k, v in via_counts.items()) or "—"
    console.print(f"  [green]{len(to_update)} matched[/green] ({via_note})  "
                  f"[yellow]{len(to_create)} new[/yellow] (parent linked: {linked})"
                  + (f"  [magenta]{len(review)} fuzzy→review[/magenta]" if review else ""))
    if review:
        console.print(f"  [magenta]{len(review)} possible (fuzzy) match(es) below "
                      f"--fuzzy-threshold {fuzzy_threshold} — NOT written[/magenta] "
                      f"(merge in Attio, or lower the threshold to accept):")
        for p, rid, conf in review[:10]:
            nm = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            console.print(f"    [dim]{nm or p.get('gid')} → existing {rid} (conf {conf:.2f})[/dim]")
    console.print()

    if dry_run:
        for r in (to_update[:20] + to_create[:20]):
            kind = "update" if r.get("Id") else "create"
            console.print(f"  [dim]{kind}: {[k for k in r if k not in ('Id', '_native')]}[/dim]")
        console.print(f"[yellow]Dry run — {len(to_update)} update / {len(to_create)} create / "
                      f"{len(review)} review; not writing to Attio.[/yellow]")
        return

    updated = _write_people_batches(adapter, to_update, "update", batch_size)
    created = _write_people_batches(adapter, to_create, "create", batch_size)
    console.print(f"\n[bold green]Done.[/bold green] updated={updated}  created={created}  "
                  f"parent_linked={linked}  fuzzy_review={len(review)}")


@people_app.command("sync")
def people_sync(
    crm: str = typer.Option(..., "--crm", help="hubspot, salesforce, or attio"),
    contacts: Path = typer.Option(..., "--contacts", help="contacts.json from `people discover` (required for attio); or a CSV/txt of person gids (hubspot/salesforce)"),
    config: Path = typer.Option(Path("config.yaml"), "--config", help="Path to your config.yaml (attio only — reads the attio_contacts mapping)."),
    enrich_email: bool = typer.Option(False, "--enrich-email", help="Enrich work email before sync (spends credits)"),
    enrich_phone: bool = typer.Option(False, "--enrich-phone", help="Enrich phone before sync (spends credits)"),
    enrich_batch_size: int = typer.Option(50, "--enrich-batch-size", help="Contacts per enrich chunk (<=50 keeps each chunk inside the poll window)."),
    poll_interval: int = typer.Option(5, "--poll-interval", help="Seconds between status polls."),
    poll_timeout: int = typer.Option(900, "--poll-timeout", help="Soft per-chunk settle wait (seconds). NOT a correctness/charging boundary — a chunk that doesn't settle is reported as unsettled and safely finished by re-running the same command (already-entitled contacts are never re-charged). Raise it if chunks routinely don't settle (upstream retries under rate limit can exceed the default); don't lower it below ~600 or you'll churn re-runs."),
    batch_size: int = typer.Option(100, "--batch-size", help="Person records per write batch (attio only; the adapter parallelizes each batch)."),
    fuzzy_threshold: float = typer.Option(0.9, "--fuzzy-threshold", help="Attio only. Min confidence to auto-merge a FUZZY name match (grizz_person_id/email/linkedin matches always merge). Fuzzy matches below this are surfaced for review, not written. Set 0 to accept all, 1.01 to never auto-merge a fuzzy match."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve + count only; do not write to the CRM."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non-interactive (attio only)."),
):
    """Push discovered contacts to your CRM — create-OR-update, never duplicating.

    HubSpot / Salesforce go through the server `/people/create-crm/` endpoint,
    which matches each contact against its parent account's existing CRM contacts
    (gid_person → email → linkedin → name).  Attio is headless, so it syncs
    CLIENT-SIDE via the local Attio REST adapter — the same model as the company
    `audience` flow — but reuses the SAME server dedup cascade via prepare-writes
    (global strong keys + an account-scoped grizz_person_id → email → linkedin →
    fuzzy-name match), and collapses city/state/country into ONE object-typed
    location attribute. Discovery is free; email/phone enrichment
    (--enrich-email/--enrich-phone) is a separate, paid step.
    """
    if not contacts.exists():
        console.print(f"[red]Contacts file not found: {contacts}[/red]")
        raise typer.Exit(1)
    crm = crm.strip().lower()
    enrich_batch_size = max(1, min(enrich_batch_size, 100))
    if crm in CONTACT_ADAPTERS:   # attio — client-side, config-driven People write
        run_attio_people_sync(config, contacts, enrich_email, enrich_phone,
                              enrich_batch_size, poll_interval, poll_timeout,
                              max(1, min(batch_size, 200)), dry_run, yes,
                              fuzzy_threshold=fuzzy_threshold)
        return
    run_people_sync(crm, contacts, enrich_email, enrich_phone, enrich_batch_size,
                    poll_interval, poll_timeout, dry_run)


@app.command()
def enrich(
    crm: str = typer.Option(..., help=f"CRM to use. Available: {', '.join(ADAPTERS)}"),
    input: Path = typer.Option(..., help="CSV file containing account IDs (requires an 'Id' column)"),
    config: Path = typer.Option(Path("config.yaml"), help="Path to your config.yaml"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without updating the CRM"),
    concurrency: int = typer.Option(12, "--concurrency", help="Parallel enrich+write workers (these CRMs have no batch-write API, so throughput comes from concurrency). Ignored in --dry-run."),
):
    """Enrich CRM accounts from a CSV of account IDs using the Grizz API."""
    if crm not in ADAPTERS:
        console.print(f"[red]Unknown CRM '{crm}'. Available: {', '.join(ADAPTERS)}[/red]")
        raise typer.Exit(1)
    run_enrich(crm, input, config, dry_run, concurrency)


# ── Interactive menu ───────────────────────────────────────────────────────────

@app.command()
def audience(
    crm: str = typer.Option("salesforce", help=f"CRM to use. Available: {', '.join(ADAPTERS)}"),
    audience_id: Optional[str] = typer.Option(None, "--audience-id", help="Existing Grizz audience ID"),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Prompt to create a new audience"),
    gids: Optional[Path] = typer.Option(None, "--gids", help="File of gid_company values (one per line or a CSV with a gid_company column) — e.g. a filtered selection from the Grizz MCP"),
    config: Path = typer.Option(Path("config.yaml"), help="Path to your config.yaml"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing to CRM"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Create unmatched companies without prompting. Required for creation in any non-TTY run — a missing terminal alone never creates."),
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
        try:
            gid_list = _read_gids(gids)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        if not gid_list:
            console.print(f"[red]No gid_company values found in {gids}.[/red]")
            raise typer.Exit(1)

    batch_size = max(1, min(batch_size, 200))
    run_audience(crm, config, dry_run, audience_id=audience_id, prompt=prompt,
                 batch_size=batch_size, gids=gid_list, assume_yes=yes)


@app.command()
def lookup(
    input_path: Path = typer.Option(..., "--input", "-i", help="Companies to look up: a .jsonl ({domain, crm_record_id}), a CSV with a domain/gid_company column, or a plain one-per-line domain list"),
    out: Optional[Path] = typer.Option(None, "--out", help="Write per-record results as JSONL (matched, match_via, gid_company, ...)"),
    companies_out: Optional[Path] = typer.Option(None, "--companies", help="Write matched company payloads as JSON, keyed by gid_company"),
):
    """Look up companies in Grizz's database — read-only, no scrape, no credits.

    The cheap first pass over a large backlog: it reports which companies Grizz
    already knows, so you only spend `enrich` on the ones it doesn't.  Domains
    are normalized before matching (the API does not do this for you), which is
    worth ~0.3-0.7% of a typical CRM export.
    """
    run_lookup(input_path, out, companies_out)


@app.command()
def write(
    crm: str = typer.Option("salesforce", help="CRM to write to"),
    results: Path = typer.Option(..., "--results", "-r", help="The per-record JSONL that `lookup --out` wrote"),
    companies: Optional[Path] = typer.Option(None, "--companies", help="The company payloads that `lookup --companies` wrote. Omit to re-fetch them from Grizz (read-only, no credits)."),
    config: Path = typer.Option(Path("config.yaml"), "--config", help="Path to your config.yaml"),
    batch_size: int = typer.Option(200, "--batch-size", help="Records per CRM batch write (max 200)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing to the CRM"),
):
    """Write lookup hits back to the CRM records they came from — keyed by record id.

    The sink for `lookup`: run `lookup --input backlog.jsonl --out hits.jsonl`,
    then `write --results hits.jsonl` to push what Grizz already knew onto those
    exact records.  Update-only — every row is a record that already exists, so
    this path cannot create a duplicate, and it spends no credits.

    Each input row needs a `crm_record_id` for its record id to round-trip; rows
    without one are reported and skipped rather than guessed at.
    """
    run_write(crm, config, results, companies, dry_run, max(1, min(batch_size, 200)))


def _run_setup(crm: str, contacts: bool, dry_run: bool, config_path: Path) -> None:
    """Route to the company or contact setup for the chosen CRM.  The Salesforce/
    HubSpot contact setups provision exactly the grizz_contact_* properties
    create-crm writes — identical to what the `setup_crm_contacts` MCP tool
    creates.  Attio is CONFIG-DRIVEN: it provisions only the attributes mapped in
    config.yaml, on just the one object requested, so contact setup never touches
    the Companies object (and vice versa)."""
    if crm == "salesforce":
        (run_setup_salesforce_contacts if contacts else run_setup_salesforce)(dry_run=dry_run)
    elif crm == "hubspot":
        (run_setup_hubspot_contacts if contacts else run_setup_hubspot)(dry_run=dry_run)
    elif crm == "attio":
        run_setup_attio(dry_run=dry_run,
                        object_="contacts" if contacts else "company",
                        config_path=config_path)
    else:
        console.print(f"[red]Setup not yet available for '{crm}'.[/red]")
        raise typer.Exit(1)


@app.command()
def setup(
    crm: str = typer.Option("salesforce", help="CRM to set up"),
    object_: str = typer.Option("company", "--object", help="Which records to set up: 'company' or 'contacts'"),
    config: Path = typer.Option(Path("config.yaml"), "--config", help="Path to your config.yaml (Attio reads its slugs from here)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without creating fields"),
):
    """Create the Grizz custom fields in your CRM (company or contact records).

    For Attio this reads config.yaml and creates only the attributes you map —
    so it never duplicates fields you already built or renamed, and `--object
    contacts` provisions the People object without touching Companies.
    """
    obj = object_.strip().lower()
    if obj not in ("company", "companies", "contact", "contacts"):
        console.print(f"[red]--object must be 'company' or 'contacts' (got '{object_}').[/red]")
        raise typer.Exit(1)
    try:
        _run_setup(crm, contacts=obj in ("contact", "contacts"), dry_run=dry_run,
                   config_path=config)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
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
        try:
            _run_setup(crm, contacts=(which == "Contact fields"),
                       dry_run=bool(dry_run), config_path=Path("config.yaml"))
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
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
