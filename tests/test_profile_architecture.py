"""Architectural boundary tests for bundled and external game profiles."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from custom_components.nitrado_gameserver.plugins import api as public_api
from custom_components.nitrado_gameserver.plugins.base import profile_extension_manifest
from custom_components.nitrado_gameserver.plugins.palworld import PalworldProfile

ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = ROOT / "custom_components" / "nitrado_gameserver" / "plugins"
COMPONENT_DIR = PLUGINS_DIR.parent
CORE_PROFILE_PATHS = {
    Path("__init__.py"),
    Path("api.py"),
    Path("base.py"),
    Path("generic.py"),
    Path("registry.py"),
}
PUBLIC_API_MODULE = "custom_components.nitrado_gameserver.plugins.api"


class ProfileArchitectureTests(unittest.TestCase):
    """Prevent bundled adapters from quietly acquiring private core access."""

    def test_bundled_game_profiles_import_only_public_profile_api(self) -> None:
        violations: list[str] = []
        for path in sorted(PLUGINS_DIR.rglob("*.py")):
            relative = path.relative_to(PLUGINS_DIR)
            if relative in CORE_PROFILE_PATHS or "__pycache__" in relative.parts:
                continue
            adapter_package = None if len(relative.parts) == 1 else ".".join(relative.parts[:-1])
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        resolved = self._resolve_relative_import(relative, node.level, node.module)
                        if resolved != PUBLIC_API_MODULE and not self._inside_adapter_package(
                            resolved, adapter_package
                        ):
                            violations.append(
                                f"{relative}:{node.lineno} imports private relative module "
                                f"{'.' * node.level}{node.module or ''}"
                            )
                    elif (node.module or "").startswith("custom_components.nitrado_gameserver") and (
                        node.module != PUBLIC_API_MODULE
                        and not self._inside_adapter_package(node.module or "", adapter_package)
                    ):
                        violations.append(f"{relative}:{node.lineno} imports private module {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("custom_components.nitrado_gameserver") and (
                            alias.name != PUBLIC_API_MODULE
                            and not self._inside_adapter_package(alias.name, adapter_package)
                        ):
                            violations.append(f"{relative}:{node.lineno} imports private module {alias.name}")

        self.assertEqual(violations, [])

    def test_public_api_exports_palworld_baseline_requirements(self) -> None:
        expected = {
            "COCKPIT_API_VERSION",
            "PROFILE_API_VERSION",
            "RUNNING_STATUS",
            "SUPPORTED",
            "TRANSITION_STATUSES",
            "ActionDeclaration",
            "ActionInputDeclaration",
            "ActionInputType",
            "BaseGameProfile",
            "CapabilityState",
            "CapabilityVerdict",
            "CockpitDeclaration",
            "CockpitSnapshotContext",
            "ControlContext",
            "DataSource",
            "EditableFileDeclaration",
            "EntityDeclaration",
            "ExtensionAccess",
            "GameProfile",
            "LifecycleEvent",
            "LifecycleHookDeclaration",
            "LocalCockpitAsset",
            "MatchResult",
            "NitradoApiError",
            "NitradoService",
            "ParsedServer",
            "ProfileActionContext",
            "ProfileEntityContext",
            "ProfileOptionDeclaration",
            "ProfileOptionType",
            "ProfileReadTransport",
            "ProfileStatus",
            "ResourceContentFamily",
            "ResourceDeclaration",
            "SaveBundleDeclaration",
            "SurfaceDeclaration",
            "ValidatorDeclaration",
            "ValidatorDomain",
            "ValidatorTarget",
            "async_register_profile",
            "blocked",
            "first_string",
            "parse_int",
            "unsupported",
            "validate_action_payload",
            "validate_profile_option_value",
        }

        self.assertEqual(set(public_api.__all__), expected)
        self.assertNotIn("NitradoClient", public_api.__all__)
        self.assertEqual(
            tuple(inspect.signature(public_api.async_register_profile).parameters),
            ("hass", "export", "owner_domain", "cockpit_assets"),
        )
        signature = inspect.signature(public_api.async_register_profile)
        self.assertEqual(signature.parameters["owner_domain"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(signature.parameters["cockpit_assets"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_palworld_baseline_has_no_save_monitor_lifecycle_policy(self) -> None:
        manifest = profile_extension_manifest(PalworldProfile())

        self.assertEqual(manifest.lifecycle_hooks, ())
        self.assertNotIn("save_validation_available", {entity.key for entity in manifest.entities})

    def test_generic_cockpit_host_and_backend_do_not_interpret_palworld_truth(self) -> None:
        forbidden = (
            "palworld",
            "allow_insecure_rest",
            "player_reporting",
            "level.sav",
            "levelmeta.sav",
            "worldoption.sav",
            "localdata.sav",
        )
        generic_paths = (
            *(
                path
                for path in COMPONENT_DIR.rglob("*.py")
                if path != PLUGINS_DIR / "palworld.py" and "__pycache__" not in path.parts
            ),
            COMPONENT_DIR / "frontend" / "nitrado-game-server-panel.js",
        )
        violations: list[str] = []
        for path in generic_paths:
            source = path.read_text(encoding="utf-8").lower()
            for term in forbidden:
                if term in source:
                    violations.append(f"{path.relative_to(ROOT)} contains {term}")
        self.assertEqual(violations, [])

    @staticmethod
    def _resolve_relative_import(relative: Path, level: int, module: str | None) -> str:
        module_parts = ["custom_components", "nitrado_gameserver", "plugins", *relative.with_suffix("").parts]
        package_parts = module_parts if relative.name == "__init__.py" else module_parts[:-1]
        resolved = package_parts[: len(package_parts) - level + 1]
        if module:
            resolved.extend(module.split("."))
        return ".".join(resolved)

    @staticmethod
    def _inside_adapter_package(module: str, adapter_package: str | None) -> bool:
        if adapter_package is None:
            return False
        prefix = f"custom_components.nitrado_gameserver.plugins.{adapter_package}"
        return module == prefix or module.startswith(f"{prefix}.")


if __name__ == "__main__":
    unittest.main()
