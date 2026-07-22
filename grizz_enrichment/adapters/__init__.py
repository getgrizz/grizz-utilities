from .salesforce import SalesforceAdapter
from .hubspot import HubSpotAdapter
from .attio import AttioAdapter, AttioContactAdapter

ADAPTERS = {
    "salesforce": SalesforceAdapter,
    "hubspot": HubSpotAdapter,
    "attio": AttioAdapter,
}

# People-object adapters keyed by CRM (the company ADAPTERS above are the
# Company-object flow).  Only Attio's contact sync runs client-side today —
# HubSpot/Salesforce contacts go through the server /people/create-crm/ endpoint.
CONTACT_ADAPTERS = {
    "attio": AttioContactAdapter,
}
