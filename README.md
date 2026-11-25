# Prusa Connect (on-premise) for Home Assistant

[![Release](https://img.shields.io/github/v/release/prusa3d/prusa-connect-homeassistant)](https://github.com/prusa3d/prusa-connect-homeassistant/releases)
[![HACS: custom repository](https://img.shields.io/badge/HACS-custom_repository-41BDF5)](#marketplace-or-hacs)
[![Home Assistant 2025.8+](https://img.shields.io/badge/Home_Assistant-2025.8%2B-18BCF2?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![CI](https://github.com/prusa3d/prusa-connect-homeassistant/actions/workflows/ci.yml/badge.svg)](https://github.com/prusa3d/prusa-connect-homeassistant/actions/workflows/ci.yml)
[![Validate](https://github.com/prusa3d/prusa-connect-homeassistant/actions/workflows/validate.yml/badge.svg)](https://github.com/prusa3d/prusa-connect-homeassistant/actions/workflows/validate.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)

**Prusa Connect (on-premise)** is a Home Assistant integration for
[Prusa Connect](https://github.com/prusa3d/prusa-connect), the self-hosted server your Prusa printers
connect to. The name sets it apart from the Prusa Connect cloud service; its domain is `prusa_connect`.
It works in one of two ways:

- **Inside Home Assistant.** The integration downloads the Prusa Connect server, checks its signature,
  runs it and installs its updates. Your printers connect to Home Assistant's address.
- **With a server of its own.** Prusa Connect runs elsewhere (a Raspberry Pi, a NAS, Docker) and Home
  Assistant connects to it.

Either way the integration talks only to the server's public HTTP API (`/api/v1`) and its event stream
(`/api/v1/events`), and puts the Prusa Connect web app in the sidebar, under **Prusa Connect**.

- Domain: `prusa_connect`, IoT class `local_push` (updates arrive over server-sent events)
- One config entry per Prusa Connect server (keyed by its `instance_id`); one device per printer, linked to
  a hub device for the server
- Found automatically over zeroconf (`_prusa-connect._tcp`), or added by URL
- Licence: MIT (this repository: the integration and the client library). Prusa Connect itself keeps its own licence.

> This replaces the old `easy_connect` integration. The domain changed, so remove the old integration
> (and its `custom_components/easy_connect` folder) before installing this one. Existing `easy_connect`
> entities are not migrated.

## Requirements

- Home Assistant 2025.8 or newer
- Inside Home Assistant: a 64-bit Intel/AMD or ARM machine, or 32-bit ARMv7, and a free port for the
  printers and the web app (8080 by default)
- With a server of its own: Prusa Connect from the public `prusa3d/prusa-connect` repository
- An account in Prusa Connect that may operate the printers (to approve the pairing)

## Installation

### Marketplace or HACS

> [!NOTE]
> The integration is not in the default catalog of the Marketplace and HACS yet. The registration is in
> progress. Until then, add this repository as a custom repository, as below.

[![Open your Home Assistant instance and open this repository in the Marketplace or HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=prusa3d&repository=prusa-connect-homeassistant&category=integration)

The button opens this repository in the Marketplace (*Settings → Marketplace*, Home Assistant 2026.11 and
newer) or in HACS on an older Home Assistant, and offers to add it first. Install
**Prusa Connect (on-premise)**. The Marketplace loads it without a restart; HACS asks you to restart.

Without the button: HACS → ⋮ → *Custom repositories* → add `https://github.com/prusa3d/prusa-connect-homeassistant`,
category *Integration*.

Both install `prusa_connect.zip` from the GitHub release. Releases marked as pre-release (alpha, beta) are
offered only when you turn on *Pre-release* for this repository. The integration's releases are full
releases; the Prusa Connect server it installs is in beta, see [Inside Home Assistant](#inside-home-assistant).

### Manual

Unzip `prusa_connect.zip` from a GitHub release into `custom_components/prusa_connect` in your
Home Assistant configuration (or copy that folder from this repository) and restart.

The integration carries its client library with it, so nothing has to be installed from PyPI. See
[The client library](#the-client-library).

## Configuration

*Settings → Devices & services → Add integration → Prusa Connect (on-premise)* asks where the server
runs.

### Inside Home Assistant

1. **Port and address.** Choose the port the server listens on (8080) and the address of the device
   running Home Assistant. Printers and browsers reach the server there. Home Assistant preselects the
   device's own IP address and also offers its internal URL's host and `homeassistant.local`, or you can
   type another. A reverse proxy in front of Home Assistant does not forward this port, so its address
   will not work here.
2. **Install.** Home Assistant downloads the newest release from the `prusa3d/prusa-connect` GitHub
   releases (alpha and beta included, see *Options*), checks the Ed25519 signature over `SHA256SUMS` with
   Prusa Connect's release key and the binary's SHA-256, and starts it. Nothing unsigned is ever run.
3. **First run.** Open the link Home Assistant shows and go through Prusa Connect's first-run wizard. It
   asks for the setup code Home Assistant shows. Create your administrator there.
4. **Pair**, as below. The flow then creates the entry.

The server keeps everything under `<config>/prusa_connect/` (the binary, its settings and database, the
uploaded files), so a Home Assistant backup includes it. Home Assistant restarts the server if it stops
and stops it with Home Assistant. The server's own update page is switched off: updates come through
the **Prusa Connect Server** update entity. Installing one downloads and checks the new release, copies
the server's settings and database aside, swaps the binary and starts it; if the new version does not
start, the previous one and its data come back.

### With a server of its own

1. **Address.** Accept the discovered *Prusa Connect*, or choose *Connect to a server* and enter the
   server's address with its port, for example `https://192.168.1.10:8080`.
2. **Certificate.** A server that uses its own certificate authority (the Prusa Connect setup wizard makes
   one) is not trusted by Home Assistant. The flow then shows the SHA-256 fingerprint of the server's
   certificate. Compare it with the one Prusa Connect shows and confirm: Home Assistant stores the
   fingerprint and trusts exactly that certificate. If the server gets a new certificate, use
   *Reconfigure* to trust the new one.
3. **Pair.** Choose *Pair with a code*. Home Assistant shows a code and a link to `/pair` on your server.
   Open it, sign in to Prusa Connect, check the code and approve *Home Assistant*. The flow continues on
   its own and keeps the token the server hands out. Home Assistant asks for the *operate* scope: reading,
   plus the buttons, the temperature, speed and flow numbers, and the chamber light and fan. You may approve only *read*; the sensors then
   work and the commands are refused. The code is valid for ten minutes.

   *Paste an API token* is the other way: create a credential in Prusa Connect under
   *Settings → API credentials* (`/settings/tokens`) and paste the token (`pcl_…`).

When the token expires or is revoked, Home Assistant asks to pair again or for a new token
(*reauthentication*). The server's address can be changed with *Reconfigure*.

### Options

| Option | Default | Meaning |
|---|---|---|
| Camera entities | on | Create a camera entity for every camera declared in Prusa Connect |
| Minimum time between state updates | 1 s | Readings arrive several times a second; they are written to Home Assistant at most this often |
| Show the web app in the sidebar | on | A *Prusa Connect* item in the sidebar opens the web app in a frame; you sign in to Prusa Connect there |
| Web app address | the server's address | The address your browser opens Prusa Connect at, when it differs from the one Home Assistant uses |
| Offer alpha and beta releases | on | Inside Home Assistant only: the update entity offers pre-releases too |

The sidebar frame is loaded by your browser, not by Home Assistant: the browser has to reach the address,
and a Home Assistant served over https can only frame an https Prusa Connect. Signing in inside the frame
works when Home Assistant and Prusa Connect share a host name or IP address (the session cookie is
`SameSite=Lax`); on different hosts, open Prusa Connect in its own tab.

## What you get

Tools are numbered from 1 in entity names, as on the printer's screen (*Tool 1*, *Tool 1 slot 3*). Unique
IDs use the 0-based tool ids of the Prusa Connect API (`0`, `0.2`), so a name can change without breaking
history.

| Device | Entities |
|---|---|
| Server (hub) | Device only: version and a link to the web app |
| Printer | State (enum, build volume as attributes), progress, time remaining, start time, finish time, file, thumbnail of the file being printed (image), print time (disabled by default), bed temperature, X/Y/Z position (diagnostic, disabled by default), connected (diagnostic), pause / resume / stop buttons, bed target, print speed and flow numbers |
| Chamber (CORE One) | Chamber temperature, chamber target (number), chamber light (dimmable), chamber fan (speed in percent, or *auto* to leave it to the printer), chamber fan 1 and 2 speed (diagnostic) |
| Head `/tools/<n>` | Nozzle temperature, nozzle target (number, `0` switches the heater off), active, material (heads without a feeder), nozzle diameter, high-flow and hardened nozzle, print and heatbreak fan speed (diagnostic) |
| Feeder (MMU) on a head | Feeder progress code and current operation (diagnostic) |
| Feeder slot `/tools/<n>.<k>` | Material, active |
| Camera | Still images from the server's snapshots; the live view is proxied when the camera offers one |

Entities are created from what each printer reports, so an MK4S with an MMU gets slot entities, an XL gets
one set per head, and a head switched off on the printer is left out (or becomes unavailable when it is
switched off later). Printers and heads that appear later get their entities without a restart; a printer
removed from Prusa Connect is removed from Home Assistant. Buttons and numbers are only created for
printers the token may *operate*, and only for verbs the printer accepts; the same goes for the chamber light and fan, and for the thumbnail, which the server only serves to a caller that may operate the printer. Buttons are available only in
states where they make sense (pause while printing, resume while paused).

Printers of other vendors connected through a Prusa Connect driver get the same entities for whatever
their driver reports.

### How data is updated

At start the integration reads every printer once (`GET /api/v1/devices`), then keeps one event stream
open. Telemetry patches the state in place; arrivals, departures and job events re-read the printer. After
a reconnect, or when the server says the stream has a gap, everything is read again, because telemetry is
not replayed. A full refresh also runs every five minutes as a safety net. When the server is unreachable,
the entities become unavailable until it answers again.

### Known limitations

- A server whose address changes must be updated with *Reconfigure*. The entry is keyed by the server's id
  (`instance_id`), so the same server is never added twice, but discovery does not move the address on its own.
- No services yet (printing a file, cancelling an object), no dialog events. The only update entity is
  the one for a server that runs inside Home Assistant.
- The same printer added through the `prusalink` integration as well appears as a second device.

## Removal

*Settings → Devices & services → Prusa Connect (on-premise) → ⋮ → Delete*, then remove the integration in HACS.
Revoke its credential in Prusa Connect under *Settings → API credentials* (a paired one is named
*Home Assistant (…)*).

## Diagnostics

*Download diagnostics* on the integration includes the server version, the stream state and every printer
record. The token, serial numbers, names and network details are redacted.

## The client library

`lib/` holds `prusa-connect`, a standalone async client (aiohttp, typed, MIT) for the Prusa Connect
API and event stream. It has its own `pyproject.toml`, tests and README, and does not import anything from
Home Assistant, so it can move to its own public repository unchanged. It is the only place the library is
edited.

### Bundled or from PyPI

The library is not on PyPI yet, so the integration bundles it. One switch in `pyproject.toml` decides how a
release ships it:

```toml
[tool.prusa-connect.packaging]
library = "bundled"   # or "pypi"
provided-by-home-assistant = ["aiohttp"]
```

| | `bundled` (now) | `pypi` |
|---|---|---|
| Library in the release | copy in `custom_components/prusa_connect/_vendor/` | left out |
| `manifest.json` `requirements` | the library's dependencies Home Assistant does not ship (none today) | `prusa-connect==<lib version>` |
| Import | `library.py` imports the bundled copy | `library.py` falls back to the installed package |

- The repository always holds the bundled shape: `_vendor/` is generated from `lib/src` by
  `scripts/vendor_lib.py sync` and committed (HACS validation and hassfest see it). Never edit it by hand;
  CI runs `scripts/vendor_lib.py check` and fails when it differs from `lib/` or when `manifest.json` does
  not match.
- The integration imports the library only through `custom_components/prusa_connect/library.py`, and so
  do the tests. `lib/` is not installed for the integration tests.
- The release workflow builds the shipped folder with `scripts/vendor_lib.py build`, which applies the
  mode: in `pypi` mode it removes `_vendor/` and pins the package in `manifest.json`. A release in `pypi`
  mode refuses to run unless the `PYPI_PUBLISH` repository variable is `true`, and the GitHub release waits
  for the PyPI upload. `scripts/vendor_lib.py build --mode pypi --out build` shows the result locally.

Switching to PyPI: set up a trusted publisher on PyPI and `PYPI_PUBLISH=true`, set `library = "pypi"`, and
tag a release. Before a Home Assistant core submission `lib/` has to move to a public repository (for
example `prusa3d/python-prusa-connect`), as HA requires the dependency's source and CI to be
public; `_vendor/` and `library.py`'s fallback then go away.

## Development

To try the integration in a real Home Assistant, `cd dev && make up` starts one next to a Prusa Connect
demo server with simulated printers and connects them; see [dev/README.md](dev/README.md).

Tests and checks:

```sh
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
scripts/vendor_lib.py sync   # after every change in lib/src
scripts/vendor_lib.py check && ruff check . && ruff format --check . && mypy custom_components lib/src scripts
pytest tests                 # integration (pytest-homeassistant-custom-component), on the bundled copy

python3.13 -m venv lib/.venv && lib/.venv/bin/pip install -e './lib[test]'
(cd lib && .venv/bin/pytest) # library, in its own environment
```

The test fixtures in `lib/tests/fixtures` were recorded from the Prusa Connect demo
(`go run -tags "full diagnostics" ./cmd/demo`).

## Brand

The icon ships in `custom_components/prusa_connect/brand/`; Home Assistant 2026.3 and newer show it, an
older one shows a placeholder. A submission to Home Assistant core moves it to `home-assistant/brands`,
under one `prusa` brand grouping `prusalink` and `prusa_connect`.

## Publishing

`scripts/catalog_pr.py` adds this repository to the catalog the Marketplace and HACS read
([hacs/default](https://github.com/hacs/default)):

```sh
scripts/catalog_pr.py check        # what the catalog review checks, read from GitHub
scripts/catalog_pr.py open --dry-run
scripts/catalog_pr.py open         # fork hacs/default, add the entry, open the pull request
```

`check` passes only when the repository is public, has a description, topics, issues and a licence,
its latest *full* release (a pre-release does not count) matches `manifest.json`, the icon is in
`brand/`, and the newest Validate run on `main` passed both the HACS action (without `ignore`) and
hassfest. `open` runs `check` first and fills in the pull request template with those links. The pull
request has to come from your own GitHub account, not an organisation, and you have to own the
repository or be one of its main contributors.
