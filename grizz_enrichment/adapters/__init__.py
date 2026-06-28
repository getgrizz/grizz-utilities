from .salesforce import SalesforceAdapter
from .hubspot import HubSpotAdapter
from .attio import AttioAdapter

ADAPTERS = {
    "salesforce": SalesforceAdapter,
    "hubspot": HubSpotAdapter,
    "attio": AttioAdapter,
}
