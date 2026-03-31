"""Apply Grizz field mappings to produce CRM update payloads."""


def apply_mapping(grizz_data: dict, field_mapping: dict) -> dict:
    """Map Grizz result fields to CRM field names.

    Args:
        grizz_data:    The results dict returned by the Grizz API.
        field_mapping: Dict of {grizz_field: crm_field} from config.yaml.

    Returns:
        Dict of {crm_field: value} containing only fields that are present
        and non-None in grizz_data.
    """
    updates = {}
    for grizz_field, crm_field in field_mapping.items():
        value = grizz_data.get(grizz_field)
        if value is not None and value != "":
            updates[crm_field] = value
    return updates
