# ADR 0002: Reliable Nitrado Filesystem Transport

- Status: Accepted; source implementation complete, live provider mutation validation pending
- Date: 2026-08-16
- Decision owners: Nitrado Game Server maintainers

## Context

Nitrado exposes several overlapping ways to affect server files:

- the HTTP gameserver file-browser API;
- per-service FTP credentials and filesystem access;
- provider-native game backup inventory and restore APIs.

These mechanisms are not equivalent and none is, by itself, a trustworthy
filesystem abstraction.

Observed production failures include:

- file-browser downloads timing out or returning HTTP 502;
- upload-token requests returning HTTP 500;
- the second-stage upload endpoint rejecting the intuitive multipart protocol
  with HTTP 400 and requiring an SDK-specific raw-body request;
- directory deletion reporting success without recursively deleting a non-empty
  directory;
- provider-native backup/startup behavior rehydrating stale files over a
  directly restored tree;
- a tiny provider backup faithfully restoring the wrong blank world.

The current integration exposes bounded file listing and text download to game
profiles and uses the HTTP file API for verified editable-file transactions.
That is adequate for a narrow text-editor surface, but it must not be advertised
or treated internally as a complete provider filesystem.

## Decision

Nitrado core will own a service-bound, multi-transport provider filesystem.

Game profiles and companion integrations request filesystem capabilities from
core. They do not receive FTP credentials, raw FTP clients, private Nitrado
clients, or transport-selection controls.

Core chooses and composes the available Nitrado mechanisms according to the
operation:

| Provider mechanism | Appropriate uses |
| --- | --- |
| HTTP file-browser API | Metadata, bounded directory listing, bounded text reads, independent readback verification |
| FTP/FTPS transport | Recursive traversal, binary transfer, recursive deletion, bulk tree replacement, large-file operations |
| Native backup API | Provider-owned backup inventory and provider-native restore/recovery |

Provider backup operations remain a distinct service from ordinary filesystem
operations, even when a higher-level connector exposes both.

## Ownership boundaries

### Nitrado core owns

- retrieving and refreshing service FTP connection details;
- transport negotiation and security policy;
- canonical remote-path handling and root confinement;
- retry, timeout, connection, and passive-mode policy;
- size, file-count, depth, and execution-time budgets;
- per-service transaction serialization and overlapping-path locking;
- stopped/running state requirements;
- backup-before-mutation policy;
- upload, delete, rename, and tree-replacement mechanics;
- cancellation-resistant cleanup and rollback;
- readback, manifest, and post-operation verification;
- redacted diagnostics, audit records, and Repairs issues;
- a versioned provider-connector API for trusted companion integrations.

### Game profiles own

- locating game-specific files relative to the provider root;
- parsing, serializing, redacting, and validating game-specific content;
- declaring whether a settings edit requires the server stopped or restarted;
- interpreting file contents as game state;
- declaring the bounded files/resources/editors they need.

Profiles do not select HTTP versus FTP and do not receive mutation primitives
outside core-hosted declarations and actions.

### A future Palworld Save Monitor owns

- expected-world adoption and replacement;
- save-tree semantics and structural validation;
- backup-retention policy and incident history;
- deciding which verified snapshot should be restored;
- intentional-new-world workflows;
- optional, separately enabled Start interlocks;
- post-start semantic validation that the intended world actually loaded.

The Save Monitor consumes a provider-neutral connector. It does not contain a
private Nitrado FTP implementation.

## Public contract shape

### Existing game-profile API

Profile API version 1 keeps its existing service-bound read methods:

- `list_files(directory)`
- `download_text_file(path)`

Their implementation will route through the new filesystem service. Existing
profiles do not need to know which transport supplied the result.

The profile API may add narrowly bounded read capabilities additively, such as:

- structured `FileEntry` results rather than raw provider dictionaries;
- bounded binary resource reads when a declaration explicitly permits them;
- file metadata or hashes.

Profiles will not receive general recursive mutation methods.

### Core-hosted editable files

`EditableFileDeclaration` remains the profile-facing settings contract. Core
continues to own preview, confirmation, backup, write, readback verification,
rollback, authorization, and server-state checks.

The backing transport becomes an implementation detail of the provider
filesystem. A text edit may use the HTTP API when it is healthy and appropriate,
or a secure FTP path when required. Every successful result means the target was
read back and matched, not merely that Nitrado returned 2xx.

### Provider connector for companion integrations

A separate versioned provider API will expose a service-bound connector for
trusted companion integrations. It will not be part of the game-profile object
contract.

Implemented API version 2 shape:

```python
lease = await async_register_provider_consumer(
    hass,
    api_version=PROVIDER_CONNECTOR_API_VERSION,
    consumer_domain=DOMAIN,
    consumer_entry_id=entry.entry_id,
)
connector = await lease.async_get_connector(
    ProviderServiceRef(account_entry_id, service_id),
    scopes={ProviderScope.FILESYSTEM_READ},
    roots={"game-root"},
)

content = await connector.async_read_file(ProviderFileReadRequest("game-root/settings.ini"))
snapshot = await connector.async_snapshot_tree(ProviderTreeRequest("game-root"))
verified = await connector.async_verify_tree(ProviderTreeVerifyRequest("game-root", manifest))

plan = connector.plan_tree_replace(ProviderTreeReplaceRequest(...))
result = await connector.async_execute_tree_replace(plan, core_issued_approval)
progress = connector.mutation_progress(plan)

inventory = await connector.async_list_native_backups(limit=100)
plan = connector.plan_native_backup_restore(NativeBackupRestoreRequest(...))
result = await connector.async_execute_native_backup_restore(plan, core_issued_approval)

entry.async_on_unload(lease.async_close)
```

The API uses typed requests/results, explicit limits, explicit
preconditions, and transaction/audit identifiers. It must not expose raw
credentials or raw provider clients.

## Transport capability model

Core will model capabilities explicitly rather than assuming every transport can
perform every operation.

Minimum filesystem capabilities:

- list one directory;
- stat one path;
- read bounded text;
- read bounded bytes;
- write one file;
- create a directory;
- delete one file;
- recursively clear or delete a tree;
- rename a file or directory when supported;
- download a bounded tree;
- upload or replace a bounded tree;
- calculate or verify content hashes;
- report whether verification is strong, partial, or unavailable.

Minimum native-backup capabilities:

- list backups with identity, time, and size;
- request backup creation only when a provider endpoint is independently
  discovered and validated; Nitrado creation is not implemented today;
- request restore;
- observe restore transition/completion;
- reject suspicious restore candidates through caller-supplied policy;
- refresh and verify server/filesystem state after restore.

Capability absence is normal and must fail closed with a clear reason.

## Path model

Public consumers use normalized paths relative to the service filesystem root.
Provider-specific paths such as `/gameserver/ftproot/...` remain internal.

Core will:

- reject absolute consumer paths, `..`, NULs, empty components, and ambiguous
  normalization;
- enforce that every resolved path remains under the service root;
- refuse unknown entry types and symbolic-link traversal;
- apply platform-independent slash normalization;
- use canonical paths as lock identities;
- detect overlapping tree/file operations, not only exact-string collisions;
- prevent writes to undeclared or caller-unapproved roots.

## Credentials and transport security

FTP connection details are obtained on demand from the authenticated Nitrado
gameserver response. Passwords are never persisted in config entries, entity
state, logs, diagnostics, or connector results.

Core may hold credentials in memory for a short bounded lifetime and refresh
them after authentication failure or provider relocation.

Implementation must first determine whether Nitrado supports FTPS for the
service endpoint:

1. Prefer verified TLS when supported.
2. Reject invalid certificates and silent TLS downgrade.
3. If Nitrado offers only plaintext FTP, require an explicit administrator
   option before operations that need it.
4. The option wording must say that FTP credentials and file contents traverse
   the network without transport encryption.
5. Ordinary monitoring, Start/Stop, player reporting, and HTTP-backed settings
   reads must continue to work when insecure FTP is disabled.

The user must not be asked for an FTP password already available from Nitrado.

## Backend-selection policy

Transport selection is deterministic and capability-driven.

Initial policy:

- bounded listing and text reads: HTTP first, with bounded retry; optionally use
  FTP fallback when enabled and HTTP fails;
- single text-file mutation: use the transport that supports verified write and
  rollback; HTTP remains acceptable only with exact readback verification;
- binary or large files: FTP/FTPS;
- recursive delete, recursive upload, or exact tree replacement: FTP/FTPS;
- provider-native backup restore: native backup API;
- verification: prefer an independent transport when possible, but never weaken
  the requirement for exact expected content;
- transport success without verification is not operation success.

Core records which transport performed and verified an operation without
recording credentials or sensitive content.

## Transaction semantics

### Reads

- Apply explicit byte, file-count, directory-depth, and time limits.
- Retry only provider/transient failures with bounded backoff.
- Do not retry deterministic permission, path, validation, or policy failures.
- Distinguish provider unavailability from content validation failure.

### Single-file writes

1. Require current server state when the declaration demands it.
2. Acquire the service/path mutation lock.
3. Read and hash current content.
4. Create and verify a recoverable backup when configured.
5. Recheck that the target did not change during backup creation.
6. Upload proposed content.
7. Download and hash the target through the strongest available verification
   path.
8. On failure or cancellation, restore and verify the previous content.
9. Return success only after verification.

### Tree replacement

Tree replacement is not presented as atomic unless the provider proves an
atomic rename/swap primitive.

Core serializes its own readers and mutations, but cannot lock out Nitrado's
web file manager, another FTP client, another Home Assistant instance, or
provider background work.  Reliable replacement therefore has an explicit
single-writer operational assumption.  An administrator must not run an
independent writer against the same service during a transaction.

Required flow:

1. Require the server stopped and stably stopped.
2. Validate the local source and build an exact manifest of relative paths,
   sizes, and hashes.
3. Snapshot the current remote target to recoverable storage and verify that
   snapshot.
4. Acquire the exclusive per-service filesystem mutation lock.
5. Reconfirm server state and profile/connector generation.
6. Stage and verify content when provider capabilities permit.
7. Clear/replace the target using FTP/FTPS child-first operations.
8. Verify the complete remote manifest, including absence of unexpected files.
9. Preserve recovery material until the caller explicitly retires it.
10. If the server is later started, let the higher-level product perform a
    post-start semantic check before declaring the recovery complete.

Cancellation during an irreversible transaction triggers protected cleanup and
rollback. Integration unload or profile-registry change drains such a bounded
transaction rather than abandoning it halfway.

## Concurrency and lifecycle

- One mutating filesystem transaction may run per service.
- Reads may run concurrently only when they cannot observe a partially replaced
  tree; otherwise they wait behind the mutation lock.
- Locks use canonical paths and understand ancestor/descendant overlap.
- Start/Stop and filesystem transactions share service-operation coordination
  when server state is a precondition.
- Every operation is bound to config-entry, service, connector, profile-registry,
  and integration lifecycle generations as applicable.
- Removal, unload, reauthentication, or provider relocation revokes new work and
  safely drains or cancels existing work according to transaction phase.
- Blocking FTP libraries may not execute on Home Assistant's event loop.
  Implementation must use a bounded dedicated worker or a reviewed asynchronous
  FTP library, with explicit cancellation/close behavior.

## User experience

Normal users should not see FTP credentials, transport selectors, or generic
filesystem internals.

Account-level advanced options may expose:

- **Advanced file operations**: enabled automatically when secure transport is
  available, otherwise disabled until consent;
- **Allow plaintext Nitrado FTP**: administrator-only, default off, shown only
  when required and FTPS is unavailable;
- diagnostic capability state such as `HTTP file API available`, `Secure FTP
  available`, or `Advanced file operations unavailable`.

Game-specific settings remain in game-specific editors. A future Save Monitor
owns world-adoption, backup, and restore UI.

Failures should tell the user what did and did not happen:

- provider could not be reached;
- operation was refused before mutation;
- mutation failed and rollback was verified;
- mutation failed and rollback also failed;
- provider reported success but verification failed;
- operation succeeded and was independently verified.

## Diagnostics and audit

Diagnostics may include:

- supported transport capabilities;
- selected transport names;
- sanitized endpoints/ports;
- last operation type, phase, timestamp, duration, and outcome;
- retry counts and provider status classes;
- verification strength;
- whether recovery material exists.

Diagnostics must exclude:

- FTP username/password;
- API upload tokens and signed URLs;
- raw file contents;
- secret-bearing parsed settings;
- local backup contents;
- unrestricted remote paths when they contain user data.

Mutations receive an audit/transaction ID suitable for logs and incident
correlation.

## Implementation plan

### Phase 0: contract and provider spike

- Accept this ADR after review.
- Read-only probe the actual Nitrado gameserver credential response without
  logging secrets.
- Determine FTP versus FTPS support, passive-mode behavior, MLSD support,
  root/path behavior, file-size behavior, timeouts, and credential rotation.
- Confirm which API operations are eventually consistent or unreliable.
- Record a capability matrix and decide the transport-security default.

### Phase 1: internal filesystem abstraction

- Introduce typed `FileEntry`, `FileCapabilities`, operation requests/results,
  verification results, and normalized remote paths.
- Move current HTTP list/download/upload calls behind
  `NitradoHttpFileTransport`.
- Introduce `NitradoFilesystemService` as the only core consumer-facing entry
  point.
- Route existing profile reads and editable-file operations through it without
  changing their external behavior.
- Add compatibility tests proving current Palworld settings behavior is
  unchanged.

### Phase 2: FTP/FTPS backend

- Add bounded connection lifecycle and credential refresh.
- Implement list/stat/read/write/mkdir/delete/rename/tree traversal.
- Constrain passive data connections and validate provider endpoints.
- Keep all blocking work off the HA event loop.
- Add hash/readback verification and deterministic error mapping.
- Do not expose credentials or raw transport objects.

### Phase 3: reliable transaction coordinator

- Add per-service and overlapping-path locks.
- Generalize current editable-file backup/write/rollback machinery onto the
  filesystem service.
- Implement exact-manifest snapshot and tree verification.
- Implement recursive replacement with recoverable pre-operation snapshot.
- Coordinate server-state checks with Start/Stop locks.
- Harden cancellation, unload, reauth, and relocation behavior.

### Phase 4: provider backup service

- Model native backup metadata and transitions separately from files.
- Require exact backup identity and surface backup size/time before restore.
- Refuse obviously suspicious candidates only through explicit caller policy;
  provider recency alone is not trust.
- Refresh provider/filesystem state after restore.
- Do not claim semantic game recovery; return facts to the caller.

### Phase 5: versioned companion connector

- Publish a minimal service-bound connector API separate from the game-profile
  API.
- Add registration/lifecycle ownership for trusted companion integrations.
- Add explicit limits, confirmation/precondition policy, progress, audit IDs,
  and structured recovery results.
- Prove a separately packaged fake Save Monitor can install, acquire a
  connector, perform bounded read-only operations, unload, and lose access
  cleanly.
- Add mutation tests only after the connector's authority model is reviewed.

### Phase 6: user and administrator surfaces

- Add advanced transport/security options with safe defaults.
- Add capability/health diagnostics without exposing credentials.
- Add Repairs issues for requested-but-unavailable advanced transport,
  persistent credential failure, or unresolved failed rollback.
- Keep ordinary Nitrado and Palworld setup free of unnecessary FTP controls.
- Provide a generic pending-mutation review surface that displays the exact
  consumer, service, action, audit ID, payload digest, expiry, and progress;
  approve/reject decisions remain exact, one-shot, and core-owned.
- Enforce bounded leases, connectors, concurrent operations, open snapshot
  handles/bytes, capability probes, and retry backoff so companions cannot
  exhaust Home Assistant or hammer Nitrado.

### Phase 7: adversarial and live validation

- Run architecture, HA/user, extension-author, and security/reliability reviews.
- Test against a fake FTP server with injected disconnects, timeouts, stale
  credentials, partial writes, wrong hashes, path traversal, symlinks, and
  passive-mode failures.
- Reproduce HTTP 400/500/502, false delete success, and eventual-consistency
  behavior in transport fakes.
- Test cancellation, HA reload/unload, reauthentication, profile replacement,
  service removal, and concurrent operations.
- On live Nitrado, begin read-only; then use a harmless temporary directory/file
  while the game server is stopped.
- Test real Home Assistant process death and journal/Repairs recovery only
  against that disposable tree.
- Verify disposable content across a controlled server Start/Stop lifecycle to
  detect provider startup rehydration.
- Back up live state before any destructive test.
- Never test recursive delete or restore against the active save tree until all
  fake and temporary-tree gates pass and an administrator explicitly approves it.
- Because native restore is service-wide, validate actual restore only on a
  disposable Nitrado service. A temporary directory on a production service
  cannot make a provider-native restore harmless.

## Test matrix

Required automated coverage includes:

- path normalization and root confinement;
- capability selection and fallback;
- credentials absent, rotated, rejected, or redacted;
- FTP/FTPS handshake and downgrade behavior;
- list/read/write limits;
- HTTP transient and deterministic failures;
- FTP disconnect before, during, and after transfer;
- partial upload and hash mismatch;
- false provider success;
- target changed between preview, backup, and write;
- exact tree verification including unexpected remote files;
- rollback success and rollback failure;
- repeated cancellation during recovery;
- overlapping path/tree lock conflicts;
- Start/Stop races;
- HA unload/reload/reauth/service removal;
- profile and connector generation replacement;
- multi-account isolation;
- diagnostics and secret redaction;
- mobile/desktop error and consent UI;
- separately packaged companion lifecycle.

## Release gates

Before claiming reliable Nitrado filesystem support:

1. Current editable-file behavior runs through the new abstraction and retains
   verified backup/rollback semantics.
2. No profile imports or handles FTP credentials or raw provider clients.
3. Plaintext FTP cannot be used without explicit administrator consent when
   secure FTP is unavailable.
4. All file paths are root-confined and canonical.
5. Blocking FTP work never runs on the HA event loop.
6. Recursive mutations use FTP/FTPS and exact manifest verification, not the
   unreliable API directory-delete result.
7. Success always means verified content, never merely provider HTTP/FTP
   acknowledgement.
8. Native backup restore is modeled separately and exposes backup size/identity.
9. Failed/canceled mutations either verify rollback or surface a durable critical
   repair condition.
10. A real separately packaged connector fixture passes install, operation,
    reload, removal, and stale-access tests.
11. Independent adversarial reviewers find no meaningful architecture,
    authorization, concurrency, or recovery blocker.
12. Live validation uses only a temporary test tree until an administrator
    separately approves any active-save operation.

## Implementation status

Phases 0 through 6 are implemented in source. Phase 7's automated and
adversarial work is complete; its live-provider mutation checkpoint remains
deliberately pending.

Implemented release evidence includes:

- HTTP plus FTPS/FTP service-bound filesystem transports;
- exact path confinement, bounded reads, snapshots, manifests, verified writes,
  tree replacement, rollback, and durable recovery journals;
- reader isolation from partial tree mutations;
- provider-native backup inventory and restore as a separate service;
- a versioned, grant-, scope-, root-, generation-, and approval-bound companion
  connector;
- live grant revocation that drains started work and cannot race connector
  acquisition or first mutation;
- truthful mutation progress (`planned`, `authorizing`, `running`, `completed`,
  `refused`, `failed`) and outcome-unknown errors after the provider mutation
  boundary;
- administrator consent, capability facts, grant management, diagnostics, and
  Repairs surfaces;
- a generic core mutation-review broker and panel with exact approve/reject,
  companion waiting, and recent progress;
- bounded leases, connectors, operations, content handles, open snapshot bytes,
  capability-probe caching, and failure backoff;
- real Home Assistant lifecycle coverage using a separately packaged companion
  fixture;
- repeated read-only adversarial review with no remaining source release blocker.

The remaining live filesystem checkpoint must use a harmless temporary tree on
a stopped server. It must prove create/write/read/rename/delete, credential
refresh, readback, disconnect handling, crash recovery, and persistence across
a controlled Start/Stop lifecycle without touching the active save tree.

Provider-native restore is a separate service-wide checkpoint. It requires a
disposable Nitrado service; inventory and `restore_possible` observation on a
production service do not validate restore. General reliable-filesystem claims
remain withheld until the temporary-tree checkpoint passes, and native-restore
claims remain withheld until the disposable-service checkpoint passes.
