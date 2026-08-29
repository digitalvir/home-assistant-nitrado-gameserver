# ADR 0001: Core, Game Adapter, and Companion Integration Boundaries

- Status: Accepted
- Date: 2026-08-15
- Decision owners: Nitrado Game Server maintainers

## Context

Nitrado Game Server is intended to make Nitrado-hosted game servers behave as
first-class Home Assistant devices. It must remain useful for unknown games,
allow first-party game support without turning generic core into a game-specific
codebase, and permit independently maintained adapters and richer companion
products.

Code separation and distribution separation are different decisions. A bundled
adapter may still be architecturally isolated, while a heavyweight optional
feature may deserve its own integration and release lifecycle.

## Decision

The project uses a hybrid, one-install-first architecture.

### Nitrado core owns

- Nitrado authentication, account identity, discovery, and service transport.
- Home Assistant config entries, devices, entities, actions, and lifecycle.
- Generic Start/Stop guards and opt-in idle-shutdown machinery.
- File transport, authorization, locking, verification, and diagnostics.
- Versioned profile registration, execution budgets, validation, and hosting.
- The generic extension panel and portable resource/action/editor plumbing.

Core must not interpret game-specific player truth, save health, configuration
formats, maps, commands, or world identity.

### Bundled game adapters own

- Game matching and ordinary game-specific status semantics.
- Trust decisions for player reporting and automation eligibility.
- Lightweight game-specific settings and entity declarations.
- Provider-aware behavior necessary for truthful everyday operation.

Bundled adapters must consume only the versioned public profile API exposed by
`custom_components.nitrado_gameserver.plugins.api`. They receive no private
imports or privileged implementation access. The Generic fallback is part of
core and is the only profile exempt from this rule.

### External game adapters own

Independently maintained or experimental games may register through the same
versioned public API. External packaging is an option, not a requirement for
ordinary first-party support.

### Companion integrations own

A feature belongs in a companion integration when it owns substantial durable
state, has an independent scheduler or UI, remains useful beyond Nitrado, adds
heavy dependencies/workflows, or requires a distinct release lifecycle.

Full Palworld save monitoring belongs in a provider-independent companion
integration. It includes world adoption, expected identity, fingerprints,
corruption detection, backup history, restore candidates, reset/new-world
workflows, alerts, and optional Start blocking.

The bundled Palworld adapter does not block Start based on save-tree state.
Creating or wiping a world must not require a Nitrado-integration bypass.

## Palworld baseline behavior

The bundled Palworld adapter provides:

- Palworld service matching.
- Palworld REST endpoint discovery and explicit plaintext-transport consent.
- Trustworthy REST player count/name reporting.
- Fail-closed Auto Shutdown eligibility when trusted player data is unavailable.
- Palworld transition safety and settings-file declarations.
- A profile-declared active-world locator for explicit, stopped-server save
  bundle export/import hosted entirely by core's generic archive and verified
  filesystem transaction machinery.

Manual save portability is not save monitoring. The adapter does not adopt an
expected world, retain semantic restore history, schedule validation, diagnose
corruption, or block Start. It only identifies the active world tree for one
administrator-requested snapshot or reviewed overlay transaction.

World/save diagnostics never determine player truth or Auto Shutdown authority.
Nitrado player data remains untrusted for Palworld automation unless a future
adapter revision defines and tests an exact reliable contract.

## Public API rule

All non-generic bundled profiles and all external profiles must:

- Import profile contracts only from `plugins.api`.
- Declare a compatible `PROFILE_API_VERSION`.
- Register through `async_register_profile()` when packaged externally so
  active Nitrado entries reload immediately on registration and removal.
- Avoid `hass.data`, coordinator internals, private registries, and private
  Nitrado helpers.

If a bundled adapter cannot be implemented through the public API, the public
API is incomplete. Core must expose the smallest portable capability required;
the adapter may not reach around the boundary.

## Consequences

### Benefits

- One installation provides useful first-party Palworld support.
- Core remains game-agnostic and independently testable.
- Bundled adapters continuously prove the external contract is sufficient.
- Heavy save-monitor behavior does not surprise ordinary users.
- Future extraction is a distribution decision rather than an architectural
  rewrite.

### Costs

- The public profile API becomes a compatibility commitment.
- First-party adapters require architectural boundary tests.
- Companion integrations need explicit compatibility and optional connector
  contracts.
- Some advanced features require a second installation rather than silently
  expanding the Nitrado integration.

## Release gates

Before publication:

1. Palworld imports only the public profile API.
2. Architectural tests reject private imports from bundled game adapters.
3. External registration/unregistration works in the real HA harness.
4. Palworld has no save-based Start-blocking lifecycle hook.
5. Player trust is independent of world/save diagnostics.
6. User, HA lifecycle, power-developer, and security reviews find no meaningful
   boundary regression.
7. Profiles receive only a service-bound, read-only provider transport; Start,
   Stop, and file mutation remain core-owned operations.
8. Any profile-registry addition, replacement, or removal immediately revokes
   stale dispatch and shutdown intent until loaded runtimes have reselected a
   profile from the new registry generation. Registry changes first quiesce
   loaded coordinators so old actions, resources, lifecycle hooks, file work,
   and profile reads cannot cross the generation boundary.
9. Public registry transitions are serialized, cancellation-safe, reject
   callback reentry before lock acquisition, and restore the exact vetted
   registration if Home Assistant reload fails.
10. Stream resources remain coordinator-owned and generation-bound through
    close without holding the ordinary profile-operation lock while waiting
    for the next event.
