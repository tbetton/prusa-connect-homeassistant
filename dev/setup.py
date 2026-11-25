"""Set up the dev Home Assistant against the dev Prusa Connect.

usage: python3 setup.py

Waits for both servers, finishes Home Assistant's onboarding with the dev
account, and adds the Prusa Connect integration by pairing: it starts the
pairing in Home Assistant and approves the code on the demo server. Every
step is skipped when it is already done, so it is safe to run after every
`make up`. Standard library only.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

HA = "http://127.0.0.1:8123"
CONNECT = f"http://127.0.0.1:{os.environ.get('CONNECT_PORT', '8090')}"
# Both run on the host network, so Home Assistant reaches the demo where the browser does.
CONNECT_FROM_HA = f"http://localhost:{os.environ.get('CONNECT_PORT', '8090')}"
HA_USER = os.environ.get("HA_USERNAME", "dev")
HA_PASSWORD = os.environ.get("HA_PASSWORD", "prusa-dev")
CONNECT_USER = "demo"
CONNECT_PASSWORD = os.environ.get("CONNECT_PASSWORD", "prusa-demo")
DOMAIN = "prusa_connect_ce"
CLIENT_ID = f"{HA}/"
WAIT = 300


def request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    form: bool = False,
) -> Any:
    headers = {}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["content-type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    with opener.open(urllib.request.Request(url, data, headers, method=method), timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def wait_for(name: str, url: str) -> None:
    deadline = time.monotonic() + WAIT
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5):
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if time.monotonic() > deadline:
                sys.exit(f"{name} did not answer at {url} within {WAIT} s; see `make logs`")
            time.sleep(2)


def ha_token(opener: urllib.request.OpenerDirector) -> str:
    """Onboard Home Assistant on its first start, otherwise log in."""
    try:
        steps = request(opener, "GET", f"{HA}/api/onboarding")
    except urllib.error.HTTPError as err:
        # Home Assistant stops serving onboarding once it is finished.
        if err.code != 404:
            raise
        steps = []
    if any(s["step"] == "user" and not s["done"] for s in steps):
        code = request(
            opener,
            "POST",
            f"{HA}/api/onboarding/users",
            {
                "client_id": CLIENT_ID,
                "name": "Developer",
                "username": HA_USER,
                "password": HA_PASSWORD,
                "language": "en",
            },
        )["auth_code"]
        print(f"home assistant: created the account {HA_USER}")
    else:
        flow = request(
            opener,
            "POST",
            f"{HA}/auth/login_flow",
            {"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": f"{HA}/"},
        )
        result = request(
            opener,
            "POST",
            f"{HA}/auth/login_flow/{flow['flow_id']}",
            {"client_id": CLIENT_ID, "username": HA_USER, "password": HA_PASSWORD},
        )
        if result.get("type") != "create_entry":
            sys.exit(f"home assistant refused {HA_USER}: {result.get('errors')}. `make clean` starts over.")
        code = result["result"]
    token: str = request(
        opener,
        "POST",
        f"{HA}/auth/token",
        {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
        form=True,
    )["access_token"]
    for step in steps:
        if step["done"] or step["step"] == "user":
            continue
        body = None
        if step["step"] == "integration":
            body = {"client_id": CLIENT_ID, "redirect_uri": f"{HA}/?auth_callback=1"}
        request(opener, "POST", f"{HA}/api/onboarding/{step['step']}", body or {}, token=token)
    return token


def connect_session() -> urllib.request.OpenerDirector:
    """Sign in to the demo server as the demo account."""
    session = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    request(
        session,
        "POST",
        f"{CONNECT}/api/v1/auth/login",
        {"username": CONNECT_USER, "password": CONNECT_PASSWORD},
    )
    return session


def wait_until_running(opener: urllib.request.OpenerDirector, token: str) -> None:
    """Home Assistant answers before it has set its integrations up: wait for that too."""
    deadline = time.monotonic() + WAIT
    while request(opener, "GET", f"{HA}/api/config", token=token).get("state") != "RUNNING":
        if time.monotonic() > deadline:
            sys.exit(f"Home Assistant did not finish starting within {WAIT} s; see `make logs`")
        time.sleep(2)


def add_integration(opener: urllib.request.OpenerDirector, token: str) -> None:
    """Add the integration the way a person does: pair it, and approve the code in Prusa Connect."""
    entries = request(opener, "GET", f"{HA}/api/config/config_entries/entry", token=token)
    for entry in (e for e in entries if e["domain"] == DOMAIN):
        if entry["state"] not in {"setup_error", "setup_retry"}:
            print("home assistant: the integration is already set up")
            return
        # The demo server sets its password again on every start, which ends the
        # tokens it handed out before: the entry is added again and paired anew.
        request(opener, "DELETE", f"{HA}/api/config/config_entries/entry/{entry['entry_id']}", token=token)
        print(f"home assistant: removed the entry that could not start ({entry['state']})")

    def step(flow_id: str, body: dict[str, Any]) -> Any:
        return request(opener, "POST", f"{HA}/api/config/config_entries/flow/{flow_id}", body, token=token)

    flow = request(opener, "POST", f"{HA}/api/config/config_entries/flow", {"handler": DOMAIN}, token=token)
    flow = step(flow["flow_id"], {"url": CONNECT_FROM_HA})
    if flow.get("step_id") != "auth":
        sys.exit(f"home assistant did not accept {CONNECT_FROM_HA}: {flow}")
    flow = step(flow["flow_id"], {"next_step_id": "pair"})
    if flow.get("type") != "progress":
        sys.exit(f"pairing did not start: {flow}")
    code = flow["description_placeholders"]["code"]
    print(f"home assistant: pairing code {code}, approving it in Prusa Connect")
    request(connect_session(), "POST", f"{CONNECT}/api/v1/auth/app-pairings/{code}/approve", {})
    deadline = time.monotonic() + 60
    while flow.get("type") == "progress" and time.monotonic() < deadline:
        time.sleep(1)
        flow = step(flow["flow_id"], {})
    if flow.get("type") != "create_entry":
        sys.exit(f"adding the integration failed: {flow}")
    print(f"home assistant: added {flow['title']} at {CONNECT_FROM_HA}")


def main() -> None:
    wait_for("Prusa Connect", f"{CONNECT}/api/v1/health")
    wait_for("Home Assistant", f"{HA}/manifest.json")
    opener = urllib.request.build_opener()
    token = ha_token(opener)
    wait_until_running(opener, token)
    add_integration(opener, token)
    print()
    print(f"  Home Assistant  {HA}   {HA_USER} / {HA_PASSWORD}")
    print(f"  Prusa Connect   {CONNECT}   {CONNECT_USER} / {CONNECT_PASSWORD}")


if __name__ == "__main__":
    main()
