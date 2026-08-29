"""Palworld profile helpers."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

from .api import (
    COCKPIT_API_VERSION,
    PROFILE_API_VERSION,
    RUNNING_STATUS,
    SUPPORTED,
    TRANSITION_STATUSES,
    ActionDeclaration,
    BaseGameProfile,
    CapabilityVerdict,
    CockpitDeclaration,
    CockpitSnapshotContext,
    ControlContext,
    EditableFileDeclaration,
    EntityDeclaration,
    LifecycleHookDeclaration,
    LocalCockpitAsset,
    MatchResult,
    NitradoApiError,
    NitradoService,
    ParsedServer,
    ProfileOptionDeclaration,
    ProfileOptionType,
    ProfileReadTransport,
    ProfileStatus,
    ResourceDeclaration,
    SaveBundleDeclaration,
    SurfaceDeclaration,
    ValidatorDeclaration,
    blocked,
    first_string,
    parse_int,
)

PALWORLD_TRANSITION_STATUSES = TRANSITION_STATUSES | {"updating", "gs_installation"}
PALWORLD_SETTINGS_SECTION = "/Script/Pal.PalGameWorldSettings"
MAX_PALWORLD_SETTINGS_BYTES = 65_536


@dataclass(slots=True, frozen=True)
class ParsedPalworldSettings:
    """Lossless PalWorldSettings.ini parse with bounded validation facts."""

    raw_text: str
    entries: tuple[tuple[str, str], ...]
    issues: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class PalworldSettingSpan:
    """One exact value span inside the Palworld OptionSettings envelope."""

    key: str
    raw_value: str
    value_start: int
    value_end: int


@dataclass(slots=True, frozen=True)
class PalworldRestConfig:
    """REST API connection details read from PalWorldSettings.ini."""

    enabled: bool
    host: str | None
    port: int | None
    admin_password: str | None
    endpoints: tuple[tuple[str, int], ...] = ()


class PalworldProfile(BaseGameProfile):
    """Palworld-specific profile."""

    api_version = PROFILE_API_VERSION
    profile_id = "palworld"
    name = "Palworld"
    supported_games = ("palworld", "palworldxb")
    idle_shutdown_supported = True

    def matches(self, service: NitradoService, server: ParsedServer | None = None) -> MatchResult:
        """Match Palworld service metadata."""

        supported = {value.casefold() for value in self.supported_games}
        # Nitrado's service list currently puts the human label
        # ``Palworld Xbox`` in ``details.game`` while exposing the real machine
        # identifier as ``details.folder_short=palworldxb``.  Treat the folder
        # and gameserver fields as authoritative machine identifiers first;
        # otherwise a correct service is rejected merely because the human
        # label and machine ID use different spellings.
        machine_values = {
            value.strip().casefold()
            for value in (
                service.folder_short,
                server.game_short if server else None,
            )
            if isinstance(value, str) and value.strip()
        }
        if machine_values:
            if machine_values <= supported:
                return MatchResult(True, confidence=0.99, reason="Palworld machine identifier matched")
            return MatchResult(False)
        service_game = service.game.strip().casefold() if isinstance(service.game, str) else ""
        if service_game in supported:
            return MatchResult(True, confidence=0.95, reason="Palworld service game identifier matched")
        human_values = tuple(
            value.casefold()
            for value in (
                service.game if service_game else None,
                service.game_human,
                server.game_human if server else None,
            )
            if isinstance(value, str) and value
        )
        if human_values:
            if all("palworld" in value for value in human_values):
                return MatchResult(True, confidence=0.8, reason="Palworld human game metadata matched")
            return MatchResult(False)
        if isinstance(service.name, str) and "palworld" in service.name.casefold():
            return MatchResult(True, confidence=0.25, reason="Palworld service-name fallback matched")
        return MatchResult(False)

    async def enrich_status(
        self,
        client: ProfileReadTransport,
        service: NitradoService,
        server: ParsedServer,
        context: ControlContext,
    ) -> ProfileStatus:
        """Use Palworld REST as the only trusted Palworld player source."""

        base = ProfileStatus(
            player_count=server.player_count,
            player_max=server.player_max,
            player_names=server.player_names,
            player_source=server.player_source,
            # Nitrado's generic Palworld query has returned false zero-player
            # results in production. Preserve its values for diagnostics, but
            # never mark them valid for entities or automatic shutdown.
            query_valid=False,
            extra={"nitrado_query_trustworthy": False},
        )
        if server.raw_status != RUNNING_STATUS:
            return base

        if context.options.get("allow_insecure_rest") is not True:
            return ProfileStatus(
                player_count=base.player_count,
                player_max=base.player_max,
                player_names=base.player_names,
                player_source=base.player_source,
                query_valid=False,
                extra={**base.extra, "palworld_rest_blocked": "insecure_transport_not_approved"},
            )

        try:
            config = await fetch_palworld_rest_config(client, service, server)
        except NitradoApiError as err:
            return ProfileStatus(
                player_count=base.player_count,
                player_max=base.player_max,
                player_names=base.player_names,
                player_source=base.player_source,
                query_valid=base.query_valid,
                extra={**base.extra, "palworld_rest_error": err.__class__.__name__},
            )
        if not config or not config.enabled:
            return ProfileStatus(
                player_count=base.player_count,
                player_max=base.player_max,
                player_names=base.player_names,
                player_source=base.player_source,
                query_valid=base.query_valid,
                extra={**base.extra, "palworld_rest_enabled": False},
            )

        try:
            return await fetch_palworld_rest_status(client, server, config)
        except NitradoApiError as err:
            return ProfileStatus(
                player_count=base.player_count,
                player_max=base.player_max,
                player_names=base.player_names,
                player_source=base.player_source,
                query_valid=base.query_valid,
                extra={
                    **base.extra,
                    "palworld_rest_enabled": True,
                    "palworld_rest_error": err.__class__.__name__,
                },
            )

    async def suggest_display_name(
        self,
        client: ProfileReadTransport,
        service: NitradoService,
        server: ParsedServer | None,
    ) -> str | None:
        """Suggest the configured Palworld server name when file access allows it."""

        if server is None:
            return None
        try:
            content = await fetch_palworld_settings(client, service, server)
        except NitradoApiError:
            return server.server_name
        return first_string(parse_palworld_settings_server_name(content), server.server_name, service.name)

    async def can_start(self, context: ControlContext) -> CapabilityVerdict:
        """Add Palworld transition safety on top of generic checks."""

        if context.force:
            return SUPPORTED
        status = context.server.raw_status if context.server else None
        if status in PALWORLD_TRANSITION_STATUSES:
            return blocked(f"Palworld/Nitrado status is {status}; start was not sent.", overridable=True)
        return SUPPORTED

    async def can_stop(self, context: ControlContext) -> CapabilityVerdict:
        """No additional Palworld stop rule in the skeleton."""

        return SUPPORTED

    def idle_shutdown_capability(self, context: ControlContext) -> CapabilityVerdict:
        """Palworld supports idle shutdown through its native REST player data."""

        if (
            context.server is None
            or not context.status_fresh
            or context.using_cached_data
            or context.server.raw_status != RUNNING_STATUS
        ):
            return SUPPORTED
        if context.options.get("allow_insecure_rest") is not True:
            return blocked(
                "Palworld REST transport has not been approved by an administrator.",
                overridable=False,
            )
        if context.server is None or not context.server.query_valid or context.server.player_source != "palworld_rest":
            return blocked(
                "Palworld REST has not supplied a fresh trusted player count.",
                overridable=False,
            )
        return SUPPORTED

    def extra_entities(self) -> tuple[EntityDeclaration, ...]:
        """Declare Palworld profile-specific surfaces."""

        return (
            EntityDeclaration(
                "sensor",
                "player_source",
                "Player Source",
                value_fn=lambda context: context.server.player_source if context.server else None,
                attributes={"entity_category": "diagnostic", "entity_registry_enabled_default": False},
            ),
        )

    def profile_options(self) -> tuple[ProfileOptionDeclaration, ...]:
        """Declare administrator-only Palworld security consent."""

        return (
            ProfileOptionDeclaration(
                key="allow_insecure_rest",
                name="Allow Palworld REST over plaintext HTTP",
                option_type=ProfileOptionType.BOOLEAN,
                description=(
                    "Permit plaintext HTTP Basic authentication to the game-native Palworld REST API. "
                    "On hosted servers this credential may cross the public internet without encryption. "
                    "Use a unique REST password and leave this disabled unless you accept that risk."
                ),
                default=False,
                standard_options=True,
                onboarding=True,
                confirmation_required=True,
                acknowledgement_revision=1,
                idle_shutdown_required=True,
                repair_if_unacknowledged=True,
            ),
        )

    def editable_files(self) -> tuple[EditableFileDeclaration, ...]:
        """Declare Palworld editable text files for future editor surfaces."""

        return (
            EditableFileDeclaration(
                key="settings",
                name="PalWorldSettings.ini",
                description="Palworld server settings file.",
                path_fn=palworld_settings_path,
                parser=parse_palworld_settings_text,
                serializer=serialize_palworld_settings_text,
                validator=validate_palworld_settings_text,
                redactor=redact_palworld_settings_text,
                editor_modeler=palworld_settings_editor_model,
                editor_patcher=patch_palworld_settings_text,
                requires_restart=True,
                requires_stopped=True,
                create_backup=True,
            ),
        )

    def save_bundles(self) -> tuple[SaveBundleDeclaration, ...]:
        """Declare the active Palworld world as a portable save bundle."""

        return (
            SaveBundleDeclaration(
                key="world",
                name="Palworld world save",
                description="The active world and player save files for offline editing or migration.",
                root_fn=palworld_save_root,
                required_files=("Level.sav",),
                allowed_suffixes=(".sav",),
                excluded_paths=("backup",),
                editor_root_files=("Level.sav", "LevelMeta.sav", "WorldOption.sav", "LocalData.sav"),
                editor_root_directories=("Players",),
                requires_stopped=True,
                attributes={"editor_compatible": True, "merge_uploads": True},
            ),
        )

    def resources(self) -> tuple[ResourceDeclaration, ...]:
        """No Palworld read-only resources are exposed yet."""

        return ()

    def actions(self) -> tuple[ActionDeclaration, ...]:
        """No Palworld profile actions are exposed yet."""

        return ()

    def surfaces(self) -> tuple[SurfaceDeclaration, ...]:
        """No Palworld rich surfaces are exposed yet."""

        return ()

    def lifecycle_hooks(self) -> tuple[LifecycleHookDeclaration, ...]:
        """Palworld baseline does not own save-monitoring lifecycle policy."""

        return ()

    def validators(self) -> tuple[ValidatorDeclaration, ...]:
        """No reusable validators are declared yet."""

        return ()

    def cockpit(self) -> CockpitDeclaration:
        """Declare the Palworld-owned administrator cockpit."""

        return CockpitDeclaration(
            key="palworld",
            name="Palworld",
            cockpit_api_version=COCKPIT_API_VERSION,
            asset_key="palworld_cockpit",
            frontend_revision="sha256:b6c3fb2f67196413a021c55945717846a751b159ecadfc9aa050d1a9f2615ce0",
            default_route="overview",
            route_keys=("overview", "auto-shutdown", "save-games", "game-settings"),
            route_aliases=(("palworld", "settings", "game-settings"),),
        )

    def cockpit_assets(self) -> dict[str, LocalCockpitAsset]:
        """Return the package-owned module bound to the cockpit declaration."""

        return {
            "palworld_cockpit": LocalCockpitAsset(
                package_resource="frontend/palworld-cockpit.js",
                sha256="b6c3fb2f67196413a021c55945717846a751b159ecadfc9aa050d1a9f2615ce0",
            )
        }

    def cockpit_snapshot(self, context: CockpitSnapshotContext) -> dict[str, Any]:
        """Return secret-free Palworld reporting truth for its own cockpit."""

        key = "allow_insecure_rest"
        revision = 1
        configured = key in context.configured_options
        value = context.public_options.get(key)
        acknowledgement = context.option_acknowledgements.get(key, 0)
        if not configured:
            consent = {
                "status": "unresolved",
                "reason": "An administrator must explicitly choose whether to allow this connection.",
            }
        elif type(value) is not bool or type(acknowledgement) is not int or acknowledgement < 0:
            consent = {
                "status": "unavailable",
                "reason": "The saved administrator decision cannot be verified.",
            }
        elif acknowledgement < revision and not (revision == 1 and configured):
            consent = {
                "status": "unresolved",
                "reason": "The current security warning requires administrator acknowledgement.",
            }
        else:
            consent = {
                "status": "enabled" if value else "disabled",
                "reason": (
                    "Plaintext Palworld REST access is approved."
                    if value
                    else "Plaintext Palworld REST access is not approved."
                ),
            }

        server = context.server
        if consent["status"] != "enabled":
            verification = {
                "status": "not_testable",
                "reason": "Connection verification requires approved player reporting.",
            }
        elif server is None or not context.status_fresh or context.using_cached_data:
            verification = {
                "status": "unavailable",
                "reason": "Fresh server status is unavailable, so the connection cannot be verified.",
            }
        elif server.raw_status != RUNNING_STATUS:
            verification = {
                "status": "not_testable",
                "reason": "Connection verification resumes when the server is running.",
            }
        elif server.query_valid and server.player_source == "palworld_rest":
            verification = {
                "status": "verified",
                "reason": "Palworld REST supplied a fresh trusted player count.",
            }
        else:
            verification = {
                "status": "unverified",
                "reason": "Palworld REST has not supplied a fresh trusted player count.",
            }

        address = server.address if server is not None else None
        endpoint_label = (
            f"the Palworld REST endpoint configured for server {address}"
            if isinstance(address, str) and address
            else "the Palworld REST endpoint configured in PalWorldSettings.ini"
        )
        return {
            "player_reporting": {
                "consent": consent,
                "verification": verification,
                "endpoint_label": endpoint_label,
            }
        }


PROFILE = PalworldProfile


def parse_palworld_rest_config(content: str, fallback_address: str | None) -> PalworldRestConfig:
    """Extract only the REST fields needed from PalWorldSettings.ini."""

    settings = _palworld_setting_map(content)

    def setting(key: str) -> str | None:
        raw = settings.get(key.casefold())
        return None if raw is None else _decode_palworld_setting_value(raw)

    enabled = str(setting("RESTAPIEnabled") or "").lower() == "true"
    public_host = setting("PublicIP")
    public_port = parse_int(setting("PublicPort"))
    rest_port = parse_int(setting("RESTAPIPort"))
    fallback_host, fallback_port = split_host_port(fallback_address)
    endpoints = rest_endpoint_candidates(
        fallback_host=fallback_host,
        fallback_port=fallback_port,
        public_host=public_host,
        public_port=public_port,
        rest_port=rest_port,
    )
    return PalworldRestConfig(
        enabled=enabled,
        host=endpoints[0][0] if endpoints else None,
        port=endpoints[0][1] if endpoints else None,
        admin_password=setting("AdminPassword"),
        endpoints=tuple(endpoints),
    )


def parse_palworld_settings_server_name(content: str) -> str | None:
    """Extract the Palworld server display name from PalWorldSettings.ini."""

    raw = _palworld_setting_map(content).get("servername")
    if raw is None:
        return None
    value = _decode_palworld_setting_value(raw)
    return value or None


def _decode_palworld_setting_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return re.sub(r"\\([\\\"])", r"\1", value[1:-1])
    return value


def _palworld_setting_map(content: str) -> dict[str, str]:
    """Parse setting values without truncating quoted commas or nested tuples."""

    text = content if isinstance(content, str) else str(content)
    analyzed = _analyze_palworld_settings_text(text)
    if not analyzed.issues:
        return {key.casefold(): value for key, value in analyzed.entries}
    body = text.strip()
    assignment = re.search(r"OptionSettings\s*=\s*\(", body)
    if assignment is not None:
        open_index = assignment.end() - 1
        close_index, issue = _find_palworld_settings_closing_paren(body, open_index)
        if issue is None and close_index is not None:
            body = body[open_index + 1 : close_index]
    elif body.endswith(")"):
        body = body[:-1]
    entries, issues = _parse_palworld_setting_entries(body)
    return {} if issues else {key.casefold(): value for key, value in entries}


def parse_palworld_rest_players(players: list[Any]) -> tuple[str, ...]:
    """Parse Palworld REST player records without exposing IDs or IP addresses."""

    names: list[str] = []
    for index, player in enumerate(players, start=1):
        if isinstance(player, dict):
            name = first_string(player.get("name"), player.get("accountName"))
            names.append(name or f"Player {index}")
        elif isinstance(player, str) and player:
            names.append(player)
        else:
            names.append(f"Player {index}")
    return tuple(names)


def parse_palworld_rest_status(
    *,
    info: dict[str, Any],
    metrics: dict[str, Any],
    players_payload: dict[str, Any],
    fallback: ParsedServer,
) -> ProfileStatus:
    """Parse Palworld REST info/metrics/players into profile status."""

    players = players_payload.get("players")
    if not isinstance(players, list):
        raise NitradoApiError("Palworld REST players payload did not contain players list")

    player_names = parse_palworld_rest_players(players)
    player_count = parse_int(metrics.get("currentplayernum"))
    if player_count is None:
        player_count = len(player_names)
    elif player_count == 0 and player_names:
        raise NitradoApiError("Palworld REST metrics reported zero players but player list was not empty")

    player_max = parse_int(metrics.get("maxplayernum"))
    return ProfileStatus(
        player_count=player_count,
        player_max=player_max if player_max is not None else fallback.player_max,
        player_names=player_names,
        player_source="palworld_rest",
        query_valid=True,
        extra={
            "palworld_rest_enabled": True,
            "nitrado_query_trustworthy": False,
        },
        display_name=first_string(info.get("servername"), fallback.server_name),
    )


async def fetch_palworld_rest_config(
    client: ProfileReadTransport,
    service: NitradoService,
    server: ParsedServer,
) -> PalworldRestConfig | None:
    """Read Palworld REST settings from Nitrado's file browser."""

    content = await fetch_palworld_settings(client, service, server)
    return parse_palworld_rest_config(content, server.address)


async def fetch_palworld_settings(
    client: ProfileReadTransport,
    service: NitradoService,
    server: ParsedServer,
) -> str:
    """Download PalWorldSettings.ini through Nitrado's file browser."""

    root = await client.list_files()
    game_dir = find_palworld_game_dir(root, service=service, server=server)
    if not game_dir:
        raise NitradoApiError("Palworld game directory was not found in Nitrado file browser")

    settings_path = f"{game_dir}/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"
    return await client.download_file(settings_path)


async def palworld_settings_path(context) -> str | None:
    """Return the active Palworld settings path for editor integrations."""

    if not context.service or not context.server:
        return None
    root = await context.client.list_files()
    game_dir = find_palworld_game_dir(root, service=context.service, server=context.server)
    if not game_dir:
        return None
    return f"{game_dir}/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"


async def palworld_save_root(context) -> str | None:
    """Locate the one active Palworld world without guessing its world ID."""

    if not context.service or not context.server:
        return None
    root = await context.client.list_files()
    game_dir = find_palworld_game_dir(root, service=context.service, server=context.server)
    if not game_dir:
        return None
    worlds_root = f"{game_dir}/Pal/Saved/SaveGames/0"
    config_root = f"{game_dir}/Pal/Saved/Config/WindowsServer"
    settings_path = f"{config_root}/GameUserSettings.ini"
    config_listing = await context.client.list_files(config_root)
    settings_declared = any(
        entry.get("type") != "dir" and entry_name(entry).casefold() == "gameusersettings.ini"
        for entry in file_entries(config_listing)
    )
    if settings_declared:
        settings = await context.client.download_file(settings_path)
    else:
        settings = None
    if isinstance(settings, bytes):
        try:
            settings = settings.decode("utf-8")
        except UnicodeDecodeError as err:
            raise NitradoApiError("GameUserSettings.ini is not valid UTF-8") from err
    if settings is not None and not isinstance(settings, str):
        settings = None
    world_id: str | None = None
    if settings is not None:
        match = re.search(r"(?mi)^DedicatedServerName\s*=\s*([^\r\n]+?)\s*$", settings)
        if match is None:
            raise NitradoApiError("GameUserSettings.ini does not declare DedicatedServerName")
        world_id = match.group(1).strip()
        if (
            not world_id
            or world_id in {".", ".."}
            or "/" in world_id
            or "\\" in world_id
            or any(ord(char) < 32 or ord(char) == 127 for char in world_id)
        ):
            raise NitradoApiError("GameUserSettings.ini declares an unsafe DedicatedServerName")
    worlds = await context.client.list_files(worlds_root)
    directories = {
        entry_name(entry).casefold(): (entry_path(entry) or f"{worlds_root}/{entry_name(entry)}").rstrip("/")
        for entry in file_entries(worlds)
        if entry.get("type") == "dir"
    }
    if world_id is not None:
        selected = directories.get(world_id.casefold())
        if selected is None:
            raise NitradoApiError("The configured Palworld world is missing or does not contain Level.sav")
        children = await context.client.list_files(selected)
        if not any(
            child.get("type") != "dir" and entry_name(child).casefold() == "level.sav"
            for child in file_entries(children)
        ):
            raise NitradoApiError("The configured Palworld world is missing or does not contain Level.sav")
        return selected
    candidates: dict[str, str] = {}
    for name, path in directories.items():
        children = await context.client.list_files(path)
        if any(
            child.get("type") != "dir" and entry_name(child).casefold() == "level.sav"
            for child in file_entries(children)
        ):
            candidates[name] = path
    if len(candidates) != 1:
        if candidates:
            raise NitradoApiError("Multiple Palworld world saves were found and GameUserSettings.ini was unavailable")
        return None
    return next(iter(candidates.values()))


def parse_palworld_settings_text(content: str) -> str:
    """Preserve the exact raw text; validation performs a lossless analysis."""

    return content if isinstance(content, str) else str(content)


def _analyze_palworld_settings_text(content: str) -> ParsedPalworldSettings:
    """Analyze the settings envelope without rewriting unknown supported content."""

    text = content if isinstance(content, str) else str(content)
    issues: list[str] = []
    if len(text.encode("utf-8")) > MAX_PALWORLD_SETTINGS_BYTES:
        issues.append("The settings file exceeds the 64 KiB editor limit.")
    if any(ord(char) < 0x20 and char not in "\t\r\n" for char in text):
        issues.append("The settings file contains unsupported control characters.")

    section_matches = list(re.finditer(r"(?m)^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:[;#][^\r\n]*)?\r?$", text))
    matching_sections = [match for match in section_matches if match.group(1) == PALWORLD_SETTINGS_SECTION]
    if len(matching_sections) != 1:
        issues.append("The file must contain exactly one Palworld world-settings section.")
        return ParsedPalworldSettings(text, (), tuple(issues))

    section = matching_sections[0]
    following_sections = [match for match in section_matches if match.start() > section.start()]
    section_end = following_sections[0].start() if following_sections else len(text)
    assignments = list(re.finditer(r"(?m)^[ \t]*OptionSettings[ \t]*=", text[section.end() : section_end]))
    if len(assignments) != 1:
        issues.append("The Palworld world-settings section must contain exactly one OptionSettings assignment.")
        return ParsedPalworldSettings(text, (), tuple(issues))

    start = section.end() + assignments[0].end()
    while start < len(text) and text[start] in " \t":
        start += 1
    if start >= len(text) or text[start] != "(":
        issues.append("OptionSettings must use a parenthesized value envelope.")
        return ParsedPalworldSettings(text, (), tuple(issues))

    closing, scan_issue = _find_palworld_settings_closing_paren(text[:section_end], start)
    if scan_issue:
        issues.append(scan_issue)
        return ParsedPalworldSettings(text, (), tuple(issues))
    if closing is None or closing >= section_end:
        issues.append("OptionSettings does not have a closing parenthesis.")
        return ParsedPalworldSettings(text, (), tuple(issues))
    trailing_line = text[closing + 1 :].splitlines()[0] if closing + 1 < len(text) else ""
    if trailing_line.strip() and not trailing_line.lstrip().startswith((";", "#")):
        issues.append("OptionSettings has unexpected text after its closing parenthesis.")

    entries, entry_issues = _parse_palworld_setting_entries(text[start + 1 : closing])
    issues.extend(entry_issues)
    return ParsedPalworldSettings(text, entries, tuple(issues))


def _palworld_setting_spans(content: str) -> tuple[tuple[PalworldSettingSpan, ...], tuple[str, ...]]:
    """Return exact setting value spans after the structural validator succeeds."""

    parsed = _analyze_palworld_settings_text(content)
    if parsed.issues:
        return (), parsed.issues
    text = parsed.raw_text
    section = next(
        re.finditer(
            rf"(?m)^[ \t]*\[{re.escape(PALWORLD_SETTINGS_SECTION)}\][ \t]*(?:[;#][^\r\n]*)?\r?$",
            text,
        )
    )
    section_matches = list(re.finditer(r"(?m)^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:[;#][^\r\n]*)?\r?$", text))
    following_sections = [match for match in section_matches if match.start() > section.start()]
    section_end = following_sections[0].start() if following_sections else len(text)
    assignment = re.search(r"(?m)^[ \t]*OptionSettings[ \t]*=", text[section.end() : section_end])
    if assignment is None:
        return (), ("The Palworld world-settings section has no OptionSettings assignment.",)
    open_index = section.end() + assignment.end()
    while open_index < len(text) and text[open_index] in " \t":
        open_index += 1
    close_index, issue = _find_palworld_settings_closing_paren(text[:section_end], open_index)
    if issue or close_index is None or close_index >= section_end:
        return (), (issue or "OptionSettings does not have a closing parenthesis.",)

    boundaries: list[tuple[int, int]] = []
    part_start = open_index + 1
    quote = False
    escaped = False
    depth = 0
    for index in range(open_index + 1, close_index + 1):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
        elif char == '"':
            quote = True
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif (char == "," and depth == 0) or index == close_index:
            boundaries.append((part_start, index))
            part_start = index + 1

    spans: list[PalworldSettingSpan] = []
    for part_start, part_end in boundaries:
        part = text[part_start:part_end]
        match = re.fullmatch(
            r"([ \t]*)([A-Za-z][A-Za-z0-9_]*)([ \t]*)=([ \t]*)(.*?)([ \t]*)",
            part,
            flags=re.DOTALL,
        )
        if match is None:
            return (), ("OptionSettings contains an entry that cannot be edited losslessly.",)
        value_start = part_start + match.start(5)
        value_end = part_start + match.end(5)
        spans.append(PalworldSettingSpan(match.group(2), match.group(5), value_start, value_end))
    return tuple(spans), ()


def _palworld_sensitive_key(key: str) -> bool:
    return (
        re.search(
            r"password|secret|token|credential|(?:api|private|auth|access|client|encryption)_?key",
            key,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _palworld_contains_sensitive_assignment(raw_value: str) -> bool:
    return (
        re.search(
            r"(?i)(?:[A-Za-z0-9_]*(?:password|secret|token|credential)[A-Za-z0-9_]*"
            r"|[A-Za-z0-9_]*(?:api|private|auth|access|client|encryption)_?key[A-Za-z0-9_]*)[ \t]*=",
            raw_value,
        )
        is not None
    )


def _palworld_secret_placeholder(raw_value: str) -> bool:
    """Reject redaction markers that must never become saved credentials."""

    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    normalized = value.casefold()
    return normalized in {"[redacted]", "<redacted>", "redacted"} or (
        len(value) >= 3 and set(value) <= {"*", "•", "x", "X"}
    )


def palworld_settings_editor_model(content: str) -> dict[str, Any]:
    """Return a structured browser model without returning stored credentials."""

    text = content if isinstance(content, str) else str(content)
    spans, issues = _palworld_setting_spans(text)
    if issues:
        raise ValueError(issues[0])
    settings: list[dict[str, Any]] = []
    for span in spans:
        sensitive = _palworld_sensitive_key(span.key) or _palworld_contains_sensitive_assignment(span.raw_value)
        raw_value = span.raw_value.strip()
        configured = raw_value not in {'""', "''", ""}
        settings.append(
            {
                "key": span.key,
                "raw_value": None if sensitive else span.raw_value,
                "sensitive": sensitive,
                "configured": configured if sensitive else None,
            }
        )
    return {
        "schema_revision": "palworld-1.0-2026-08",
        "settings": settings,
        "redacted_source": redact_palworld_settings_text(text),
    }


def patch_palworld_settings_text(content: str, operations: Any) -> str:
    """Apply bounded setting operations by replacing value spans only."""

    if not isinstance(operations, list) or len(operations) > 256:
        raise ValueError("Structured settings operations must be a bounded list.")
    spans, issues = _palworld_setting_spans(content)
    if issues:
        raise ValueError(issues[0])
    by_key = {span.key.casefold(): span for span in spans}
    replacements: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") != "set":
            raise ValueError("Every structured setting operation must be an explicit set.")
        key = operation.get("key")
        raw_value = operation.get("raw_value")
        if not isinstance(key, str) or not isinstance(raw_value, str):
            raise ValueError("Structured setting operations require string keys and values.")
        folded = key.casefold()
        if folded in seen:
            raise ValueError(f"Structured settings contain duplicate operation {key}.")
        seen.add(folded)
        span = by_key.get(folded)
        if span is None:
            raise ValueError(f"Setting {key} is not present in the current file.")
        if len(raw_value.encode("utf-8")) > 16_384 or any(char in raw_value for char in "\r\n\x00"):
            raise ValueError(f"Setting {key} contains an unsupported value.")
        if (
            _palworld_sensitive_key(span.key) or _palworld_contains_sensitive_assignment(span.raw_value)
        ) and _palworld_secret_placeholder(raw_value):
            raise ValueError(f"Setting {key} cannot use a redaction placeholder as a credential.")
        replacements.append((span.value_start, span.value_end, raw_value))
    result = content
    for start, end, raw_value in sorted(replacements, reverse=True):
        result = f"{result[:start]}{raw_value}{result[end:]}"
    verdict = validate_palworld_settings_text(result)
    if not verdict.allowed:
        raise ValueError(verdict.reason)
    return result


def serialize_palworld_settings_text(value: Any) -> str:
    """Serialize the exact source text so unknown settings remain lossless."""

    if isinstance(value, ParsedPalworldSettings):
        return value.raw_text
    return value if isinstance(value, str) else str(value)


def validate_palworld_settings_text(value: Any) -> CapabilityVerdict:
    """Return a human-readable structural verdict for Palworld settings."""

    if not isinstance(value, str):
        return blocked("PalWorldSettings.ini could not be parsed safely.")
    parsed = _analyze_palworld_settings_text(value)
    if parsed.issues:
        return blocked(parsed.issues[0])
    return SUPPORTED


def _find_palworld_settings_closing_paren(text: str, start: int) -> tuple[int | None, str | None]:
    depth = 0
    quote = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            elif char in "\r\n":
                return None, "A quoted OptionSettings value crosses a line boundary."
            continue
        if char == '"':
            quote = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index, None
            if depth < 0:
                return None, "OptionSettings contains an unmatched closing parenthesis."
    if quote:
        return None, "OptionSettings contains an unterminated quoted value."
    return None, "OptionSettings contains an unterminated parenthesized value."


def _parse_palworld_setting_entries(body: str) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    parts: list[str] = []
    current: list[str] = []
    quote = False
    escaped = False
    depth = 0
    issues: list[str] = []
    for char in body:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            if depth == 0:
                issues.append("OptionSettings contains an unmatched nested parenthesis.")
            else:
                depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    if quote:
        issues.append("OptionSettings contains an unterminated quoted value.")
    if depth:
        issues.append("OptionSettings contains an unterminated nested value.")

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, part in enumerate(parts, start=1):
        if not part:
            issues.append(f"OptionSettings entry {index} is empty.")
            continue
        key, separator, raw_value = part.partition("=")
        key = key.strip()
        value = raw_value.strip()
        if separator != "=" or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) is None:
            issues.append(f"OptionSettings entry {index} does not have a valid setting key.")
            continue
        if not value and key != "DenyTechnologyList":
            issues.append(f"OptionSettings setting {key} has no value.")
        folded = key.casefold()
        if folded in seen:
            issues.append(f"OptionSettings contains duplicate setting {key}.")
        seen.add(folded)
        entries.append((key, value))
    if not entries:
        issues.append("OptionSettings does not contain any settings.")
    return tuple(entries), tuple(issues)


def redact_palworld_settings_text(content: str) -> str:
    """Redact Palworld secrets from settings previews/diffs."""

    text = content if isinstance(content, str) else str(content)
    spans, issues = _palworld_setting_spans(text)
    if not issues:
        result = text
        for span in sorted(
            (
                item
                for item in spans
                if _palworld_sensitive_key(item.key) or _palworld_contains_sensitive_assignment(item.raw_value)
            ),
            key=lambda item: item.value_start,
            reverse=True,
        ):
            result = f"{result[: span.value_start]}[redacted]{result[span.value_end :]}"
    else:
        result = text
    sensitive_key_pattern = (
        r"(?:[A-Za-z0-9_]*(?:password|secret|token|credential)[A-Za-z0-9_]*"
        r"|[A-Za-z0-9_]*(?:api|private|auth|access|client|encryption)_?key[A-Za-z0-9_]*)"
    )
    return re.sub(
        rf'(?i)({sensitive_key_pattern}[ \t]*=[ \t]*)("(?:\\.|[^"\\])*"|\'[^\'\r\n]*\'|[^,\)\r\n;#]*)',
        r"\1[redacted]",
        result,
    )


async def fetch_palworld_rest_status(
    client: ProfileReadTransport,
    server: ParsedServer,
    config: PalworldRestConfig,
) -> ProfileStatus:
    """Fetch and parse Palworld REST status from the first working endpoint."""

    if not config.endpoints or not config.admin_password:
        raise NitradoApiError("Palworld REST API is enabled but host, port, or admin password is missing")

    auth = base64.b64encode(f"admin:{config.admin_password}".encode()).decode()
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
    }
    last_error: NitradoApiError | None = None
    for host, port in config.endpoints:
        try:
            base_url = f"http://{host}:{port}"
            info = await client.fetch_external_json(f"{base_url}/v1/api/info", allowed_hosts=(host,), headers=headers)
            metrics = await client.fetch_external_json(
                f"{base_url}/v1/api/metrics", allowed_hosts=(host,), headers=headers
            )
            players = await client.fetch_external_json(
                f"{base_url}/v1/api/players", allowed_hosts=(host,), headers=headers
            )
            return parse_palworld_rest_status(info=info, metrics=metrics, players_payload=players, fallback=server)
        except NitradoApiError as err:
            last_error = err
    raise NitradoApiError(f"Palworld REST failed for all resolved endpoints: {last_error or 'unknown'}")


def find_palworld_game_dir(
    root_payload: dict[str, Any],
    *,
    service: NitradoService,
    server: ParsedServer,
) -> str | None:
    """Find the Palworld game directory in Nitrado's file browser root."""

    entries: dict[str, list[str]] = {}
    fuzzy: list[str] = []
    for entry in file_entries(root_payload):
        if entry.get("type") != "dir":
            continue
        name = entry_name(entry).lower()
        path = entry_path(entry)
        if not path:
            continue
        entries.setdefault(name, []).append(path.rstrip("/"))
        if "palworld" in name:
            fuzzy.append(path.rstrip("/"))
    supported = {value.casefold() for value in PalworldProfile.supported_games}
    for value in (server.game_short, service.folder_short, service.game):
        if not isinstance(value, str) or not value.strip():
            continue
        alias = value.strip().casefold()
        if alias not in supported:
            return None
        matches = entries.get(alias, [])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise NitradoApiError(f"Multiple exact {alias} game directories were found")
        # The first populated identifier is the most authoritative. Falling
        # through to a lower-priority alias can select a stale installation.
        return None
    exact = [path for alias in supported for path in entries.get(alias, [])]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise NitradoApiError("Multiple exact Palworld game directories were found without authoritative metadata")
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise NitradoApiError("Multiple possible Palworld game directories were found")
    return None


def split_host_port(address: str | None) -> tuple[str | None, int | None]:
    """Split host:port address."""

    if not address:
        return None, None
    host, separator, port_text = address.rpartition(":")
    if not separator:
        return address, None
    return host or None, parse_int(port_text)


def rest_endpoint_candidates(
    *,
    fallback_host: str | None,
    fallback_port: int | None,
    public_host: str | None,
    public_port: int | None,
    rest_port: int | None,
) -> list[tuple[str, int]]:
    """Build Palworld REST endpoint candidates."""

    candidates: list[tuple[str, int]] = []

    def add(host: str | None, port: int | None) -> None:
        if not host or not port:
            return
        candidate = (host, port)
        if candidate not in candidates:
            candidates.append(candidate)

    if fallback_host and fallback_port and rest_port:
        derived_port = rest_port
        if public_port:
            derived_port = fallback_port + (rest_port - public_port)
        add(fallback_host, derived_port)

    add(public_host, rest_port)
    add(fallback_host, rest_port)
    return candidates


def file_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return file browser entries from Nitrado payload variants."""

    data = payload.get("data")
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def entry_name(entry: dict[str, Any]) -> str:
    """Return a file-browser entry basename."""

    value = entry.get("name")
    if isinstance(value, str) and value:
        return value.rstrip("/").split("/")[-1]
    path = entry_path(entry)
    return path.rstrip("/").split("/")[-1] if path else ""


def entry_path(entry: dict[str, Any]) -> str | None:
    """Return a file-browser entry path."""

    value = entry.get("path") or entry.get("name")
    return value if isinstance(value, str) and value else None
