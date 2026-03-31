"""HubSpot CRM adapter — coming soon."""

from .base import CRMAdapter


class HubSpotAdapter(CRMAdapter):

    def connect(self) -> None:
        raise NotImplementedError("HubSpot adapter is coming soon.")

    def get_domain(self, record_id: str, domain_field: str) -> str | None:
        raise NotImplementedError("HubSpot adapter is coming soon.")

    def update_record(self, record_id: str, fields: dict) -> None:
        raise NotImplementedError("HubSpot adapter is coming soon.")
