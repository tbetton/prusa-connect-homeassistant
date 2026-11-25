# Local Home Assistant

Runs a fresh Home Assistant with the integration from this checkout installed but not set up, so you can
go through both modes as a user would. Needs Docker (with compose), git, make and Python 3.11+.

```sh
cd dev
make up
```

The first run clones Prusa Connect into `dev/prusa-connect` and builds its images (the web app and the
server, a few minutes). Then:

| | URL | |
|---|---|---|
| Home Assistant | http://localhost:8123 | create the account on the first visit |
| Prusa Connect demo | http://localhost:8090 | an external server with simulated printers, `demo` / `prusa-demo` |
| Releases | http://localhost:8091 | development releases for the server Home Assistant runs itself |

Add **Prusa Connect** in Home Assistant and pick:

- **Inside Home Assistant**: Home Assistant installs the server from the development releases (signed with
  a key the releases service makes at its first start; `make up` writes its public half into this Home
  Assistant's `configuration.yaml`, and no other Home Assistant trusts it),
  runs it on port 8080, and shows the setup code for its first-run wizard. Then pair. To try an update,
  `make publish VERSION=0.0.1-alpha.2` and *Check for updates* on the **Prusa Connect Server** update
  entity.
- **Connect to a server**: the demo on port 8090, which Home Assistant also discovers. Pair with the
  `demo` account.

Either way **Prusa Connect** appears in the sidebar.

To connect printers to a server, `make printers` starts simulated ones (`tools/printersim` of Prusa
Connect: the real registration handshake, telemetry, commands) that dial the server inside Home Assistant
on port 8080:

```sh
make printers MODEL=MK4S COUNT=1     # models: MK4, MK4S, XL, CoreOne, MINI, MK3.9, …
make printers MODEL=XL PRINTERS_URL=http://localhost:8090   # or the demo
make printers-logs                   # their sessions and refusals
make printers-stop
```

A server that holds new printers at the door shows them in Prusa Connect to approve.

`make setup` connects to the demo for you instead: it creates the Home Assistant account `dev` /
`prusa-dev` and adds the integration by pairing, approving the code on the demo. Steps already done are
skipped.

Everything runs on the host network, so `localhost` is the same machine for Home Assistant and for your
browser.

## Working on the integration

| | |
|---|---|
| `make restart` | bundles `lib/` into the integration and restarts Home Assistant, after any code change |
| `make logs` | all logs; the integration logs at debug level, the embedded server under `custom_components.prusa_connect_ce.server.output` |
| `make publish VERSION=…` | publishes another development release |
| `make printers` / `make printers-stop` | connects simulated printers to a server / stops them |
| `make down` / `make up` | stop and start, keeping the data |
| `make clean` | removes the containers and all data; the next `make up` starts fresh |
| `make update-source` | moves `dev/prusa-connect` to the head of its branch; `make up` rebuilds |

The integration is mounted read only from `custom_components/prusa_connect_ce`. `make restart` runs
`scripts/vendor_lib.py sync` first, so a change in `lib/src` reaches Home Assistant too.

The demo sets its password again on every start, which ends the tokens it handed out. After a
`make down` / `make up` Home Assistant asks to sign in to it again; pair again, or run `make setup`.

## Settings

Pass them to make, for example `make up CONNECT_PRINTERS=6`.

| variable | default | |
|---|---|---|
| `PRUSA_CONNECT_SRC` | `dev/prusa-connect` | a Prusa Connect checkout to build instead of the clone |
| `PRUSA_CONNECT_REPO` | `https://github.com/prusa3d/prusa-connect.git` | where the clone comes from |
| `PRUSA_CONNECT_REF` | `main` | the branch or tag it clones |
| `CONNECT_PORT` | `8090` | the demo's port |
| `RELEASES_PORT` | `8091` | the development releases' port (`configuration.yaml` names it too) |
| `HA_VERSION` | `stable` | the Home Assistant image tag |
| `CONNECT_PRINTERS` | `3` | how many simulated printers dial in to the demo |

The images are built from Prusa Connect's own `docker/mockup/Dockerfile`: the demo runs `cmd/demo`, and
the development releases carry the full server build from its builder stage, as a release does.
