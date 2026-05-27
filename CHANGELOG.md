# Changelog

All notable changes to this project will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/).

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
