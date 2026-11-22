from .salesforce import SalesforceAdapter
from .hubspot import HubSpotAdapter
from .attio import AttioAdapter, AttioContactAdapter

ADAPTERS = {
    "salesforce": SalesforceAdapter,
    "hubspot": HubSpotAdapter,
    "attio": AttioAdapter,
}

# People-object adapters keyed by CRM (the company ADAPTERS above are the
# Company-object flow).  Attio's contact adapter is config-driven; HubSpot and
# Salesforce contacts use native_contacts.NATIVE_CONTACT_ADAPTERS.  Every CRM
# syncs client-side with the operator's own credentials.
CONTACT_ADAPTERS = {
    "attio": AttioContactAdapter,
}
