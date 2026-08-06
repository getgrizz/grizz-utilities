"""Canonical domain normalization.

ONE implementation, used by every path that sends a domain to Grizz or
searches a CRM by domain.  It previously existed as a private `_clean_domain`
in each of the three adapters, and any caller that bypassed an adapter had no
normalization at all.  The copies drifted in reach, not in logic: the adapters
applied it, `grizz_client`'s cascade path did not.

Why it matters: `POST /api/v1/companies/lookup-batch/` does NOT normalize its
input.  A stored CRM domain of `https://www.example.com/` comes back
`matched:false` for a company Grizz knows perfectly well.  A small but real
share of `domain` values in a typical CRM export are dirty this way; cleaning
them recovers in-ICP matches that were previously invisible.
"""

_URL_PREFIXES = ("https://", "http://", "www.")


def clean_domain(raw: str | None) -> str | None:
    """Strip protocol, www, and any path to a bare lowercase domain.

    Returns None for empty/None input so callers can filter in one step.
    """
    if not raw:
        return None
    domain = raw.strip().lower()
    for prefix in _URL_PREFIXES:
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.rstrip("/").split("/")[0]
    return domain or None
