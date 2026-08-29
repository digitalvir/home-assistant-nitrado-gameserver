"""Tests for persisted service option helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.const import (
    CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS,
    CONF_DRY_RUN_SERVICE_IDS,
    CONF_IDLE_MINUTES,
    CONF_IDLE_SHUTDOWN_SERVICE_IDS,
    CONF_IGNORED_SERVICE_IDS,
    CONF_IMPORTED_SERVICE_IDS,
    CONF_MAINTENANCE_SERVICE_IDS,
    CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS,
    CONF_PROFILE_OPTIONS,
    CONF_SERVICE_AREA_IDS,
    CONF_SERVICE_DISPLAY_NAMES,
    CONF_STARTUP_COOLDOWN_MINUTES,
    DEFAULT_DISCOVERY_INTERVAL,
    DEFAULT_DISCOVERY_MODE,
    DEFAULT_MISSING_SERVICE_THRESHOLD,
    DEFAULT_SETTLE_SECONDS,
    DEFAULT_STATUS_INTERVAL,
)
from custom_components.nitrado_gameserver.service_options import (
    clear_service_runtime_options,
    default_service_options,
    normalized_service_options,
    profile_option_acknowledged,
    profile_option_acknowledgements_map,
    profile_option_is_configured,
    updated_idle_number_options,
    updated_idle_toggle_options,
    updated_profile_option_acknowledgement_options,
    updated_profile_option_options,
    updated_service_options,
)


class ServiceOptionsTests(unittest.TestCase):
    """Service option helper tests."""

    def test_default_options_include_runtime_safety_shape(self) -> None:
        options = default_service_options()

        self.assertEqual(options["discovery_mode"], DEFAULT_DISCOVERY_MODE)
        self.assertEqual(options["discovery_interval"], DEFAULT_DISCOVERY_INTERVAL)
        self.assertEqual(options["status_interval"], DEFAULT_STATUS_INTERVAL)
        self.assertEqual(options["missing_service_threshold"], DEFAULT_MISSING_SERVICE_THRESHOLD)
        self.assertEqual(options["settle_seconds"], DEFAULT_SETTLE_SECONDS)
        self.assertEqual(options[CONF_IMPORTED_SERVICE_IDS], [])
        self.assertEqual(options[CONF_IGNORED_SERVICE_IDS], [])
        self.assertEqual(options[CONF_SERVICE_AREA_IDS], {})
        self.assertEqual(options[CONF_SERVICE_DISPLAY_NAMES], {})
        self.assertEqual(options[CONF_IDLE_SHUTDOWN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_MAINTENANCE_SERVICE_IDS], [])
        self.assertEqual(options[CONF_DRY_RUN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS], [])
        self.assertEqual(options[CONF_IDLE_MINUTES], {})
        self.assertEqual(options[CONF_STARTUP_COOLDOWN_MINUTES], {})
        self.assertEqual(options[CONF_PROFILE_OPTIONS], {})
        self.assertEqual(options[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS], {})

    def test_import_adds_imported_and_removes_ignored(self) -> None:
        options = updated_service_options(
            {
                CONF_IMPORTED_SERVICE_IDS: [],
                CONF_IGNORED_SERVICE_IDS: ["123"],
            },
            import_service_id="123",
        )

        self.assertEqual(options[CONF_IMPORTED_SERVICE_IDS], ["123"])
        self.assertEqual(options[CONF_IGNORED_SERVICE_IDS], [])

    def test_ignore_adds_ignored_and_removes_imported(self) -> None:
        options = updated_service_options(
            {
                CONF_IMPORTED_SERVICE_IDS: ["123"],
                CONF_IGNORED_SERVICE_IDS: [],
            },
            ignore_service_id="123",
        )

        self.assertEqual(options[CONF_IMPORTED_SERVICE_IDS], [])
        self.assertEqual(options[CONF_IGNORED_SERVICE_IDS], ["123"])

    def test_remove_clears_known_choice(self) -> None:
        options = updated_service_options(
            {
                CONF_IMPORTED_SERVICE_IDS: ["123"],
                CONF_IGNORED_SERVICE_IDS: ["456"],
                CONF_SERVICE_AREA_IDS: {"123": "games"},
                CONF_SERVICE_DISPLAY_NAMES: {"123": "Example Server"},
            },
            remove_service_id="123",
        )

        self.assertEqual(options[CONF_IMPORTED_SERVICE_IDS], [])
        self.assertEqual(options[CONF_IGNORED_SERVICE_IDS], ["123", "456"])
        self.assertEqual(options[CONF_SERVICE_AREA_IDS], {})
        self.assertEqual(options[CONF_SERVICE_DISPLAY_NAMES], {})

    def test_import_stores_display_name_and_area(self) -> None:
        options = updated_service_options(
            {
                CONF_IMPORTED_SERVICE_IDS: [],
                CONF_IGNORED_SERVICE_IDS: [],
            },
            import_service_id="123",
            service_area_id="games",
            service_display_name="Example Palworld Server",
        )

        self.assertEqual(options[CONF_SERVICE_AREA_IDS], {"123": "games"})
        self.assertEqual(options[CONF_SERVICE_DISPLAY_NAMES], {"123": "Example Palworld Server"})

    def test_normalize_restores_missing_runtime_option_keys(self) -> None:
        options = normalized_service_options(
            {
                CONF_IMPORTED_SERVICE_IDS: ["123"],
                CONF_IGNORED_SERVICE_IDS: [],
                CONF_SERVICE_AREA_IDS: {"123": "games"},
                CONF_SERVICE_DISPLAY_NAMES: {"123": "Example Server"},
            }
        )

        self.assertEqual(options[CONF_IDLE_SHUTDOWN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_MAINTENANCE_SERVICE_IDS], [])
        self.assertEqual(options[CONF_DRY_RUN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS], [])
        self.assertEqual(options[CONF_IDLE_MINUTES], {})
        self.assertEqual(options[CONF_STARTUP_COOLDOWN_MINUTES], {})
        self.assertEqual(options["discovery_mode"], DEFAULT_DISCOVERY_MODE)
        self.assertEqual(options["status_interval"], DEFAULT_STATUS_INTERVAL)

    def test_normalize_repairs_malformed_and_out_of_range_scalars(self) -> None:
        options = normalized_service_options(
            {
                "discovery_mode": "invented",
                "discovery_interval": "not-an-int",
                "status_interval": -10,
                "missing_service_threshold": True,
                "settle_seconds": 1.5,
            }
        )

        self.assertEqual(options["discovery_mode"], DEFAULT_DISCOVERY_MODE)
        self.assertEqual(options["discovery_interval"], DEFAULT_DISCOVERY_INTERVAL)
        self.assertEqual(options["status_interval"], DEFAULT_STATUS_INTERVAL)
        self.assertEqual(options["missing_service_threshold"], DEFAULT_MISSING_SERVICE_THRESHOLD)
        self.assertEqual(options["settle_seconds"], DEFAULT_SETTLE_SECONDS)

    def test_normalize_accepts_safe_integer_strings_and_repairs_bad_service_numbers(self) -> None:
        options = normalized_service_options(
            {
                "status_interval": "90",
                CONF_IDLE_MINUTES: {"123": 2, "456": 30, "789": 121},
                CONF_STARTUP_COOLDOWN_MINUTES: {"123": 0, "456": 10, "789": 61},
            }
        )

        self.assertEqual(options["status_interval"], 90)
        self.assertEqual(options[CONF_IDLE_MINUTES], {"456": 30.0})
        self.assertEqual(options[CONF_STARTUP_COOLDOWN_MINUTES], {"456": 10.0})

    def test_profile_options_drop_nonfinite_oversized_and_non_scalar_values(self) -> None:
        options = normalized_service_options(
            {
                CONF_PROFILE_OPTIONS: {
                    "example": {
                        "mode": {
                            "1": "safe",
                            "2": float("nan"),
                            "3": float("inf"),
                            "4": "x" * 20000,
                            "5": {"not": "scalar"},
                        }
                    }
                }
            }
        )

        self.assertEqual(options[CONF_PROFILE_OPTIONS], {"example": {"mode": {"1": "safe"}}})

    def test_profile_option_acknowledgement_is_versioned_and_legacy_explicit_value_migrates(self) -> None:
        legacy = updated_profile_option_options(
            {},
            "palworld",
            "allow_insecure_rest",
            "123",
            False,
        )

        self.assertTrue(profile_option_is_configured(legacy, "palworld", "allow_insecure_rest", "123"))
        self.assertTrue(profile_option_acknowledged(legacy, "palworld", "allow_insecure_rest", "123", 1))
        self.assertFalse(profile_option_acknowledged(legacy, "palworld", "allow_insecure_rest", "123", 2))

        revised = updated_profile_option_acknowledgement_options(
            legacy,
            "palworld",
            "allow_insecure_rest",
            "123",
            2,
        )
        self.assertEqual(
            profile_option_acknowledgements_map(revised),
            {"palworld": {"allow_insecure_rest": {"123": 2}}},
        )
        self.assertTrue(profile_option_acknowledged(revised, "palworld", "allow_insecure_rest", "123", 2))

    def test_service_choice_preserves_runtime_options(self) -> None:
        options = updated_service_options(
            {
                CONF_IMPORTED_SERVICE_IDS: ["123"],
                CONF_IGNORED_SERVICE_IDS: [],
                CONF_IDLE_SHUTDOWN_SERVICE_IDS: ["123"],
                CONF_MAINTENANCE_SERVICE_IDS: [],
                CONF_DRY_RUN_SERVICE_IDS: [],
                CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS: ["123"],
                CONF_IDLE_MINUTES: {"123": 15},
                CONF_PROFILE_OPTIONS: {
                    "palworld": {
                        "allow_insecure_rest": {"123": True},
                    }
                },
                CONF_STARTUP_COOLDOWN_MINUTES: {"123": 10},
            },
            import_service_id="456",
        )

        self.assertEqual(options[CONF_IMPORTED_SERVICE_IDS], ["123", "456"])
        self.assertEqual(options[CONF_IDLE_SHUTDOWN_SERVICE_IDS], ["123"])
        self.assertEqual(options[CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS], ["123"])
        self.assertEqual(options[CONF_IDLE_MINUTES], {"123": 15.0})
        self.assertEqual(options[CONF_STARTUP_COOLDOWN_MINUTES], {"123": 10.0})
        self.assertEqual(
            options[CONF_PROFILE_OPTIONS],
            {"palworld": {"allow_insecure_rest": {"123": True}}},
        )

    def test_idle_toggle_normalizes_runtime_option_keys(self) -> None:
        options = updated_idle_toggle_options(
            {
                CONF_IMPORTED_SERVICE_IDS: ["123"],
                CONF_IGNORED_SERVICE_IDS: [],
            },
            "123",
            CONF_IDLE_SHUTDOWN_SERVICE_IDS,
            True,
        )

        self.assertEqual(options[CONF_IDLE_SHUTDOWN_SERVICE_IDS], ["123"])
        self.assertEqual(options[CONF_MAINTENANCE_SERVICE_IDS], [])
        self.assertEqual(options[CONF_DRY_RUN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS], [])
        self.assertEqual(options[CONF_IDLE_MINUTES], {})
        self.assertEqual(options[CONF_STARTUP_COOLDOWN_MINUTES], {})
        self.assertEqual(options[CONF_PROFILE_OPTIONS], {})

    def test_idle_number_normalizes_runtime_option_keys(self) -> None:
        options = updated_idle_number_options(
            {
                CONF_IMPORTED_SERVICE_IDS: ["123"],
                CONF_IGNORED_SERVICE_IDS: [],
            },
            "123",
            CONF_IDLE_MINUTES,
            20,
        )

        self.assertEqual(options[CONF_IDLE_MINUTES], {"123": 20.0})
        self.assertEqual(options[CONF_IDLE_SHUTDOWN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_MAINTENANCE_SERVICE_IDS], [])
        self.assertEqual(options[CONF_DRY_RUN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_STARTUP_COOLDOWN_MINUTES], {})

    def test_clear_runtime_options_normalizes_skinny_options(self) -> None:
        options = clear_service_runtime_options(
            {
                CONF_IMPORTED_SERVICE_IDS: ["123"],
                CONF_IGNORED_SERVICE_IDS: [],
                CONF_IDLE_SHUTDOWN_SERVICE_IDS: ["123"],
                CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS: ["123"],
                CONF_IDLE_MINUTES: {"123": 15},
                CONF_PROFILE_OPTIONS: {"palworld": {"allow_insecure_rest": {"123": True}}},
                CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS: {"palworld": {"allow_insecure_rest": {"123": 1}}},
            },
            "123",
        )

        self.assertEqual(options[CONF_IMPORTED_SERVICE_IDS], ["123"])
        self.assertEqual(options[CONF_IDLE_SHUTDOWN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_MAINTENANCE_SERVICE_IDS], [])
        self.assertEqual(options[CONF_DRY_RUN_SERVICE_IDS], [])
        self.assertEqual(options[CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS], [])
        self.assertEqual(options[CONF_IDLE_MINUTES], {})
        self.assertEqual(options[CONF_STARTUP_COOLDOWN_MINUTES], {})
        self.assertEqual(options[CONF_PROFILE_OPTIONS], {})
        self.assertEqual(options[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS], {})


if __name__ == "__main__":
    unittest.main()
