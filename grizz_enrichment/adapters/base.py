"""Abstract base class for CRM adapters."""

from abc import ABC, abstractmethod


class CRMAdapter(ABC):

    @abstractmethod
    def connect(self) -> None:
        """Establish a connection to the CRM using credentials from the environment."""

    @abstractmethod
    def get_domain(self, record_id: str, domain_field: str) -> str | None:
        """Return the domain value for a given record ID, or None if not found."""

    @abstractmethod
    def update_record(self, record_id: str, fields: dict) -> None:
        """Update a CRM record with the provided field values."""

    @abstractmethod
    def find_account(self, domain: str | None, grizz_id: str | None) -> str | None:
        """Return the CRM account ID matching domain or grizz_id, or None."""

    @abstractmethod
    def find_accounts_bulk(self, companies: list[dict]) -> dict[str, str]:
        """Match a list of company dicts to existing CRM accounts in bulk.

        Each dict should have 'grizz_id' and/or 'domain' keys.
        Returns a dict mapping grizz_id (or domain as fallback) → CRM account ID
        for every company that was matched.
        """

    @abstractmethod
    def update_accounts(self, records: list[dict]) -> list[dict]:
        """Update multiple CRM accounts in one batch call.

        Each record must include an 'Id' key alongside the fields to update.
        Returns a list of result dicts, each with at least:
          {"id": str, "success": bool, "errors": list}
        """

    @abstractmethod
    def create_accounts(self, records: list[dict]) -> list[dict]:
        """Create multiple CRM accounts in one batch call.

        Returns a list of result dicts, each with at least:
          {"id": str, "success": bool, "errors": list}
        """
