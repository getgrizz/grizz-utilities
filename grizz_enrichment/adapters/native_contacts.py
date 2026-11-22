"""HubSpot and Salesforce contact adapters for the client-side contact sync.

Grizz never receives CRM credentials. These adapters run on the operator's
machine with the operator's own HubSpot / Salesforce credentials (from .env).
The Grizz API supplies only the write plan (people/prepare-writes): which
existing contact to update, and the values to write.

Every adapter offers the same small surface:
  connect()
  account_ids_by_gid(gids, gid_field)       -> {gid: account_id}
  contacts_for_account(account_id, fields)  -> [raw CRM contact records]
  existing_by_person_id(gids, field)        -> {gid: contact_id}   (whole CRM)
  existing_by_email(emails, field)          -> {email: (contact_id, person_id)}  (HubSpot only)
  update_contacts([{"Id": ..., **values}])  -> [{id, success, errors}]
  create_contacts([fields], key_field, account_ids)
                                            -> [{key, id, success, errors}]
"""

import os
import re
import time

import requests

_HS = "https://api.hubapi.com"
_HS_IN_BATCH = 50        # max values per IN filter
_HS_WRITE_BATCH = 100    # max records per batch create/update
_SF_BATCH = 200          # sObject Collections limit

# Salesforce record ids are 15 or 18 alphanumeric characters.  Anything else
# is refused before it can reach a SOQL string.
_SF_ID = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
# Custom field API names: letters, digits, underscores.
_SF_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _soql_quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


class HubSpotContactAdapter:
    """HubSpot contacts, via a private-app token in HUBSPOT_API_KEY."""

    native_fields = ["firstname", "lastname", "email"]

    def __init__(self):
        self._s: requests.Session | None = None

    def connect(self) -> None:
        token = os.environ.get("HUBSPOT_API_KEY")
        if not token:
            raise RuntimeError("HUBSPOT_API_KEY is not set.")
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {token}",
                                "Content-Type": "application/json"})
        resp = self._s.get(f"{_HS}/crm/v3/objects/contacts", params={"limit": 1}, timeout=10)
        if resp.status_code == 401:
            raise RuntimeError("HUBSPOT_API_KEY is invalid or lacks contact scopes.")
        resp.raise_for_status()

    # ── HTTP with 429 retry; never swallow a failure (a silent empty read
    #    turns updates into duplicate creates) ───────────────────────────────
    def _call(self, method: str, url: str, body: dict | None = None, retries: int = 4):
        for attempt in range(retries + 1):
            resp = self._s.request(method, url, json=body, timeout=30)
            if resp.status_code == 429 and attempt < retries:
                time.sleep(int(resp.headers.get("Retry-After", "10")))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"HubSpot {method} {url.split('/crm/')[-1]} -> "
                                   f"{resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise RuntimeError("HubSpot rate limit: retries exhausted")

    def _search(self, obj: str, prop: str, values: list[str], props: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(values), _HS_IN_BATCH):
            after = None
            while True:
                body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": "IN",
                                                       "values": values[i:i + _HS_IN_BATCH]}]}],
                        "properties": props, "limit": 100}
                if after:
                    body["after"] = after
                data = self._call("POST", f"{_HS}/crm/v3/objects/{obj}/search", body)
                out.extend(data.get("results", []))
                after = (data.get("paging") or {}).get("next", {}).get("after")
                if not after:
                    break
        return out

    def account_ids_by_gid(self, gids: list[str], gid_field: str) -> dict[str, str]:
        found: dict[str, list[str]] = {}
        for r in self._search("companies", gid_field, sorted(set(gids)), [gid_field]):
            g = (r.get("properties") or {}).get(gid_field)
            if g:
                found.setdefault(g, []).append(r["id"])
        # A gid on two companies is ambiguous: link neither (the caller reports it).
        return {g: ids[0] for g, ids in found.items() if len(ids) == 1}

    def contacts_for_account(self, account_id: str, fields: list[str]) -> list[dict]:
        ids: list[str] = []
        url = f"{_HS}/crm/v4/objects/companies/{account_id}/associations/contacts?limit=500"
        while url:
            data = self._call("GET", url)
            ids.extend(str(r["toObjectId"]) for r in data.get("results", []))
            nxt = (data.get("paging") or {}).get("next") or {}
            url = nxt.get("link")
        out: list[dict] = []
        props = sorted(set(self.native_fields + fields))
        for i in range(0, len(ids), _HS_WRITE_BATCH):
            data = self._call("POST", f"{_HS}/crm/v3/objects/contacts/batch/read",
                              {"inputs": [{"id": x} for x in ids[i:i + _HS_WRITE_BATCH]],
                               "properties": props})
            out.extend({"id": r["id"], "properties": r.get("properties") or {}}
                       for r in data.get("results", []))
        return out

    def existing_by_person_id(self, gids: list[str], field: str) -> dict[str, str]:
        """Contacts anywhere in the portal already carrying a Grizz person id —
        e.g. created earlier without a company link — so they're updated, not
        created again."""
        out: dict[str, str] = {}
        for r in self._search("contacts", field, sorted(set(gids)), [field]):
            g = (r.get("properties") or {}).get(field)
            if g:
                out.setdefault(g, r["id"])
        return out

    def existing_by_email(self, emails: list[str], person_id_field: str) -> dict[str, tuple]:
        """HubSpot keeps contact email unique across the portal, so a create with
        an email already in use fails.  Look them up first, with the Grizz person
        id each one already carries (so a different person is never merged in)."""
        emails = sorted({e.strip().lower() for e in emails if e and e.strip()})
        out: dict[str, tuple] = {}
        for r in self._search("contacts", "email", emails, ["email", person_id_field]):
            props = r.get("properties") or {}
            e = (props.get("email") or "").lower()
            if e:
                out.setdefault(e, (r["id"], props.get(person_id_field) or ""))
        return out

    def update_contacts(self, records: list[dict]) -> list[dict]:
        results: list[dict] = []
        for i in range(0, len(records), _HS_WRITE_BATCH):
            chunk = records[i:i + _HS_WRITE_BATCH]
            inputs = [{"id": r["Id"], "properties": {k: v for k, v in r.items() if k != "Id"}}
                      for r in chunk]
            try:
                data = self._call("POST", f"{_HS}/crm/v3/objects/contacts/batch/update",
                                  {"inputs": inputs})
                done = {str(x["id"]) for x in data.get("results", [])}
                results.extend({"id": r["Id"], "success": str(r["Id"]) in done,
                                "errors": [] if str(r["Id"]) in done else ["not updated"]}
                               for r in chunk)
            except Exception as e:
                results.extend({"id": r["Id"], "success": False, "errors": [str(e)]} for r in chunk)
        return results

    def create_contacts(self, records: list[dict], key_field: str,
                        account_ids: list[str | None]) -> list[dict]:
        """Batch-create, then pair each created id to its input by `key_field`
        (the Grizz person id), never by position: HubSpot doesn't guarantee
        batch order.  Then associate each new contact with its company."""
        results: list[dict] = []
        for i in range(0, len(records), _HS_WRITE_BATCH):
            chunk = records[i:i + _HS_WRITE_BATCH]
            accts = account_ids[i:i + _HS_WRITE_BATCH]
            keys = [r.get(key_field) for r in chunk]
            try:
                data = self._call("POST", f"{_HS}/crm/v3/objects/contacts/batch/create",
                                  {"inputs": [{"properties": r} for r in chunk]})
            except Exception as e:
                results.extend({"key": k, "id": None, "success": False, "errors": [str(e)]}
                               for k in keys)
                continue
            by_key = {(x.get("properties") or {}).get(key_field): x["id"]
                      for x in data.get("results", [])}
            for k, acct in zip(keys, accts):
                cid = by_key.get(k)
                if not cid:
                    results.append({"key": k, "id": None, "success": False,
                                    "errors": ["created record could not be paired by key"]})
                    continue
                res = {"key": k, "id": cid, "success": True, "errors": []}
                if acct:
                    try:
                        self._call("PUT", f"{_HS}/crm/v4/objects/contacts/{cid}"
                                          f"/associations/default/companies/{acct}")
                        res["linked"] = True
                    except Exception as e:
                        res["errors"].append(f"company link failed: {e}")
                results.append(res)
        return results


class SalesforceContactAdapter:
    """Salesforce Contacts, via SALESFORCE_* credentials in the local .env."""

    native_fields = ["FirstName", "LastName", "Email"]

    def __init__(self):
        self.sf = None

    def connect(self) -> None:
        from simple_salesforce import Salesforce
        self.sf = Salesforce(
            username=os.environ["SALESFORCE_USERNAME"],
            password=os.environ["SALESFORCE_PASSWORD"],
            security_token=os.environ["SALESFORCE_SECURITY_TOKEN"],
            domain=os.environ.get("SALESFORCE_DOMAIN", "login"),
        )

    def account_ids_by_gid(self, gids: list[str], gid_field: str) -> dict[str, str]:
        if not _SF_FIELD.match(gid_field):
            raise ValueError(f"not a Salesforce field name: {gid_field!r}")
        found: dict[str, list[str]] = {}
        uniq = sorted(set(gids))
        for i in range(0, len(uniq), _SF_BATCH):
            ins = ", ".join(_soql_quote(g) for g in uniq[i:i + _SF_BATCH])
            res = self.sf.query_all(f"SELECT Id, {gid_field} FROM Account WHERE {gid_field} IN ({ins})")
            for r in res.get("records", []):
                if r.get(gid_field):
                    found.setdefault(r[gid_field], []).append(r["Id"])
        return {g: ids[0] for g, ids in found.items() if len(ids) == 1}

    def contacts_for_account(self, account_id: str, fields: list[str]) -> list[dict]:
        if not _SF_ID.match(account_id or ""):
            raise ValueError(f"not a Salesforce record id: {account_id!r}")
        cols = ["Id"] + sorted({f for f in self.native_fields + fields if _SF_FIELD.match(f)})
        res = self.sf.query_all(f"SELECT {', '.join(cols)} FROM Contact "
                                f"WHERE AccountId = {_soql_quote(account_id)}")
        return [{k: v for k, v in r.items() if k != "attributes"} for r in res.get("records", [])]

    def existing_by_person_id(self, gids: list[str], field: str) -> dict[str, str]:
        if not _SF_FIELD.match(field):
            raise ValueError(f"not a Salesforce field name: {field!r}")
        out: dict[str, str] = {}
        uniq = sorted(set(gids))
        for i in range(0, len(uniq), _SF_BATCH):
            ins = ", ".join(_soql_quote(g) for g in uniq[i:i + _SF_BATCH])
            res = self.sf.query_all(f"SELECT Id, {field} FROM Contact WHERE {field} IN ({ins})")
            for r in res.get("records", []):
                if r.get(field):
                    out.setdefault(r[field], r["Id"])
        return out

    def existing_by_email(self, emails: list[str], person_id_field: str) -> dict[str, tuple]:
        return {}   # Salesforce allows duplicate emails; the person-id check covers re-runs

    def _collections(self, method: str, records: list[dict]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(records), _SF_BATCH):
            tagged = [{"attributes": {"type": "Contact"}, **r} for r in records[i:i + _SF_BATCH]]
            out.extend(self.sf.restful("composite/sobjects", method=method,
                                       json={"allOrNone": False, "records": tagged}))
        return out

    def update_contacts(self, records: list[dict]) -> list[dict]:
        return [{"id": r.get("id"), "success": bool(r.get("success")),
                 "errors": r.get("errors") or []}
                for r in self._collections("PATCH", records)]

    def create_contacts(self, records: list[dict], key_field: str,
                        account_ids: list[str | None]) -> list[dict]:
        """Create with AccountId set in the same call, then re-read the new ids'
        `key_field` and pair each created record to its input by that key, never
        by position."""
        if not _SF_FIELD.match(key_field):
            raise ValueError(f"not a Salesforce field name: {key_field!r}")
        rows = [{**r, **({"AccountId": a} if a else {})} for r, a in zip(records, account_ids)]
        res = self._collections("POST", rows)
        new_ids = [x["id"] for x in res if x.get("success") and x.get("id")]
        by_key: dict[str, str] = {}
        for i in range(0, len(new_ids), _SF_BATCH):
            ins = ", ".join(_soql_quote(x) for x in new_ids[i:i + _SF_BATCH])
            got = self.sf.query_all(f"SELECT Id, {key_field} FROM Contact WHERE Id IN ({ins})")
            for g in got.get("records", []):
                if g.get(key_field):
                    by_key[g[key_field]] = g["Id"]
        errors = [x.get("errors") for x in res if not x.get("success")]
        out = []
        acct_by_key = {r.get(key_field): a for r, a in zip(records, account_ids)}
        for r in records:
            k = r.get(key_field)
            cid = by_key.get(k)
            out.append({"key": k, "id": cid, "success": bool(cid),
                        "linked": bool(cid and acct_by_key.get(k)),
                        "errors": [] if cid else (["create failed"] + [str(e) for e in errors[:1]])})
        return out


NATIVE_CONTACT_ADAPTERS = {
    "hubspot": HubSpotContactAdapter,
    "salesforce": SalesforceContactAdapter,
}
