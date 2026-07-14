# Changelog

All notable changes to this project will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
- `setup --object contacts` — provision the Grizz Contact fields on HubSpot /
  Salesforce Contact records (the company setup is `--object company`, the
  default). Ported from the MCP `setup_crm_contacts` tool with the identical
  property set, so CLI setup and MCP setup produce the same portal — and since
  `create-crm` writes these `grizz_contact_*` properties, they must exist before
  `people sync` (a missing property makes contacts/batch/create 400). Uses the
  same `HUBSPOT_API_KEY` / `SALESFORCE_*` env as the sync, so setup and sync
  always target the same portal. The interactive menu now asks Company vs
  Contact fields.
- `people sync` command — enrich (optional) + push discovered contacts to the
  CRM via the same server-side `/people/create-crm/` endpoint the MCP uses, which
  matches each contact against its parent account's existing CRM contacts
  (gid_person → email → linkedin → name) and UPDATES in place rather than
  duplicating. Takes a `people discover` `contacts.json` (or a CSV/txt of person
  gids), packages the customer CRM key from `.env` into the request-body
  `credentials` (`HUBSPOT_API_KEY` → `hubspot_key`; `SALESFORCE_SESSION_ID` +
  `SALESFORCE_INSTANCE_URL`), batches at 100, polls each write to completion, and
  reports created/updated/no_grizz_match/no_parent_match/parent_linked/errors.
  `--enrich-email`/`--enrich-phone` (off by default) spend credits; `--dry-run`
  resolves + counts without writing.
- `people discover` command — local path for contact (people) discovery over a
  company list, so discovery no longer has to run through MCP/chat (which timed
  out on large batches). Takes a CSV (`record_id` + `gid_company` and/or
  `domain`), keys discovery on each company's Grizz `gid_company` (domain-only
  rows are resolved to one first via a free cascade lookup, which also collapses
  franchise/redirect domains onto the canonical company), batches into people
  audiences (default 50/batch), polls to completion, and pages members back.
  Emits `contacts.json` (each contact joined back to its CRM `record_id`) +
  `checked.json` (a coverage roster of every company checked, found or not) plus
  a review CSV. Resumable (`--resume`) and persists after each batch. Discovery
  is free — email/phone enrichment stays a separate, paid step.
- `grizz_client`: `create_people_audience`, `get_people_audience`, and
  `get_people_audience_members` wrap the `/api/v1/people/audiences/` submit →
  poll → members cycle (pair of the `build_people_audience` MCP tool).

### Fixed
- HubSpot setup: dropped `hasUniqueValue` from the `grizz_company_id` company
  property (and the "Unique" wording in its description). HubSpot enforces
  `hasUniqueValue` at write time, but client CRMs hold duplicate company records
  that legitimately resolve to one Grizz company, so many records share a
  `grizz_company_id`. The unique constraint 409'd every write after the first in
  a duplicate cluster and — because record writes are atomic — that failure also
  blocked the record's other `grizz_*` fields, breaking enrichment of exactly the
  duplicate population we care about. Uniqueness isn't needed for matching.
  (Salesforce uses `externalId` without a `unique` flag and Attio creates every
  attribute `is_unique=False`, so neither is affected.)
- Attio adapter resilience: a read timeout or connection drop on a single write no
  longer fails the whole batch. Writes (and reads) now retry transient failures —
  timeout, connection drop, 429 (honoring Retry-After), and 5xx — with backoff,
  and per-record errors are isolated (one bad record never aborts the rest). The
  write timeout was also raised from 15s to 60s (Attio writes can be slow under
  load).
- Attio adapter now stamps `grizz_last_sync` on every write. It was dropped in the
  headless→adapter conversion, so freshness/coverage counts never moved even though
  the data synced.
- Attio matching is now driven by `gid_company` (the canonical id, api.md §10),
  with a native `domains` fallback for records that don't carry a gid yet — fixing
  both the wrong driving key and the duplicate-creation risk for pre-gid records.
- A 429 `Retry-After` returned as an HTTP-date (Attio's form) no longer crashes
  with a `ValueError` that failed the whole batch — it's parsed as integer-seconds
  OR HTTP-date, and capped.
- A phone value Attio rejects (invalid for its phone-number type) no longer fails
  the whole record — the phone is dropped and the write retried so every other
  field still lands.

### Performance
- Attio writes run concurrently (Attio has no batch-write API), bounded to stay
  under Attio's 25 writes/sec limit, with 429 backoff. Single records are still
  seconds each server-side, so large tier syncs are best run as a background job.

### Changed
- `audience` runs non-interactively when `--yes/-y` is passed or stdout/stdin is
  not a TTY (e.g. an agent/MCP hand-off or CI): unmatched companies are created
  and failed batches retried once without prompting, so a run never blocks waiting
  on a terminal that isn't there.

### Added
- `audience --gids <file>` — push an explicit list of `gid_company` values (one
  per line, or a CSV with a `gid_company` column) instead of a whole audience.
  Resolves the gids to full company data via the read-only, no-credit
  lookup-batch endpoint, then runs the same match → update → create flow. Works
  for every CRM. Intended for handing a filtered selection off from the Grizz MCP
  (discover/refine in the MCP, load the bulk write here, out of model context).
- **Attio support** as a first-class CRM adapter — it works the same way as the
  Salesforce/HubSpot adapters: same `setup` / `enrich` / `audience` commands, the
  same client-side `config.yaml` mapping, the same find-then-update/create flow,
  and it appears in the interactive menu. New `AttioAdapter` in
  `grizz_enrichment/adapters/`, registered in `ADAPTERS`.
- `attio:` section added to `config.example.yaml`, pre-filled with the canonical
  `grizz_*` attribute slugs.
- `ATTIO_API_KEY` added to `.env.example`.

### Notes — Attio's two design-forced differences (handled by the adapter)
- `setup --crm attio` also needs `GRIZZ_API_KEY`: the attribute catalog is fetched
  from Grizz at runtime (not hardcoded) so it stays in lock-step with the Grizz
  schema, and is created over the Attio REST API (its MCP cannot create attributes).
- Dedup/match uses the native `domains` attribute (Attio has no Grizz domain field);
  domain is written to native `domains` on create only. Matching is by
  `grizz_company_id` then `domains`. Phone values are normalized to E.164 (Attio
  rejects records with malformed phone numbers).

---

## [0.5.0] — 2026-05-28

### Added
- `grizz_client.tech_gap(api_key, crm, credentials, ...)` — wraps
  `POST /api/v1/companies/tech-gap/`.  Server-side orchestration returns
  the combined tech-gap list (in_crm + not_in_crm) with per-signal status.
- `grizz_client.create_in_crm(api_key, crm, credentials, records, ...)` —
  wraps `POST /api/v1/companies/create-crm/`.  Universal Step 4 of the
  company-resolution flow: creates new CRM accounts from Grizz data.
- `grizz_client.get_crm(api_key, crm, credentials, filter, ...)` — wraps
  `POST /api/v1/companies/get-crm/`.  Paginated CRM detail rows with
  per-account Grizz status (filter: awaiting / stale / all).
- `grizz_client.update_crm(api_key, crm, credentials, records, ...)` —
  wraps `POST /api/v1/companies/update-crm/`.  Rewrites Grizz_* fields
  on existing CRM accounts.

All four are paired 1:1 with their MCP tools (`get_tech_gap_companies`,
`create_crm_companies`, `get_crm_companies`, `update_crm_companies`) for
parallel naming end-to-end.  The whole company surface is now
server-side orchestrated — the MCP holds no CRM read/write logic.

---

## [0.4.0] — 2026-05-27

### Added
- `grizz_client.lookup_batch(api_key, lookups)` — read-only batch
  cascade lookup against `POST /api/v1/companies/lookup-batch/`.  Up to
  5000 inputs per call, no credits charged, returns Grizz's structured
  view per record.  Used by upcoming MCP `get_crm_companies` and
  `update_crm_companies` tools to compose per-account status without
  burning credits.

### Changed
- **BREAKING — endpoint renames (no redirects).**  All `/api/v1/*` paths
  brought into line with the canonical noun convention in the Grizz API docs §2.
  Customers calling the API directly must update their URLs:

      /api/v1/enrichment/             → /api/v1/companies/enrich/
      /api/v1/enrichment/bulk/        → /api/v1/companies/enrich-bulk/
      /api/v1/enrichment/budget/      → /api/v1/companies/enrich/budget/
      /api/v1/enrichment/<id>/        → /api/v1/companies/enrich/<id>/
      /api/v1/enrichment/<id>/results/ → /api/v1/companies/enrich/<id>/results/
      /api/v1/admin/crm-coverage-reports/ → /api/v1/admin/crm-coverage/
      /api/v1/admin/crm-mappings/     → /api/v1/admin/crm-field-mappings/

  Old paths return 404.  See the Grizz API docs §11 (Tool ↔ Endpoint Mapping)
  for the full table.

---

## [0.3.0] — 2026-05-27

### Added
- `grizz_client.enrich()` accepts the full canonical cascade input
  (`gid_company`, `grizz_id`, `domain`, `company_name`, `hq_city`,
  `hq_state`, `hq_country`, `hq_phone`).  `domain` may still be passed
  positionally.
- `grizz_client.submit_bulk()` and `grizz_client.enrich_bulk()` —
  submit up to 200 companies in one round-trip via
  `POST /api/v1/enrichment/bulk/`.  Soft-caps to org monthly budget.
- `grizz_client.get_budget()` — wraps `GET /api/v1/enrichment/budget/`.
- Each entry in `enrich_bulk()`'s `matched` / `no_match` / `low_conf` /
  `failed` lists now carries the original `input` dict so callers can
  correlate results to inputs.

### Changed
- **BREAKING — MCP `field_mapping` canonical key rename**: the legacy
  key `grizz_id` (mapping to the `Grizz_Company_ID__c` /
  `grizz_company_id` CRM column) has been hard-cut renamed to
  `gid_company`.  Customers using the
  `x-salesforce-field-mapping` / `x-hubspot-field-mapping` headers to
  override field mappings must update their override JSON to use
  `gid_company`.  The CRM column itself is unchanged.

---

## [0.2.0] — 2026-04-16

### Added
- HubSpot adapter — full support for enriching Company records via the HubSpot API
- `setup --crm hubspot` command to create all Grizz custom properties on HubSpot Company records
- `audience` and `enrich` commands now support `--crm hubspot`
- `revenue_range` and `grizz_activity` fields added to Salesforce setup and config
- Interactive menu now asks for CRM selection on all actions (including setup)
- `HUBSPOT_API_KEY` added to `.env.example`
- `hubspot.config.yml` added to `.gitignore`

### Changed
- Interactive menu renamed "Set up Salesforce fields" → "Set up CRM fields"
- `config.example.yaml` HubSpot section uncommented and completed with all property names
- README updated with step-by-step credential instructions for both Salesforce and HubSpot

---

## [0.1.0] — 2026-03-24

### Added
- Initial release
- `enrich` command: enrich Salesforce Account records from a CSV of Account IDs
- Interactive menu via `python run.py` (no arguments)
- Dry-run mode (`--dry-run`) to preview changes before writing to CRM
- YAML-based field mapping config (`config.yaml`)
- Salesforce adapter using `simple-salesforce`
- Grizz Enrichment API client with automatic polling
- HubSpot adapter stub (coming soon)
