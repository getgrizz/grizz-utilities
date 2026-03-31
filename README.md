# Grizz CRM Enrichment Tool

Enrich your CRM account records with Grizz construction company data — firmographics, NAICS codes, addresses, and more.

**Supported CRMs:** Salesforce
**Coming soon:** HubSpot, Attio, Pipedrive, Zoho

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
- Salesforce credentials (username, password, security token)

---

## Setup

**1. Clone the repo**

```bash
git clone https://github.com/getgrizz/grizz-crm-enrichment.git
cd grizz-crm-enrichment
```

**2. Create and activate a virtual environment**

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

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Configure credentials**

```bash
cp .env.example .env
```

Open `.env` and fill in your Grizz API key and Salesforce credentials.

**5. Configure field mappings**

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and map Grizz fields to your Salesforce field API names. See [Field Reference](#field-reference) below.

---

## Usage

### Interactive (recommended for first-time use)

```bash
python run.py
```

You'll be guided through CRM selection, file input, and options.

### Headless (for scripting and automation)

**Create Grizz custom fields in Salesforce:**

```bash
python run.py setup
```

This creates all recommended Grizz custom fields on the Account object and configures field-level security. Safe to run multiple times — existing fields are skipped.

**Enrich accounts from a CSV:**

```bash
python run.py enrich --crm salesforce --input accounts.csv
```

| Flag | Description |
|---|---|
| `--crm` | CRM to use (`salesforce`) |
| `--input` | Path to CSV file with an `Id` or `Account ID` column |
| `--config` | Path to config file (default: `config.yaml`) |
| `--dry-run` | Preview changes without writing to the CRM |

**Fetch a Grizz audience and push to CRM:**

```bash
# Use an existing audience
python run.py audience --audience-id <uuid>

# Or create a new audience from a prompt
python run.py audience --prompt "Mid-market US roofing contractors"
```

| Flag | Description |
|---|---|
| `--audience-id` | ID of an existing Grizz audience |
| `--prompt` | Natural language prompt to create a new audience |
| `--crm` | CRM to use (`salesforce`) |
| `--config` | Path to config file (default: `config.yaml`) |
| `--batch-size` | Records per API call when creating accounts (default: `200`, max: `200`) |
| `--dry-run` | Preview changes without writing to the CRM |

The audience command always saves a full copy of the audience to `csv_out/Audience <id>.csv` before touching your CRM. If any companies cannot be matched to an existing account by Grizz company ID or domain, you will be prompted before any new records are created.

### CSV format

The `enrich` command requires a CSV with one column named `Id` or `Account ID` containing Salesforce Account IDs:

```
Id
0011a00000XyzAbcAAE
0011a00000XyzDefAAE
0011a00000XyzGhiAAE
```

Additional columns are ignored.

Audience results are saved automatically to `csv_out/Audience <id>.csv` with company name, domain, location, firmographics, and tech stack columns.

---

## Field Reference

These Grizz fields are available for mapping in `config.yaml`:

| Grizz Field | Description |
|---|---|
| `company_name` | Official company name |
| `naics_code` | NAICS industry code (e.g. `236115`) |
| `naics_description` | NAICS industry label (e.g. `New Single-Family Housing Construction`) |
| `company_description` | AI-generated company description (≤1000 characters) |
| `rationale` | Short AI explanation of the NAICS classification (≤200 characters) |
| `valid_domain` | `true` if the domain is a real operating business |
| `street_address` | Street address |
| `city` | City |
| `state_province_region` | State or province |
| `country` | Country |
| `zip_code` | ZIP / postal code |
| `phone` | Primary phone number |
| `email` | Primary email address |
| `linkedin_url` | LinkedIn company page URL (enrichment source only) |
| `erp_tech_stack` | ERP software detected in use (e.g. `"Viewpoint Vista"`). `null` for enrichment-source results. |
| `erp_job_title` | Job title associated with the ERP signal |
| `ats_tech_stack` | ATS/HR software detected in use. `null` for enrichment-source results. |
| `ats_job_title` | Job title associated with the ATS signal |
| `other_tech_signals` | Comma-separated list of other detected software |
| `grizz_id` | Grizz internal company ID (database source only) |
| `grizz_url` | Link to the company's profile on getgrizz.com (database source only) |
| `employee_range` | Employee count range, e.g. `"50-200"` (database source only) |
| `grizz_construction` | `true` if Grizz has confirmed this is a construction company (database source only) |

---

## Salesforce setup notes

- The integration connects using **username + password + security token**. Reset your security token at: *Setup → My Personal Information → Reset My Security Token*
- SOAP API login must be enabled: *Setup → User Interface → Enable SOAP API login()*. This is off by default in Developer Edition orgs created after Spring '23.
- For sandbox orgs, set `SALESFORCE_DOMAIN=test` in your `.env`
- Custom fields (e.g. `Grizz_NAICS_Code__c`) must be created in Salesforce before running: *Setup → Object Manager → Account → Fields & Relationships → New*

**Adding Grizz fields to your Lightning record page:**

`python run.py setup` creates all Grizz fields and adds them to the classic Account page layout. If your org uses Lightning record pages (most do), you'll need to add the fields to your Lightning page once manually:

1. Go to *Setup → Object Manager → Account → Lightning Record Pages*
2. Click **Edit** on your Account record page
3. Add a new section and drag the `Grizz_*` fields into it
4. Save and activate

---

## Versioning

This project follows [Semantic Versioning](https://semver.org/). See [CHANGELOG.md](CHANGELOG.md) for release history.

---

## Support

For issues with this tool, open a GitHub issue.
For questions about Grizz data or your API key, contact [support@getgrizz.com](mailto:support@getgrizz.com).
