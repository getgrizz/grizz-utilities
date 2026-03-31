"""Grizz Audience API client.

Handles submit (optional) → poll → fetch for audience results.
"""

import time

import requests

BASE_URL = "https://getgrizz.com"
POLL_INTERVAL = 10   # seconds between status checks
MAX_POLLS = 60       # give up after 10 minutes


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def submit(api_key: str, prompt: str) -> str:
    """Submit a new audience request. Returns the audience ID."""
    url = f"{BASE_URL}/api/v1/audiences/"
    resp = requests.post(url, json={"prompt": prompt}, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def poll_status(api_key: str, audience_id: str) -> str:
    """Check audience status. Returns the status string."""
    url = f"{BASE_URL}/api/v1/audiences/{audience_id}/"
    resp = requests.get(url, headers=_headers(api_key), timeout=30)
    resp.raise_for_status()
    return resp.json()["status"]


def fetch_results(api_key: str, audience_id: str) -> list[dict]:
    """Fetch the list of company dicts for a completed audience."""
    url = f"{BASE_URL}/api/v1/audiences/{audience_id}/results/"
    resp = requests.get(url, headers=_headers(api_key), timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_audience(api_key: str, audience_id: str, on_status=None) -> list[dict]:
    """Poll until complete and return list of company dicts.

    Args:
        api_key:     Grizz API key.
        audience_id: ID of an existing audience.
        on_status:   Optional callable(status: str) invoked on each poll tick.

    Returns:
        List of company dicts.

    Raises:
        RuntimeError:  If the audience fails.
        TimeoutError:  If polling exceeds MAX_POLLS.
    """
    status = poll_status(api_key, audience_id)

    polls = 0
    while status not in ("COMPLETE", "FAILED") and polls < MAX_POLLS:
        time.sleep(POLL_INTERVAL)
        polls += 1
        status = poll_status(api_key, audience_id)
        if on_status:
            on_status(status)

    if status == "FAILED":
        raise RuntimeError(f"Audience {audience_id} failed.")

    if status != "COMPLETE":
        raise TimeoutError(
            f"Timed out after {polls} polls (last status: {status}). "
            f"Check manually: GET {BASE_URL}/api/v1/audiences/{audience_id}/"
        )

    return fetch_results(api_key, audience_id)
