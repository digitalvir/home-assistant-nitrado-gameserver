# Developing Provider Connectors

Provider Connector API version 2 lets a separately packaged Home Assistant
integration use Nitrado-owned filesystem and native-backup capabilities without
receiving Nitrado credentials, FTP clients, or coordinator internals.

Use this API for heavyweight companion products such as a future Palworld Save
Monitor. Use the Profile API for lightweight game matching, status enrichment,
entities, options, actions, and settings declarations. The two contracts are
deliberately separate.

## Ownership model

Nitrado Game Server core owns:

- provider credentials and HTTP/FTPS/FTP selection;
- path confinement, size/depth/count limits, locks, and timeouts;
- stopped-state checks, read isolation, verification, rollback, and journals;
- persistent administrator grants and one-shot mutation approvals;
- connector revocation, generation changes, unload draining, and audit IDs.

The companion owns:

- game/save semantics and durable product state;
- which exact service, roots, and scopes it requests;
- which snapshot or native backup the administrator should review;
- post-start semantic validation after a provider operation.

The API is a compatibility and authority boundary, not a Python sandbox. A
third-party custom integration still executes in Home Assistant's process.

## Public imports

Import connector types only from:

```python
from custom_components.nitrado_gameserver.provider_api import (
    PROVIDER_CONNECTOR_API_VERSION,
    ProviderFileReadRequest,
    ProviderScope,
    ProviderServiceRef,
    ProviderTreeRequest,
    async_register_provider_consumer,
)
```

Do not import `provider_runtime`, `filesystem`, `backups`, a coordinator, or a
private Nitrado client.

## Config-entry lifecycle

```python
async def async_setup_entry(hass, entry):
    lease = await async_register_provider_consumer(
        hass,
        api_version=PROVIDER_CONNECTOR_API_VERSION,
        consumer_domain=entry.domain,
        consumer_entry_id=entry.entry_id,
    )
    entry.async_on_unload(lease.async_close)

    connector = await lease.async_get_connector(
        ProviderServiceRef(entry.data["account_entry_id"], entry.data["service_id"]),
        scopes={ProviderScope.FILESYSTEM_READ},
        roots={entry.data["provider_root"]},
    )
    hass.data.setdefault(entry.domain, {})[entry.entry_id] = {
        "lease": lease,
        "connector": connector,
    }
    return True
```

The Nitrado administrator must create an exact persistent grant before
`async_get_connector()` succeeds. Core also proves required transport/API
capabilities during acquisition. A grant is bound to:

- companion domain and config-entry ID;
- Nitrado account-entry ID and numeric service ID;
- exact scopes;
- exact filesystem roots, when filesystem scopes are present.

Closing the lease, unloading either integration, removing the Nitrado service,
or revoking the grant invalidates every derived connector, stream, content
handle, and pending plan. Started mutations drain; work that has not crossed the
provider mutation boundary is refused.

## Scopes

- `filesystem:read`: bounded single-file reads.
- `filesystem:snapshot`: bounded exact tree snapshots and verification.
- `filesystem:replace`: planned exact tree replacement.
- `native_backup:read`: bounded provider-native backup inventory.
- `native_backup:restore`: planned exact provider-native restore.

Filesystem scopes require at least one granted root. Native-backup-only grants
must not request roots. Connector acquisition proves a working FTP/FTPS path for
filesystem scopes. Native scopes perform a bounded live native-backup API probe.

## Read operations

All requests and results are typed and bounded:

```python
content = await connector.async_read_file(ProviderFileReadRequest("game/Saved/settings.ini", max_bytes=64 * 1024))

snapshot = await connector.async_snapshot_tree(
    ProviderTreeRequest("game/Saved/SaveGames", max_files=5000, max_bytes=512 * 1024 * 1024)
)
try:
    async for chunk in snapshot.content.async_chunks():
        consume(chunk)
finally:
    await snapshot.content.async_close()
```

Authoritative reads wait until tree mutation is complete; callers never observe
the child-first deletion/upload phase. Content handles are revoked on connector
or service invalidation.

## Mutations

Companions cannot directly mutate provider state. They create an immutable,
expiring plan from an exact expected/proposed state:

```python
plan = connector.plan_tree_replace(request)
```

The companion creates the plan and may then wait on core's generic
administrator review surface.  The panel displays the exact consumer, service,
action, audit ID, digest, and expiry.  Approval or rejection is delivered only
to that plan:

```python
approval = await connector.async_wait_for_approval(plan)
result = await connector.async_execute_tree_replace(plan, approval)
progress = connector.mutation_progress(plan)
```

A companion-specific administrator page may use the protected core approval
HTTP route and pass the returned typed facts directly to its own execution
flow.  Do not display, copy, persist, or ask a user to manually transfer an
approval token.

The same pattern applies to native backup restore. Never construct or persist
an approval token in a companion integration.

Progress states are:

- `planned`
- `authorizing`
- `running`
- `completed`
- `refused` — nothing crossed the provider mutation boundary
- `failed` — the provider may have changed; inspect the audit ID and Repairs

An exception with `provider_outcome_unknown = True` means the caller must not
retry blindly. Core retains recovery/audit facts; the companion should create an
incident and await administrator review.

## Reliability rules

- Treat provider-native restore acknowledgement as a provider fact, not proof
  that a game loaded the intended save.
- Treat native backup creation as unavailable unless a future connector API
  explicitly advertises a separately validated creation capability.
- Never infer that the newest or largest native backup is good.
- Compare exact manifests before tree replacement.
- Do not retry a mutation after cancellation or outcome-unknown failure without
  reconciling remote state.
- Keep semantic world adoption, corruption rules, retention, and post-start
  validation in the companion.
- Bind every cleanup path with `entry.async_on_unload()`.
- Do not assume tree replacement is atomic.  Core serializes its own access,
  but the administrator must prevent another HA instance, FTP client, or
  Nitrado file-manager session from writing the same service concurrently.
- Expect quota and backoff failures.  Close snapshot content promptly and reuse
  connectors instead of acquiring them repeatedly.

## Testing requirement

A connector package should test:

- install, acquisition, unload, reload, and Nitrado service removal;
- missing/mismatched/revoked grants;
- capability-probe failure;
- root escape and limit rejection;
- content-handle revocation;
- plan expiry and approval mismatch;
- revocation while acquisition or mutation is waiting;
- `refused` versus `failed`/outcome-unknown behavior;
- recovery after Home Assistant cancellation and reload.

The repository's `ha_tests/fixtures/nitrado_companion` integration is the
minimal independently packaged lifecycle example.
