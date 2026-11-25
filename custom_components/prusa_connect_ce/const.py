"""Constants for the Prusa Connect integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "prusa_connect_ce"

CONF_FINGERPRINT: Final = "certificate_fingerprint"
CONF_INSTANCE_ID: Final = "instance_id"

CONF_CAMERAS: Final = "cameras"
CONF_UPDATE_THROTTLE: Final = "update_throttle"
CONF_SIDEBAR: Final = "sidebar"
CONF_PANEL_URL: Final = "panel_url"
DEFAULT_CAMERAS: Final = True
DEFAULT_UPDATE_THROTTLE: Final = 1.0
DEFAULT_SIDEBAR: Final = True

PANEL_ICON: Final = "mdi:printer-3d-nozzle"

# Where the server runs: started by this integration inside Home Assistant, or
# a server of its own that Home Assistant connects to.
CONF_MODE: Final = "mode"
MODE_EMBEDDED: Final = "embedded"
MODE_EXTERNAL: Final = "external"
CONF_PORT: Final = "port"
DEFAULT_PORT: Final = 8080
# Offer alpha and beta releases of the embedded server, not only stable ones.
CONF_PRERELEASE: Final = "prerelease"
DEFAULT_PRERELEASE: Final = True

# The releases the embedded server is installed and updated from.
RELEASES_URL: Final = "https://api.github.com/repos/prusa3d/prusa-connect/releases"
# The Ed25519 keys Prusa Connect releases sign SHA256SUMS with, base64. They are
# the keys the server binaries trust for their own updates.
TRUSTED_KEYS: Final = ("a+q3iAUJBOsL/klWGowlRYcaI73uG/TEnMAEzAaszcw=",)
# How often the releases are checked for a newer server.
RELEASE_CHECK_INTERVAL: Final = timedelta(hours=6)

# configuration.yaml keys for development: another release source and the keys
# it signs with. A release that no key signed is never installed.
CONF_RELEASES_URL: Final = "releases_url"
CONF_TRUSTED_KEYS: Final = "trusted_keys"

# The event stream carries every change. This full refresh is only a safety net
# for a change that reaches no event, such as a camera declared on the server.
FALLBACK_REFRESH: Final = timedelta(minutes=5)

# How long to gather state-class events about one device before refetching it.
DEVICE_REFETCH_DELAY: Final = 0.5

# How long to wait before opening the event stream again after the server
# refused it outright, rather than dropped it.
STREAM_RESTART_DELAY: Final = 60

MANUFACTURER: Final = "Prusa Research"
# The integration's own name, apart from a Prusa Connect in the cloud.
INTEGRATION_NAME: Final = "Prusa Connect (on-premise) CE"
# The server's device model: what this integration connects to.
HUB_MODEL: Final = INTEGRATION_NAME
# The product: the server's name when it gives none, and the sidebar item.
PRODUCT_NAME: Final = "Prusa Connect"

# The states Prusa printers report through Prusa Connect. A driver of another
# vendor may report others; those are shown as the machine spells them.
PRUSA_STATES: Final = (
    "idle",
    "busy",
    "ready",
    "printing",
    "paused",
    "finished",
    "stopped",
    "attention",
    "error",
)
