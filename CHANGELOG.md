# Changelog

All notable changes to this project will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/).

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
