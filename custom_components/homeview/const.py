"""Constants for the Apple HomeView integration."""

DOMAIN = "homeview"

CONF_IDENTIFIER = "identifier"
CONF_CREDENTIALS = "credentials"
CONF_SPEAKERS = "speakers"  # options: {entity_id: airplay output-device id}

CLIENT_NAME = "Home Assistant"
HOMEVIEW_MODEL_PREFIX = "HomeAccessory"

# Only a suggested path for the optional "import credentials from a file" step.
# The file is read once during setup; the credentials are then kept in the
# config entry like any other HA integration secret.
DEFAULT_CREDENTIALS_FILE = "/config/homeview_airplay_credentials.txt"

from datetime import timedelta

RECONNECT_DELAYS = (5, 10, 30, 60)  # seconds, last value repeats
OUTPUT_REFRESH_INTERVAL = timedelta(seconds=60)
OUTPUT_SETTLE = 2.5  # seconds for HomeView to send its output list after connect
PENDING_HOLD = 90  # seconds a change we made is shown before HomeView confirms it
CONFIRM_DELAYS = (5, 20, 45, 80, 100)  # background re-reads after a change

SERVICE_REMOVE_MEMBERS = "remove_members"
SERVICE_SET_OUTPUTS = "set_outputs"
ATTR_OUTPUT_IDS = "output_ids"
