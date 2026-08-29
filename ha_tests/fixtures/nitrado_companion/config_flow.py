"""Minimal test companion config flow."""

from homeassistant import config_entries


class CompanionConfigFlow(config_entries.ConfigFlow, domain="nitrado_companion"):
    """Test-only flow; entries are created directly by the real-HA harness."""

    VERSION = 1
