"""Static architecture checks for the profile-owned cockpit frontend."""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "nitrado_gameserver"
HOST = COMPONENT / "frontend" / "nitrado-game-server-panel.js"
PALWORLD = COMPONENT / "frontend" / "palworld-cockpit.js"
PANEL = COMPONENT / "panel.py"
PALWORLD_PROFILE = COMPONENT / "plugins" / "palworld.py"


class FrontendCockpitContractTests(unittest.TestCase):
    """Protect the core-host/profile-cockpit ownership boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.host = HOST.read_text(encoding="utf-8")
        cls.palworld = PALWORLD.read_text(encoding="utf-8")

    def test_core_host_owns_provider_shell_and_composite_routes(self) -> None:
        self.assertIn('const PANEL_ROUTE = "/nitrado-game-servers";', self.host)
        self.assertIn("account_entry_id", self.host)
        self.assertIn("service_id", self.host)
        self.assertIn("Nitrado tools", self.host)
        self.assertIn("No specialized game cockpit is installed", self.host)
        self.assertIn("cockpit/bootstrap", self.host)
        self.assertIn("cockpit/capabilities", self.host)

    def test_palworld_navigation_and_reporting_copy_stay_in_palworld_module(self) -> None:
        for label in ("Overview", "Auto Shutdown", "Player reporting connection"):
            self.assertIn(label, self.palworld)
            self.assertNotIn(label, self.host)
        self.assertIn('["game-settings", "Game settings"]', self.palworld)
        for internal in ("allow_insecure_rest", "player_reporting", "palworld"):
            self.assertNotIn(internal, self.host.lower())

    def test_release_keeps_ini_editor_absent(self) -> None:
        combined = self.host + self.palworld
        self.assertNotIn("Open editor", combined)
        self.assertNotIn("Apply settings file", combined)
        self.assertNotIn("PalWorldSettings.ini editor", combined)

    def test_host_uses_server_side_one_time_handoff(self) -> None:
        self.assertIn("const result = await this._capability(action, {});", self.host)
        self.assertIn('type: "auth/sign_path"', self.host)
        self.assertIn("popup.location.replace(await this._signedHandoffPath(result.path))", self.host)
        self.assertIn('signedUrl.searchParams.has("authSig")', self.host)
        self.assertNotIn("access_token", self.host)
        self.assertNotIn("webinterface.nitrado.net", self.host)
        self.assertNotIn("localStorage", self.host)

    def test_accessibility_contract_is_present(self) -> None:
        self.assertIn('class="skip"', self.host)
        self.assertIn('aria-live="polite"', self.host)
        self.assertIn("min-height:44px", self.host)
        self.assertIn("prefers-reduced-motion", self.palworld)
        self.assertIn('role="tablist"', self.palworld)
        self.assertIn('aria-selected="', self.palworld)

    def test_admin_panel_identity_matches_host_module(self) -> None:
        panel = PANEL.read_text(encoding="utf-8")
        match = re.search(r'^PANEL_ASSET_VERSION = "([^"]+)"$', panel, re.MULTILINE)
        self.assertIsNotNone(match)
        identity = match.group(1)
        self.assertIn(f'const PANEL_ELEMENT = "nitrado-game-server-panel-{identity}";', self.host)
        self.assertIn('sidebar_title="Nitrado Servers"', panel)
        self.assertIn("require_admin=True", panel)
        self.assertIn("show_in_sidebar=True", panel)

    def test_palworld_profile_binds_exact_module_digest(self) -> None:
        digest = hashlib.sha256(PALWORLD.read_bytes()).hexdigest()
        profile = PALWORLD_PROFILE.read_text(encoding="utf-8")
        self.assertEqual(profile.count(digest), 2)
        self.assertIn(f'frontend_revision="sha256:{digest}"', profile)
        self.assertIn(f'sha256="{digest}"', profile)


if __name__ == "__main__":
    unittest.main()
