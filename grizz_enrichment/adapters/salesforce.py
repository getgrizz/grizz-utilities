"""Salesforce CRM adapter."""

import os

from simple_salesforce import Salesforce

from .base import CRMAdapter

# URL prefixes to strip when extracting a clean domain from a Website field
from ..domain_utils import clean_domain as _clean_domain


class SalesforceAdapter(CRMAdapter):

    def __init__(self):
        self.sf = None

    def connect(self) -> None:
        """Connect using SALESFORCE_* environment variables.

        Supports two auth methods:
          1. Session ID (SALESFORCE_SESSION_ID + SALESFORCE_INSTANCE_URL)
          2. Username / password / security token (SALESFORCE_USERNAME etc.)
        """
        session_id = os.environ.get("SALESFORCE_SESSION_ID")
        instance_url = os.environ.get("SALESFORCE_INSTANCE_URL")

        if session_id and instance_url:
            self.sf = Salesforce(session_id=session_id, instance_url=instance_url)
        else:
            self.sf = Salesforce(
                username=os.environ["SALESFORCE_USERNAME"],
                password=os.environ["SALESFORCE_PASSWORD"],
                security_token=os.environ["SALESFORCE_SECURITY_TOKEN"],
                domain=os.environ.get("SALESFORCE_DOMAIN", "login"),
            )

    def get_domain(self, account_id: str, domain_field: str) -> str | None:
        """Query the Account's domain field and return a clean domain string."""
        result = self.sf.query(
            f"SELECT {domain_field} FROM Account WHERE Id = '{account_id}'"
        )
        records = result.get("records", [])
        if not records:
            return None
        raw = records[0].get(domain_field) or ""
        return _clean_domain(raw) if raw else None

    def update_record(self, account_id: str, fields: dict) -> None:
        """Patch the Salesforce Account with the provided fields."""
        self.sf.Account.update(account_id, fields)

    def find_accounts_bulk(self, companies: list[dict], grizz_id_field: str) -> dict[str, str]:
        """Match companies to Salesforce Accounts in bulk using IN queries.

        Queries in batches of 200 to stay within SOQL limits.
        Returns {grizz_id: sf_account_id} for grizz_id matches, and
        {domain: sf_account_id} for domain-only matches.
        """
        BATCH = 200
        matched: dict[str, str] = {}

        grizz_ids = [str(c["grizz_id"]) for c in companies if c.get("grizz_id")]
        domains   = [c["domain"] for c in companies if c.get("domain")]

        # Match by grizz_id_field from config
        for i in range(0, len(grizz_ids), BATCH):
            batch = grizz_ids[i:i + BATCH]
            values = ", ".join(f"'{v}'" for v in batch)
            soql = f"SELECT Id, {grizz_id_field} FROM Account WHERE {grizz_id_field} IN ({values})"
            for record in self.sf.query_all(soql).get("records", []):
                matched[record[grizz_id_field]] = record["Id"]

        # Match remaining unmatched companies by domain
        matched_grizz_ids = set(matched.keys())
        unmatched_domains = [
            c["domain"] for c in companies
            if c.get("domain") and str(c.get("grizz_id", "")) not in matched_grizz_ids
        ]
        for i in range(0, len(unmatched_domains), BATCH):
            batch = unmatched_domains[i:i + BATCH]
            conditions = " OR ".join(f"Website LIKE '%{d}%'" for d in batch)
            soql = f"SELECT Id, Website FROM Account WHERE {conditions}"
            for record in self.sf.query_all(soql).get("records", []):
                website = record.get("Website") or ""
                for domain in batch:
                    if domain in website:
                        matched[domain] = record["Id"]
                        break

        return matched

    def find_account(self, domain: str | None, grizz_id: str | None) -> str | None:
        """Look up an Account by Grizz_Company_ID__c or Website domain.

        Tries grizz_id first (exact), then domain (substring match on Website).
        Returns the Salesforce Account Id, or None if not found.
        """
        if grizz_id:
            soql = (
                f"SELECT Id FROM Account "
                f"WHERE Grizz_Company_ID__c = '{grizz_id}' LIMIT 1"
            )
            records = self.sf.query(soql).get("records", [])
            if records:
                return records[0]["Id"]

        if domain:
            # Match on bare domain — Website may include protocol/www/path
            soql = (
                f"SELECT Id FROM Account "
                f"WHERE Website LIKE '%{domain}%' LIMIT 1"
            )
            records = self.sf.query(soql).get("records", [])
            if records:
                return records[0]["Id"]

        return None

    def update_accounts(self, records: list[dict]) -> list[dict]:
        """Update up to 200 Accounts in one Collections API call.

        Each record must include an 'Id' key.
        Returns a list of result dicts with keys: id, success, errors.
        """
        tagged = [{"attributes": {"type": "Account"}, **r} for r in records]
        response = self.sf.restful(
            "composite/sobjects",
            method="PATCH",
            json={"allOrNone": False, "records": tagged},
        )
        return response

    def create_accounts(self, records: list[dict]) -> list[dict]:
        """Create up to 200 Accounts in one Collections API call.

        Each record should be a plain field dict (no 'attributes' key needed —
        this method adds the sobject type tag automatically).

        Returns a list of result dicts with keys: id, success, errors.
        """
        tagged = [{"attributes": {"type": "Account"}, **r} for r in records]
        response = self.sf.restful(
            "composite/sobjects",
            method="POST",
            json={"allOrNone": False, "records": tagged},
        )
        return response  # list of {id, success, errors, ...}
