# Palworld Save Monitor — Product Boundary

Status: design boundary; not implemented in this repository  
Date: 2026-08-15

## Purpose

Palworld save protection is a separate operational concern from managing a
Nitrado game server. A future Palworld Save Monitor should therefore be its own
Home Assistant integration, designed around Palworld saves rather than around
one hosting provider.

Its job would be to detect save damage, preserve evidence and recovery choices,
and help an administrator deliberately adopt, reset, back up, or restore a
world. It must not silently convert an observation into a Start/Stop policy.

## Product boundary

`nitrado_gameserver` owns:

- Nitrado account authentication and service discovery
- generic server devices, status, and Start/Stop transport
- provider file transport and safe extension hosting
- generic automation and permission boundaries
- a thin Palworld adapter for service matching, settings discovery, truthful
  player reporting, and Palworld-specific capability decisions

A future Palworld Save Monitor owns:

- expected-world adoption and replacement
- save-tree fingerprints and structural health
- scheduled validation and incident history
- backup inventory, retention, and verified restore candidates
- corruption or wrong-world alerts
- explicit new-world/reset workflows
- optional, separately enabled Start interlocks
- provider-independent save semantics

## Default behavior

The monitor is observational by default.

- No world is protected until an administrator explicitly selects **Adopt
  Current World** or provides an expected identity.
- A mismatch creates a diagnostic incident; it does not block Start by default.
- Start blocking is an independent opt-in policy with a clear manual override.
- Resetting or replacing a server uses an explicit **Adopt New World** workflow.
- Removing the monitor never removes or rewrites server saves.

This allows a user to wipe a server and intentionally start a new world without
fighting hidden policy. The monitor should explain what changed and ask which
world becomes authoritative.

## State model

Keep these facts independent:

- `observed_world_identity`
- `expected_world_identity`
- `save_structure_health`
- `last_verified_backup`
- `incident_state`
- `start_interlock_enabled`

Player count, player-source trust, and automatic idle-shutdown authority are not
save-monitor state. A world mismatch must never invalidate live player data or
turn an unknown player count into zero.

## Provider connectors

The save monitor should define a provider-neutral connector contract for:

- listing and reading save files
- creating and verifying backups
- writing/restoring content when explicitly requested
- reporting whether the server is stopped
- optionally requesting a guarded Start/Stop

A Nitrado connector may use the documented public API of
`nitrado_gameserver`, ideally as an optional `after_dependency`. It must not
import private coordinators or reach into `hass.data`. Other connectors could
support local files, SMB/SFTP, Docker hosts, or another game-server provider.

That provider-neutral connector API is a design prerequisite, not an API
implemented by this Nitrado release. Building the Save Monitor requires a
separate contract/review pass; this document deliberately does not promise
that today's read-only game-profile transport is a save/restore connector.

## Safety and permissions

- Adoption, restore, deletion, retention changes, and Start-interlock changes
  are administrator-only.
- Restore requires an explicit target and confirmation.
- Backup and restore operations are serialized and read-back verified.
- Destructive replacement keeps a recoverable backup whenever possible.
- Secrets and raw save content are excluded from diagnostics.
- Alerts follow incident semantics: one clear incident, silence while ongoing,
  and one recovery confirmation.

## Integration relationship

The companion may consume a stable public connector API, HA actions, events, or
entities from a provider integration. It is not required for ordinary Nitrado
or Palworld operation, and `nitrado_gameserver` must remain functional when the
companion is absent, disabled, outdated, or removed.

## Non-goals for the Nitrado release

The initial `nitrado_gameserver` release will not:

- remember an expected Palworld world
- block Start because a save tree looks wrong
- schedule save-corruption monitoring
- manage backup retention or restore history
- claim that save health controls player-data trust

An administrator-requested portable save-bundle download or reviewed overlay
restore is compatible with these non-goals. It owns no expected-world state,
health policy, retention policy, scheduled monitoring, or Start interlock; the
provider integration merely hosts a bounded manual transaction.

Those features require their own product, consent model, state ownership, and
test matrix. Tiny horns, separate clipboard.
