from .salesforce import SalesforceAdapter
from .hubspot import HubSpotAdapter

ADAPTERS = {
    "salesforce": SalesforceAdapter,
    "hubspot": HubSpotAdapter,
}
