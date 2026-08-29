# Developing Game Profiles

Game profiles add game-specific behavior to the generic Nitrado integration.

The rule is simple: core hosts plumbing; profiles declare game meaning.

If adding support for a game requires editing `button.py`, `sensor.py`, `switch.py`, `number.py`, `select.py`, `coordinator.py`, or Home Assistant setup code, the profile contract is missing a generic hook. Add the generic hook deliberately, then use it from the profile.

## File Layout

Profiles live in:

```text
custom_components/nitrado_gameserver/plugins/
```

Built-in profiles:

- `generic.py`: fallback profile for unknown games
- `palworld.py`: Palworld-specific status, trusted player source, and settings file support

Export a profile class or a no-argument factory:

```python
from .my_game import MyGameProfile

PROFILE = MyGameProfile
```

The registry creates a fresh profile instance for each runtime, so profile objects should still be lightweight and should not depend on shared mutable module state.

### Separately Packaged Profiles

An external custom integration can register a profile without copying a module into this package. Import only from the versioned public API module:

Declare the core integration as a Home Assistant dependency in the companion's
`manifest.json`:

```json
{
  "domain": "nitrado_minecraft",
  "name": "Nitrado Minecraft",
  "dependencies": ["nitrado_gameserver"],
  "version": "2026.8.17"
}
```

Home Assistant uses that dependency to order setup. HACS does not install an
arbitrary companion repository automatically, so companion documentation must
tell users to install `nitrado_gameserver` first.

```python
from custom_components.nitrado_gameserver.plugins.api import (
    PROFILE_API_VERSION,
    async_register_profile,
)


async def async_setup_entry(hass, entry):
    unregister = await async_register_profile(hass, MyGameProfile)
    entry.async_on_unload(unregister)
    return True
```

`async_register_profile()` accepts a profile class or no-argument factory, rejects reserved/duplicate IDs, and validates one probe instance plus its complete declaration manifest off the Home Assistant event loop under a bounded timeout before committing anything. Core creates a fresh instance for each runtime and immediately reloads loaded Nitrado entries on registration or removal. Before changing the registry, core blocks new profile dispatch and cancels or drains old-profile work; dispatch also checks the live registration and whole-registry generations. Old code therefore cannot mutate a server after a companion install, update, or removal changes the registry.

Factories must be pure, lightweight, and deterministic. Do not perform network
I/O, wait on locks, start background work, or vary `profile_id` or declarations
between instances. Core validates every created instance and skips identity or
manifest drift. Registration probes run in one isolated bounded daemon-worker
slot so a timed-out factory cannot consume Home Assistant's shared executor;
additional registrations fail closed until that worker returns.

Register only from the companion integration's setup lifecycle and unregister
from its unload lifecycle. A profile callback must never add or remove profiles;
core rejects that reentrant operation before it can wait on the registry lock.
Public registry transitions are serialized, cancellation-safe, and rolled back
to the exact previously vetted factory if reload fails.

External profiles must declare `api_version = PROFILE_API_VERSION`. Core rejects incompatible versions before the profile can participate in matching or refresh. Do not import `plugins.base`, `plugins.registry`, or other private modules from a separately packaged profile; those modules may change without preserving external compatibility.

Profile API version 2 makes save-bundle archive recognition explicitly
profile-owned. Version 1 profiles must be updated before registration: declare
the permitted suffixes and the file or directory markers that identify an
editor ZIP root. Core no longer assumes Palworld filenames for other games.

The public API is a compatibility and ownership boundary, not a process-level
security sandbox. Companion custom integrations still execute Python inside the
Home Assistant process. Install only trusted companions. Core validates their
manifests, bounds hosted callbacks, enforces authorization, and revokes stale
dispatch, but it cannot make arbitrary malicious Python safe.

Registration/removal waits for already-started irreversible core transactions,
such as a verified editable-file recovery, to finish before the registry
generation changes. Cooperative profile work is canceled and closed. A
companion unload may therefore take as long as the bounded server transaction
it is safely draining; it must not assume unload is instantaneous.

Treat an API-version rejection as an installation compatibility error, not a
retryable server outage. Keep the companion entry unloaded, surface a clear
Home Assistant setup error naming the supported profile API version, and direct
the user to update either the core or companion. Do not catch the rejection and
quietly reach into private modules as a fallback.

`BaseGameProfile` supplies fail-closed defaults for every optional declaration
family. External adapters should subclass it and override only the matching,
status, controls, and extensions they actually support. Public API version 2
is a compatibility commitment: additive declarations receive safe defaults;
removing or changing an existing public callback requires a profile API version
bump and a documented migration path.

### Optional profile-owned cockpit

Profile API v2 remains valid without a cockpit. A trusted companion that wants
its own game UI can feature-detect the additive cockpit symbols and register one
owner-bound local JavaScript module. Cockpits use their own
`COCKPIT_API_VERSION`; an unsupported cockpit degrades only the UI to core's
generic fallback.

```python
from custom_components.nitrado_gameserver.plugins import api as profile_api

if hasattr(profile_api, "CockpitDeclaration"):
    unregister = await profile_api.async_register_profile(
        hass,
        MyGameProfile,
        owner_domain="nitrado_minecraft",
        cockpit_assets={
            "minecraft_cockpit": profile_api.LocalCockpitAsset(
                package_resource="frontend/minecraft-cockpit.js",
                sha256="<sha256 of the exact installed module>",
            )
        },
    )
else:
    # Old core: keep the backend unloaded and report the API mismatch.
    unregister = await profile_api.async_register_profile(hass, MyGameProfile)
```

The profile's optional `cockpit()` declaration names routes and the immutable
asset digest; `cockpit_snapshot(context)` returns a bounded, secret-free,
game-owned public payload. Core proves `owner_domain` through Home Assistant's
loader, requires the profile export and regular asset file to live inside that
integration package, rejects traversal/symlinks/digest mismatch, and serves the
exact bytes from an immutable same-origin URL. Cockpit code is trusted installed
code, not a browser sandbox. It receives frozen snapshots and narrow capability
closures—not `hass`, coordinators, provider clients, tokens, secret options, or
temporary Nitrado login URLs.

The public module also exports a small set of generic helpers so adapters do
not need private imports:

- `first_string(*values)` returns the first non-empty bounded string.
- `parse_int(value)` accepts common non-negative integer API shapes and returns
  `None` for malformed values.
- `blocked(reason, overridable=False)` and `unsupported(reason)` create typed
  capability verdicts without depending on core implementation modules.
- `validate_profile_option_value(declaration, value)` applies the same type,
  range, length, select, and secret-default rules used by core.
- `validate_action_payload(declaration, payload)` applies the declared typed
  action schema, defaults, unknown-field rejection, and bounded JSON rules.

Use these helpers when a profile itself needs pre-validation or normalization;
core still validates every hosted declaration and dispatched payload at the
trust boundary.

Bundled game profiles follow the same rule. Except for the Generic fallback,
game modules in this repository import their contract only from `plugins.api`.
An architecture test enforces that boundary. If a bundled profile needs a
private helper, first decide whether it is truly game-specific or whether the
versioned API is missing a generic capability.

Profile enrichment is isolated per service. If `enrich_status()` raises, core preserves fresh non-player Nitrado status, clears automation-authoritative player data, records the profile error for diagnostics, and continues refreshing later services. A profile should still catch expected game-native outages and return the most truthful degraded status it can; exceptions are for genuine handler failure, not normal offline state.

## Core Responsibilities

The generic Nitrado core owns:

- Nitrado account authentication
- service discovery, import, ignore, and remove
- Home Assistant config entry lifecycle
- Home Assistant devices and entity hosting
- Nitrado Start/Stop transport
- generic Start/Stop safety gates
- idle-shutdown timer mechanics
- profile declaration collection and validation
- profile context construction
- extension HTTP routing
- diagnostics, redaction, caching, and registry cleanup

## Profile Responsibilities

A profile owns:

- game matching
- game-native status enrichment
- display-name suggestions
- trusted player source decisions
- game-specific Start/Stop safety
- game-specific idle-shutdown support
- profile-declared entities
- editable files
- resources
- actions
- surfaces
- lifecycle hooks
- validators
- settings parsers, serializers, redactors, and validators

Do not expose internal trust contracts as user controls. For example, Palworld decides internally whether its player data is trustworthy for idle shutdown. Users do not need a switch for that.

## Minimal Profile

```python
from __future__ import annotations

from custom_components.nitrado_gameserver.plugins.api import (
    BaseGameProfile,
    MatchResult,
    NitradoService,
    ParsedServer,
)


class ExampleProfile(BaseGameProfile):
    profile_id = "example"
    name = "Example Game"
    supported_games = ("example",)

    def matches(self, service: NitradoService, server: ParsedServer | None = None) -> MatchResult:
        haystack = " ".join(
            value.lower()
            for value in (service.game, service.game_human, server.game_short if server else None)
            if isinstance(value, str) and value
        )
        if "example" in haystack:
            return MatchResult(True, confidence=0.95, reason="Example game metadata matched")
        return MatchResult(False)


PROFILE = ExampleProfile
```

## Matching

`matches()` receives generic Nitrado service metadata and, when available, parsed gameserver metadata.

Return:

- `matched=True`
- a confidence from `0.0` to `1.0`
- a short reason

Profiles are selected by confidence. The generic profile is the fallback.

Profile selection can run again after richer gameserver metadata arrives, so matching may use either service-level or server-level metadata.

## Status Enrichment

`enrich_status()` returns a patch-style `ProfileStatus`.

Only set fields the profile intentionally overrides:

```python
return ProfileStatus(
    player_count=0,
    player_names=(),
    player_source="example_rest",
    query_valid=True,
)
```

Unset fields do not erase generic Nitrado data. This keeps profiles from accidentally blanking useful status.

Use `extra` for diagnostic facts that do not deserve first-class core fields.

Core derives one provider-level player fact independently of profiles: a fresh,
non-cached, stable `stopped` status means Player Count `0`, Online Players
`None`, and source `server_status`. This does not set `query_valid` and is never
authority for Auto Shutdown. While `started`, player availability still
requires the profile's explicit trusted snapshot. Transitional, stale, and
cached states remain unknown and do not create a missing-player complaint.

## Start And Stop Safety

`can_start()` and `can_stop()` receive a `ControlContext` and return a `CapabilityVerdict`.

Use:

- `SUPPORTED` when the operation is safe
- `blocked("reason")` when the operation should not run
- `unsupported("reason")` when the capability does not apply

Hard safety blocks should use `overridable=False`. Operational uncertainty that an administrator may override can use `overridable=True`.

## Idle Shutdown

Set `idle_shutdown_supported = True` only when the profile has a trustworthy way to know that zero players are online.

Then implement:

```python
def idle_shutdown_capability(self, context: ControlContext) -> CapabilityVerdict:
    return SUPPORTED
```

The core owns timers, startup cooldown, final zero-player checks, and Stop dispatch. The profile owns whether player data is trustworthy for that game.

## Profile Entities

Use `EntityDeclaration` for simple Home Assistant surfaces.

Supported platforms:

- `sensor`
- `binary_sensor`
- `button`
- `switch`
- `number`
- `select`

Passive entity callbacks use `ProfileEntityContext` and must be synchronous,
pure, and non-blocking. Core evaluates them in bounded isolated workers during
profile refresh, validates and detaches their results into plain immutable
values, and lets Home Assistant entity properties read only that cache. They
must never perform network, filesystem, sleep, or expensive CPU work: a timed
out Python thread cannot be forcibly killed. Mutation callbacks use
`ProfileActionContext` and **must be async**. Core owns their timeout, tracks
them through unload/removal, and relies on cooperative cancellation to stop
them safely.

Example diagnostic sensor:

```python
EntityDeclaration(
    "sensor",
    "player_source",
    "Player Source",
    value_fn=lambda context: context.server.player_source if context.server else None,
    attributes={
        "entity_category": "diagnostic",
        "entity_registry_enabled_default": False,
    },
)
```

Entity keys are profile-scoped automatically. Do not manually bake service IDs into keys.

Supported entity attributes include:

- `entity_category`: `config` or `diagnostic`
- `entity_registry_enabled_default`: boolean
- `icon`: icon string
- `option_key`: per-service option key for core-hosted switches/numbers/selects
- number attributes: `native_min_value`, `native_max_value`, `native_step`, `native_unit_of_measurement`, `native_value`
- select attributes: `options`

Invalid entity declarations fail manifest validation instead of creating half-real entities.

## Save Bundles

Use `SaveBundleDeclaration` for stopped-server packages intended for an
external save editor. The profile owns the live save-tree locator and the
game-specific shape; core owns bounded FTPS reads, hostile-ZIP defenses,
portable manifests, review, and the release-gated mutation transaction.

```python
SaveBundleDeclaration(
    key="world",
    name="World Save",
    root_fn=resolve_active_world,
    allowed_suffixes=(".dat",),
    required_files=("level.dat",),
    excluded_paths=("backups",),
    editor_root_files=("level.dat",),
    editor_root_directories=("region",),
)
```

`editor_root_files` and `editor_root_directories` are relative, case-insensitive
markers used only to recognize manifestless editor ZIPs. At least one marker is
required. Directory markers deliberately support valid partial overlays such
as a player-only or region-only editor export. Paths must be canonical relative
POSIX paths: no absolute paths, backslashes, dot segments, control characters,
duplicates, case collisions, or required files beneath excluded paths.

Portable bundles written by core are bound to the exact `profile_id` and
bundle key on import. A package from another game profile is never accepted as
portable merely because its filenames happen to fit.

## Profile Options

Use `ProfileOptionDeclaration` for profile-specific administrator choices
that are not ordinary automatable entities, especially security consent or
transport policy.

```python
ProfileOptionDeclaration(
    key="allow_insecure_transport",
    name="Allow insecure game transport",
    option_type=ProfileOptionType.BOOLEAN,
    description="Permit this profile's documented plaintext game API.",
    default=False,
    standard_options=True,
    onboarding=True,
    confirmation_required=True,
    acknowledgement_revision=1,
    idle_shutdown_required=True,
    repair_if_unacknowledged=True,
)
```

Values are persisted per profile and service, exposed read-only through
profile contexts, and changed only through administrator-owned flows. Do not
use a normal switch entity for a choice that must be administrator-only.

Boolean options may participate in the standard administrator workflow:

- `standard_options=True` exposes the option in the integration's normal
  Options flow as well as the advanced panel.
- `onboarding=True` requires the decision while importing a matching service;
  onboarding options must also be standard options.
- `confirmation_required=True` requires an explicit confirmation when the
  choice changes or its acknowledgement is unresolved.
- `acknowledgement_revision=N` persists a versioned acknowledgement. Increment
  it only when the security meaning materially changes.
- `repair_if_unacknowledged=True` creates a fixable Home Assistant Repair issue
  for upgraded installations that have not resolved the current revision.
- `idle_shutdown_required=True` makes the option a prerequisite for automatic
  shutdown. Declining or disabling it turns Auto Shutdown off; enabling it
  never silently re-arms Auto Shutdown.

Core owns persistence, onboarding, Options, Repairs, capability gating, and the
shutdown side effects. The profile owns the security meaning and reads only the
resolved value from its context. This contract is appropriate for plaintext
game APIs, RCON transport consent, or another administrator policy that affects
trusted player reporting. It is not appropriate for internal player-source
trust, which remains profile logic rather than a user control.

Supported types are `BOOLEAN`, `NUMBER`, `TEXT`, `SELECT`, and `SECRET`.
Number options use `min`, `max`, and `step` attributes. Select options declare
an `options` list. Text and secret options may declare `max_length`; text may
also declare `multiline`. Secret defaults must be empty, stored secret values
are never returned by descriptors, and the panel exposes only whether a secret
has been configured.

## Editable Files

Use `EditableFileDeclaration` for game settings and text-like files.

Core provides:

- path resolution
- read
- parse
- redact
- preview
- diff
- validation
- backup
- write
- rollback
- restart/stopped/running prerequisites

The profile provides:

- `path_fn`
- parser
- serializer
- validator
- redactor
- state requirements

Example:

```python
EditableFileDeclaration(
    key="settings",
    name="server.properties",
    description="Server settings file.",
    path_fn=server_properties_path,
    parser=parse_properties,
    serializer=serialize_properties,
    redactor=redact_properties,
    requires_restart=True,
    requires_stopped=True,
    create_backup=True,
)
```

Redactors must return text. Parser, serializer, validator, path, and redactor failures are converted into blocked verdicts.

## Resources

Use `ResourceDeclaration` for read-only data.

Examples:

- rendered map image
- JSON player list
- text log
- binary export

```python
ResourceDeclaration(
    key="players",
    name="Players",
    content_type="application/json",
    content_family=ResourceContentFamily.JSON,
    fetch_fn=fetch_players,
    cache_seconds=30,
    access=ExtensionAccess.AUTHENTICATED,
)
```

Resource responses are authenticated and checked against their declared
content family. JSON/text responses are bounded; binary/image/stream responses
use a larger bounded transport budget. Stream resources cannot be cached or
embedded in a surface bundle. The extension HTTP layer does not apply
diagnostic redaction to normal resource payloads, so legitimate fields such as
`url` and `download_url` remain intact. Set
`access=ExtensionAccess.ADMIN` for resources containing administrative or
secret-bearing data. Never assume that “logged into Home Assistant” means
“allowed to administer a game server.”

Stream iteration remains coordinator-owned until close. Core generation-checks
each chunk, cancels and closes old streams before profile replacement or entry
reload, and does not hold the service's profile lock while waiting for the next
event. A live log/SSE panel therefore cannot freeze player refreshes, controls,
or Auto Shutdown evaluation.

## Actions

Use `ActionDeclaration` for profile-specific commands.

Examples:

- broadcast message
- run game-native command
- kick player
- refresh a cache
- verify a backup

```python
ActionDeclaration(
    key="broadcast",
    name="Broadcast Message",
    action_fn=broadcast_message,
    validators=("server_running",),
    requires_confirmation=True,
    access=ExtensionAccess.ADMIN,
    inputs=(
        ActionInputDeclaration(
            key="message",
            name="Message",
            input_type=ActionInputType.TEXT,
            required=True,
            attributes={"max_length": 500},
        ),
        ActionInputDeclaration(
            key="audience",
            name="Audience",
            input_type=ActionInputType.SELECT,
            default="all",
            attributes={"options": ["all", "admins"]},
        ),
    ),
)
```

Actions are administrator-only by default. Dangerous actions should also require confirmation. Handler exceptions are converted into structured extension failures. Action results must be finite JSON and fit within the core-owned response limit; return a compact operation result, not an unbounded game dump.

Declared actions are available through both the authenticated extension HTTP route and the `nitrado_gameserver.profile_action` Home Assistant action. This makes the same contract usable by custom pages, scripts, scenes, and automations. Optional typed inputs (`BOOLEAN`, `NUMBER`, `TEXT`, `SELECT`, and `JSON`) give generic frontends a real form contract; core rejects missing, unknown, malformed, non-finite, and oversized values before the handler runs. Schema-free actions remain available for specialized consumers and must validate their own bounded JSON payloads.

## Surfaces

Use `SurfaceDeclaration` to compose resources, actions, controls, editable files, and validators into a richer UI contract.

```python
SurfaceDeclaration(
    key="settings_editor",
    name="Settings Editor",
    editable_files=("settings",),
    actions=("validate_settings",),
    renderer_hint="settings-editor",
    access=ExtensionAccess.ADMIN,
)
```

Surface references are validated at manifest load time. Missing references make the manifest invalid instead of producing broken UI metadata. A surface resource bundle inherits administrator-only access when either the surface or one of its resources requires it.

The built-in administrator panel provides a generic renderer for the declared pieces: HA controls open their native more-info UI, resources and surface bundles can be fetched, typed action forms are rendered with confirmation, and editable files open in the verified text editor with rollback. Surface-referenced actions and editors are rendered inside the surface rather than merely listed as unrelated global operations. Core deliberately does not interpret game-specific `renderer_hint` values. A separately packaged frontend or Lovelace card can consume the same descriptors to provide a specialized map, backup browser, or other richer presentation without teaching generic core game semantics.

Surface resource responses share an aggregate byte limit; a surface cannot
multiply many individually valid resources into one unbounded response.

Every referenced control must be a profile-declared entity. The descriptor endpoint resolves its actual Home Assistant entity ID when the entity registry contains it, allowing generic and specialized panels to open native entity controls without guessing entity IDs.

## Extension Access

Every HTTP-facing declaration has an explicit `ExtensionAccess` policy:

- `AUTHENTICATED`: any authenticated Home Assistant user may call the endpoint.
- `ADMIN`: Home Assistant administrators only.

Defaults are intentionally conservative:

- actions: `ADMIN`
- editable files: `ADMIN`
- resources: `AUTHENTICATED`
- surfaces: `AUTHENTICATED`

Editable-file reads, previews, applies, and rollbacks enforce their declared access. Redacted responses omit parsed data; raw text and parsed values are returned only when an authorized caller explicitly requests raw output.

Core serializes applies and rollbacks for the same service/file, refreshes
server state before mutation, creates collision-resistant backups, verifies
backup and target content by read-back, and attempts a verified automatic
restore if the target write cannot be verified or the operation is cancelled
after mutation begins. Rollback re-reads the live target immediately before it
creates the pre-rollback backup, preventing stale preview data from
overwriting a newer file. Profiles should keep serializers deterministic:
semantically equivalent input should produce stable text, because verification
is exact.

## Lifecycle Hooks

Lifecycle hooks run around core operations.

Supported events:

- `BEFORE_START`
- `AFTER_START`
- `BEFORE_STOP`
- `AFTER_STOP`
- `BEFORE_FILE_WRITE`
- `AFTER_FILE_WRITE`
- `BEFORE_RESTORE`
- `AFTER_RESTORE`
- `SCHEDULED_VALIDATION`
- `STATUS_REFRESH`

Example:

```python
LifecycleHookDeclaration(
    key="validate_configuration_before_start",
    event=LifecycleEvent.BEFORE_START,
    hook_fn=validate_configuration_before_start,
    order=10,
    blocking=True,
)
```

Blocking hook failures are recorded in runtime diagnostics before they block the operation. Non-blocking after-hook failures are recorded without turning a successful external mutation into a false failure.

`STATUS_REFRESH` runs after each successfully parsed service refresh, even when profile enrichment degraded. `SCHEDULED_VALIDATION` runs during initial setup and on the account discovery cadence. Both are observational/non-blocking dispatches: failures are recorded in diagnostics and do not prevent other services from refreshing.

When a profile calls `ProfileReadTransport.fetch_external_json()`, it must provide the exact declared host set. The transport is bound to the active service and exposes no Start, Stop, or upload methods. Core permits only HTTP/HTTPS as appropriate, rejects embedded credentials, local-only hostnames, non-public literal IP addresses, and hosts outside that declaration. Nitrado-provided upload/download URLs are HTTPS-only and pass the same outbound safety policy.

Lifecycle hooks are appropriate for lightweight game/provider checks. A
feature that owns substantial persistent state, scheduled monitoring, backup
history, restore workflows, or a provider-independent lifecycle should be a
companion integration rather than a bundled profile. See
`ADR-0001-PROFILE-BOUNDARIES.md` and
`PALWORLD-SAVE-MONITOR-DESIGN.md` for the current decision.

## Validators

Validators are reusable safety checks.

`ValidatorTarget` says where the validator runs:

- `ACTION`
- `RESOURCE`
- `SURFACE`
- `EDITABLE_FILE`
- `LIFECYCLE_HOOK`

`ValidatorDomain` says what the validator is about:

- `SETTINGS`
- `SAVE`
- `RESTORE`
- `SERVER_STATE`
- `PLAYER_SOURCE`

Example:

```python
ValidatorDeclaration(
    key="server_running",
    name="Server Running",
    target=ValidatorTarget.ACTION,
    applies_to=(ValidatorTarget.ACTION,),
    domains=(ValidatorDomain.SERVER_STATE,),
    validate_fn=validate_server_running,
)
```

Validator exceptions fail closed as blocked verdicts.

Synchronous parsers, serializers, redactors, resource readers, and validators
are allowed only when they are side-effect-free. Core runs them off the Home
Assistant event loop with an execution budget, but a Python worker thread
cannot be forcibly killed after timeout. Never perform external mutation from
a synchronous callback. Actions, lifecycle hooks, and entity mutations must
be async so cancellation and entry unload can stop them cooperatively. An async
callback also must not perform blocking work before its first `await` (or between
awaits): Python cannot preempt arbitrary code already running on Home
Assistant's event loop. Use an async client for I/O, break CPU work into a safe
core-owned primitive, and propagate `CancelledError` after local cleanup.

## Context Objects

Profiles receive controlled context objects instead of raw coordinator internals.

`ProfileActionContext` includes:

- `client`
- `service`
- `server`
- `status_fresh`
- `using_cached_data`
- `force`
- `now`
- `payload`
- read-only, profile-namespaced persisted `options`
- profile-scoped `extra`
- helper methods such as `require_service_id()`, `list_files()`, and `download_text_file()`

`ProfileEntityContext` includes:

- `service`
- `server`
- `status_fresh`
- `using_cached_data`
- read-only profile-scoped `extra`
- read-only, profile-namespaced persisted `options`

Do not store service-specific state on the profile instance. Use profile-scoped context storage or declared resources/entities instead.

## Manifest Validation

The core validates profile manifests before hosting them.

Validation covers:

- profile IDs
- keys
- duplicate normalized entity keys
- supported entity platforms
- callback sync/async requirements
- HA-facing entity attributes
- content types
- cache settings
- lifecycle events
- validator targets and domains
- cross-references between surfaces, resources, actions, controls, editable files, hooks, and validators

Invalid manifests are reported through descriptors and diagnostics. A broken profile should not take down valid profiles.

Profile match results, capability verdicts, status patches, metadata, action
results, and resources are validated at the core boundary. Do not rely on
Python truthiness or implicit coercion: booleans must be actual booleans,
confidence must be finite and between zero and one, and JSON-bearing values
must be finite and bounded.

## Testing A Profile

Add focused tests under `tests/`.

Useful existing test files:

- `tests/test_profiles.py`
- `tests/test_extensions.py`
- `tests/test_entities.py`
- `tests/test_editable_files.py`
- `tests/test_views.py`

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -q
```

Run the real Home Assistant boundary suite for config-entry, registry, permission, and unload behavior:

```bash
.venv-ha/bin/pytest -q ha_tests
```

Run both lint and formatting checks:

```bash
.venv-ha/bin/ruff check custom_components tests ha_tests
.venv-ha/bin/ruff format --check custom_components tests ha_tests
```

Compile:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m py_compile $(rg --files custom_components tests ha_tests -g '*.py')
```

## Review Checklist

Before adding a profile:

- The profile matches only the intended game.
- Player count is trusted only when the game-native source is actually trustworthy.
- Idle shutdown is disabled unless trusted zero-player detection exists.
- Mutating actions have validators and confirmation where appropriate.
- Editable files have parser, serializer, validator, redactor, backup, and state requirements.
- Secrets are redacted from diagnostics and editable-file previews.
- Profile-specific behavior stays inside the profile.
- Generic core changes are limited to reusable plumbing.
