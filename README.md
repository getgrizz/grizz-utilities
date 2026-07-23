# Grizz CRM Enrichment Tool

Enrich your CRM account records with Grizz construction company data — firmographics, NAICS codes, addresses, and more.

**Supported CRMs:** Salesforce, HubSpot, Attio
**Coming soon:** Pipedrive, Zoho

All three CRMs work the same way — same `setup`, `enrich`, and `audience`
commands, same `config.yaml` mapping. Attio has only a couple of small,
design-forced differences (its `setup` also needs your Grizz key, and it
deduplicates on the native `domains` attribute); see [Attio](#attio) for the
specifics.

---

## How it works

**Enrich existing accounts**

1. You provide a CSV of CRM account IDs
2. The tool fetches each account's domain from your CRM
3. The domain is submitted to the [Grizz Enrichment API](https://getgrizz.com/docs/enrichment-api/)
4. Results are mapped to your CRM fields and written back automatically

**Audience builder → CRM**

1. You provide a Grizz audience ID (or a prompt to create one)
2. The tool fetches all companies in the audience and saves them to `csv_out/Audience <id>.csv`
3. Each company is matched to an existing CRM account by Grizz company ID or domain
4. Unmatched companies can be created as new Account records in bulk

---

## Prerequisites

- Python 3.9 or higher
- A [Grizz API key](https://getgrizz.com/dashboard)
- Credentials for your CRM (see [Salesforce](#salesforce-api-credentials) or [HubSpot](#hubspot-api-credentials) below)

---

## Setup

**1. Install Python**

If you're not sure whether Python is installed, open a terminal and run:

```bash
python3 --version
```

If you see a version number (e.g. `Python 3.11.4`), you're good — skip to step 2.

If not:

- **Mac:** Download and run the installer from [python.org/downloads](https://www.python.org/downloads/). After installing, close and reopen your terminal.
- **Windows:** Download and run the installer from [python.org/downloads](https://www.python.org/downloads/). On the first screen of the installer, **check the box that says "Add Python to PATH"** before clicking Install.

Any version 3.9 or higher will work.

**2. Clone the repo**

```bash
git clone https://github.com/getgrizz/grizz-utilities.git
cd grizz-utilities
```

**3. Create and activate a virtual environment**

A virtual environment keeps this tool's dependencies isolated from the rest of your system. You only need to do this once.

On Mac or Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows:
```bash
python -m venv .venv
.venv\Scripts\activate
```

You'll see `(.venv)` appear at the start of your terminal prompt when it's active. You'll need to run the activate command each time you open a new terminal window before using the tool.

**4. Install dependencies**

```bash
pip install -r requirements.txt
```

**5. Configure credentials**

```bash
cp .env.example .env
```

Open `.env` and fill in your Grizz API key and the credentials for your CRM. See [Salesforce API credentials](#salesforce-api-credentials) or [HubSpot API credentials](#hubspot-api-credentials) below for step-by-step instructions.

**6. Configure field mappings**

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and map Grizz fields to your CRM's field names. The file contains sections for each supported CRM — fill in only the section for your CRM. See [Field Reference](#field-reference) below.

---

## Usage

### Interactive (recommended for first-time use)

```bash
python run.py
```

You'll be guided through CRM selection, file input, and options.

### Headless (for scripting and automation)

**Create Grizz custom fields in your CRM:**

```bash
# Salesforce
python run.py setup --crm salesforce

# HubSpot
python run.py setup --crm hubspot
```

This creates all recommended Grizz custom fields/properties on your Account (Salesforce) or Company (HubSpot) object. Safe to run multiple times — existing fields are skipped.

**Enrich accounts from a CSV:**

```bash
python run.py enrich --crm salesforce --input accounts.csv
python run.py enrich --crm hubspot --input companies.csv
```

| Flag | Description |
|---|---|
| `--crm` | CRM to use: `salesforce`, `hubspot`, or `attio` |
| `--input` | Path to CSV file with an `Id` or `Account ID` column |
| `--config` | Path to config file (default: `config.yaml`) |
| `--dry-run` | Preview changes without writing to the CRM |

**Push a Grizz company list to CRM:**

```bash
# Use an existing audience
python run.py audience --crm salesforce --audience-id <uuid>

# Or create a new audience from a prompt
python run.py audience --crm hubspot --prompt "Mid-market US roofing contractors"

# Or load an explicit list of companies (e.g. a filtered selection from the Grizz MCP)
python run.py audience --crm attio --gids selection.csv
```

Provide exactly one source: `--audience-id`, `--prompt`, or `--gids`.

| Flag | Description |
|---|---|
| `--audience-id` | ID of an existing Grizz audience (loads the whole audience) |
| `--prompt` | Natural language prompt to create a new audience |
| `--gids` | File of `gid_company` values — one per line, or a CSV with a `gid_company` column — to load exactly that set |
| `--crm` | CRM to use: `salesforce`, `hubspot`, or `attio` |
| `--config` | Path to config file (default: `config.yaml`) |
| `--batch-size` | Records per API call when creating accounts (default: `200`, max: `200`) |
| `--dry-run` | Preview changes without writing to the CRM |

The command saves a full copy of the resolved companies to `csv_out/` before touching your CRM. If any companies cannot be matched to an existing account by Grizz company ID or domain, you will be prompted before any new records are created.

**Filtered hand-off from the Grizz MCP.** Build and refine a company list in the
Grizz MCP (e.g. "biggest 50 in New Jersey"), export the selected `gid_company`
values to a CSV, and load exactly that set with `--gids`. Grizz resolves the gids
to full company data (free, no credits), then runs the normal match → update →
create flow — so discovery/refinement stays in the MCP and the bulk write runs
here, out of any model context.

### CSV format

The `enrich` command requires a CSV with one column named `Id` or `Account ID` containing CRM record IDs:

```
Id
0011a00000XyzAbcAAE
0011a00000XyzDefAAE
```

Additional columns are ignored.

Audience results are saved automatically to `csv_out/Audience <id>.csv` with company name, domain, location, firmographics, and tech stack columns.

---

## Field Reference

These Grizz fields are available for mapping in `config.yaml`:

| Grizz Field | Description |
|---|---|
| `grizz_id` | Grizz internal company ID |
| `gid_company` | Grizz canonical company ID (the stable driving key) |
| `grizz_url` | Link to the company's profile on getgrizz.com |
| `company_name` | Official company name |
| `domain` | Company domain |
| `linkedin_url` | LinkedIn company page URL |
| `company_description` | AI-generated company description (≤1000 characters) |
| `phone` | Primary phone number |
| `email` | Primary email address |
| `city` | City |
| `state_province_region` | State or province |
| `country` | Country |
| `employee_range` | Employee count range, e.g. `"50-200"` |
| `revenue_range` | Revenue range, e.g. `"$1M-$10M"` |
| `naics_code` | NAICS industry code (e.g. `236115`) |
| `grizz_activity` | Grizz construction activity classification |
| `grizz_construction` | `true` if Grizz has confirmed this is a construction company |
| `erp_tech_stack` | ERP software detected in use (e.g. `"Viewpoint Vista"`); `null` if none detected |
| `erp_match_type` | How the ERP signal was identified |
| `erp_keyword_usage` | Keyword evidence for the ERP signal |
| `ats_tech_stack` | ATS/HR software detected in use; `null` if none detected |
| `ats_match_type` | How the ATS signal was identified |
| `ats_keyword_usage` | Keyword evidence for the ATS signal |
| `other_tech_signals` | Comma-separated list of other detected software |

---

## Salesforce API credentials

The integration connects with your **username, password, and security token**. Here's how to get each:

**Security token**

1. Log into Salesforce
2. Click your profile picture (top right) → *Settings*
3. In the left sidebar go to *My Personal Information → Reset My Security Token*
4. Click **Reset Security Token** — Salesforce will email it to you
5. Add it to your `.env` as `SALESFORCE_SECURITY_TOKEN`

**Other required values**

| `.env` key | Where to find it |
|---|---|
| `SALESFORCE_USERNAME` | Your Salesforce login email |
| `SALESFORCE_PASSWORD` | Your Salesforce password |
| `SALESFORCE_DOMAIN` | Use `login` for production, `test` for sandbox |

**Other notes**

- SOAP API login must be enabled in your org: *Setup → User Interface → Enable SOAP API login*. This is off by default in Developer Edition orgs created after Spring '23.
- Run `python run.py setup --crm salesforce` once to create all Grizz custom fields on the Account object.

**Adding Grizz fields to your Lightning record page:**

The setup command adds fields to the classic page layout. If your org uses Lightning record pages (most do), add the fields manually once:

1. Go to *Setup → Object Manager → Account → Lightning Record Pages*
2. Click **Edit** on your Account record page
3. Add a new section and drag the `Grizz_*` fields into it
4. Save and activate

---

## HubSpot API credentials

The integration uses a **Legacy App access token**. Here's how to create one:

1. Log into your HubSpot developer account at [developers.hubspot.com](https://developers.hubspot.com)
2. Go to *Developer Settings → Legacy Apps*
3. Click **Create a new app**
4. Give it a name (e.g. "Grizz Enrichment")
5. Assign the following scopes:
   - `crm.objects.companies.read`
   - `crm.objects.companies.write`
   - `crm.schemas.companies.read`
   - `crm.schemas.companies.write`
6. Save the app
7. Copy the **Access token** from the app detail page
8. Add it to your `.env` as `HUBSPOT_API_KEY`

**Other notes**

- Run `python run.py setup --crm hubspot` once to create the Grizz property group and all custom properties on Company records.
- Properties are created in a **Grizz** group but won't appear on Company records automatically. To add them to the record view:
  1. Open any Company record in HubSpot
  2. Scroll to the bottom of the properties panel and click *Manage properties*
  3. Search for "Grizz" and add the fields you want
  4. On Enterprise, set a default layout for all users at: *Settings → Data Management → Record Customization*

---

## Attio

Attio uses the **same `setup` / `enrich` / `audience` flow** as Salesforce and
HubSpot — same `config.yaml` mapping, same matching (by Grizz company ID, then
domain), same create/update behavior. There are only two design-forced
differences, and the tool handles both for you:

- **`setup --crm attio` is config-driven.** It reads the slugs you map in
  `config.yaml` and creates only those, so it never duplicates or clobbers
  attributes you already built or renamed — and `--object company` / `--object
  contacts` each provision just that one object. (No `GRIZZ_API_KEY` needed for
  setup.)
- **Dedup is on the native `domains` attribute.** Attio has no Grizz domain field;
  the company's domain is written to native `domains` only when creating a record.
- **Location is one object-typed attribute.** Attio holds city + region + country
  in a single location attribute, so `config.yaml` maps them as dotted sub-fields
  (`grizz_location.locality` / `.region` / `.country_code`) that the tool
  collapses into one value on write (country normalized to ISO alpha-2).

**Credentials** — add both to your `.env`:

| `.env` key | Where to find it |
|---|---|
| `GRIZZ_API_KEY` | [getgrizz.com/dashboard](https://getgrizz.com/dashboard) |
| `ATTIO_API_KEY` | Attio → *Workspace settings → Developers → API keys → Create*. Grant read/write on **Objects → Companies** (records + attributes). |

**1. Create the Grizz attributes** (once per workspace — idempotent, skips any that
already exist):

```bash
python run.py setup --crm attio
python run.py setup --crm attio --dry-run   # preview what would be created
```

The Attio API (not its MCP) is the only way to create custom attributes, so this
step runs over REST.

**2. Enrich existing Attio companies from a CSV**, or **push a Grizz audience** —
exactly like the other CRMs:

```bash
python run.py enrich   --crm attio --input company_record_ids.csv
python run.py audience --crm attio --audience-id <uuid>
python run.py audience --crm attio --prompt "Mid-market US roofing contractors"
```

The `audience` flow matches each company to an existing Attio record (by
`grizz_company_id`, then `domains`), updates the matches, and offers to create the
rest as new companies. The `attio:` section of `config.yaml` is pre-filled with the
canonical `grizz_*` slugs — **edit it to match whatever you named your attributes**
(setup and sync both read these slugs, so if you built `grizz_hq_phone` rather than
`grizz_phone`, point the mapping there and setup will see it already exists).

**3. Sync contacts (People)** — Attio is headless, so contacts sync **client-side**,
the same model as companies (there is no server-side Attio contact endpoint):

```bash
python run.py setup   --crm attio --object contacts     # create the People grizz_contact_* attributes
python run.py people discover --input companies.csv --out-dir people_out
python run.py people sync --crm attio --contacts people_out/contacts.json --enrich-email
```

The `attio_contacts:` section of `config.yaml` maps person fields to your Attio
People attributes. Deduplication reuses the **same cascade as HubSpot/Salesforce**:
a global strong-key match (grizz_person_id, email) plus an **account-scoped**
`grizz_person_id → email → linkedin → fuzzy-name` match run server-side (the client
reads each parent company's existing people and passes them to Grizz's
`prepare-writes` endpoint, so the matching logic lives in one place). Matching is
scoped to the parent company, so two people with the same name at different
companies never collide; fuzzy name matches below `--fuzzy-threshold` (default 0.9)
are surfaced for review rather than merged. Following the Grizz write principle, it
writes **only the
`grizz_contact_*` attributes** on an existing contact (including
`grizz_contact_email`/`grizz_contact_phone`); the native name/email/phone and the
parent-company link are seeded **only when creating a new person**, so Grizz never
overwrites a native field on a record it didn't create. City/state/country are
collapsed into one location attribute, just like companies.

> **Attio People scope.** For contacts, grant the `ATTIO_API_KEY` read/write on
> **Objects → People** (records + attributes) in addition to Companies.

> **Bulk loads are for domain admins.** A full-tier sync reads and writes many CRM
> records; treat this script as an operator/admin tool, not something every AE or
> BDR runs against the workspace.

---

## Versioning

This project follows [Semantic Versioning](https://semver.org/). See [CHANGELOG.md](CHANGELOG.md) for release history.

---

## Support

For issues with this tool, open a GitHub issue.
For questions about Grizz data or your API key, contact [support@getgrizz.com](mailto:support@getgrizz.com).
