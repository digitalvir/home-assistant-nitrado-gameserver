"""Generic Nitrado game profile."""

from __future__ import annotations

from ..api.nitrado import NitradoService, ParsedServer
from ..const import RUNNING_STATUS, STOPPED_STATUS
from .base import (
    PROFILE_API_VERSION,
    SUPPORTED,
    ActionDeclaration,
    CapabilityVerdict,
    ControlContext,
    DataSource,
    EditableFileDeclaration,
    EntityDeclaration,
    LifecycleHookDeclaration,
    MatchResult,
    ProfileOptionDeclaration,
    ProfileReadTransport,
    ProfileStatus,
    ResourceDeclaration,
    SurfaceDeclaration,
    ValidatorDeclaration,
    blocked,
    unsupported,
)


class GenericProfile:
    """Baseline profile for unknown games."""

    api_version = PROFILE_API_VERSION
    profile_id = "generic"
    name = "Generic Nitrado"
    supported_games: tuple[str, ...] = ()
    idle_shutdown_supported = False

    def matches(self, service: NitradoService, server: ParsedServer | None = None) -> MatchResult:
        """Generic always matches as fallback."""

        return MatchResult(True, confidence=0.1, reason="Generic fallback")

    async def enrich_status(
        self,
        client: ProfileReadTransport,
        service: NitradoService,
        server: ParsedServer,
        context: ControlContext,
    ) -> ProfileStatus:
        """Use generic Nitrado data as-is."""

        del client, service, context

        return ProfileStatus(
            player_count=server.player_count,
            player_max=server.player_max,
            player_names=server.player_names,
            player_source=server.player_source,
            query_valid=server.query_valid,
        )

    async def suggest_display_name(
        self,
        client: ProfileReadTransport,
        service: NitradoService,
        server: ParsedServer | None,
    ) -> str | None:
        """Use generic Nitrado gameserver metadata as the suggested name."""

        if server and server.server_name:
            return server.server_name
        return service.name

    async def can_start(self, context: ControlContext) -> CapabilityVerdict:
        """Allow safe Start only from fresh stopped status."""

        if context.force:
            return SUPPORTED
        if context.using_cached_data or not context.status_fresh:
            return blocked(
                "Status is cached or stale; start was not sent.", overridable=True, source=DataSource.NITRADO
            )
        status = context.server.raw_status if context.server else None
        if status != STOPPED_STATUS:
            return blocked(
                f"Server status is {status or 'unknown'}; start was not sent.",
                overridable=True,
                source=DataSource.NITRADO,
            )
        return SUPPORTED

    async def can_stop(self, context: ControlContext) -> CapabilityVerdict:
        """Allow safe Stop only from fresh running status."""

        if context.force:
            return SUPPORTED
        if context.using_cached_data or not context.status_fresh:
            return blocked("Status is cached or stale; stop was not sent.", overridable=True, source=DataSource.NITRADO)
        status = context.server.raw_status if context.server else None
        if status != RUNNING_STATUS:
            return blocked(
                f"Server status is {status or 'unknown'}; stop was not sent.",
                overridable=True,
                source=DataSource.NITRADO,
            )
        return SUPPORTED

    def idle_shutdown_capability(self, context: ControlContext) -> CapabilityVerdict:
        """Generic profile does not trust Nitrado query data enough for idle shutdown."""

        return unsupported("Selected game profile does not support automatic idle shutdown")

    def extra_entities(self) -> tuple[EntityDeclaration, ...]:
        """No extra entities for generic profile."""

        return ()

    def profile_options(self) -> tuple[ProfileOptionDeclaration, ...]:
        """No generic administrator options."""

        return ()

    def editable_files(self) -> tuple[EditableFileDeclaration, ...]:
        """No generic editable files."""

        return ()

    def resources(self) -> tuple[ResourceDeclaration, ...]:
        """No generic profile resources."""

        return ()

    def actions(self) -> tuple[ActionDeclaration, ...]:
        """No generic profile actions."""

        return ()

    def surfaces(self) -> tuple[SurfaceDeclaration, ...]:
        """No generic profile surfaces."""

        return ()

    def lifecycle_hooks(self) -> tuple[LifecycleHookDeclaration, ...]:
        """No generic lifecycle hooks."""

        return ()

    def validators(self) -> tuple[ValidatorDeclaration, ...]:
        """No generic validators."""

        return ()
