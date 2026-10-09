"""A small client for the Bellhaven CRM sandbox API (built-in tools only).

The token is read from the environment variable BH_TOKEN. It is never written to
disk, never printed, and never put in an error message.
"""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_API = "https://analyst-assessment-production.up.railway.app/api/v1"


class CRMError(Exception):
    def __init__(self, message, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


def _account_from(resp):
    """The API may answer with the account itself or wrap it. Accept both."""
    if isinstance(resp, dict):
        if "account_id" in resp:
            return resp
        for key in ("data", "account"):
            if isinstance(resp.get(key), dict) and "account_id" in resp[key]:
                return resp[key]
    raise CRMError(f"Unexpected response shape from CRM: {str(resp)[:200]}")


class CRMClient:
    def __init__(self, token=None, api_base=None):
        self.token = token or os.environ.get("BH_TOKEN", "")
        if not self.token:
            raise CRMError("BH_TOKEN is not set. In PowerShell run:  $env:BH_TOKEN = \"your token\"")
        self.api = (api_base or os.environ.get("BH_API", DEFAULT_API)).rstrip("/")

    # -- low level -----------------------------------------------------------
    def _request(self, method, path, params=None, body=None, retries=3):
        url = self.api + path + (("?" + urlencode(params)) if params else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                   "User-Agent": "bellhaven-sync/1.0"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        # Only retry requests that are safe to repeat. A repeated POST could create two accounts.
        attempts = retries if method in ("GET", "PATCH") else 1
        last = None
        for attempt in range(attempts):
            try:
                req = Request(url, data=data, method=method, headers=headers)
                with urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return json.loads(raw) if raw.strip() else {}
            except HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")[:500]
                last = CRMError(f"{method} {path} failed with HTTP {exc.code}: {body_text}", exc.code, body_text)
                if exc.code < 500:
                    raise last
            except (URLError, TimeoutError, OSError) as exc:
                last = CRMError(f"{method} {path} could not reach the CRM: {exc}")
            time.sleep(1.0 * (attempt + 1))
        raise last

    # -- accounts --------------------------------------------------------------
    def list_accounts(self, **filters):
        """Return every account, following pages until the reported total is reached."""
        out, page = [], 1
        while page <= 100:
            params = {"page": page, "page_size": 200}
            params.update({k: v for k, v in filters.items() if v})
            resp = self._request("GET", "/accounts", params=params)
            rows = resp.get("data", []) if isinstance(resp, dict) else resp
            out.extend(rows)
            total = resp.get("total") if isinstance(resp, dict) else None
            if not rows or (total is not None and len(out) >= total):
                break
            page += 1
        return out

    def get_account(self, account_id):
        return _account_from(self._request("GET", f"/accounts/{account_id}"))

    def patch_account(self, account_id, fields):
        return self._request("PATCH", f"/accounts/{account_id}", body=fields)

    def create_account(self, fields):
        return _account_from(self._request("POST", "/accounts", body=fields))

    def me(self):
        return self._request("GET", "/me")
