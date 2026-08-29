# Privacy and data handling

## Telemetry

This integration has no analytics, advertising, crash-reporting, or maintainer
telemetry. It communicates with Home Assistant, Nitrado, and—only when the
administrator enables it—the managed game's own profile endpoint.

## Data sent outside Home Assistant

- The Nitrado token authenticates requests to Nitrado.
- Service status/control requests include the target service identity.
- Optional Palworld REST requests send Basic-auth credentials to the configured
  server endpoint over plaintext HTTP only after explicit administrator consent.
- This release does not upload settings or save restores because all provider
  mutation gates remain disabled.

## Data stored locally

- Home Assistant config entries store the Nitrado token and integration options.
- Entity state/history may contain server names, addresses, status, player
  counts, and player names according to the recorder configuration.
- Diagnostics are redacted but can still contain service behavior and metadata.
- Operation, filesystem, editable-file, native-backup, grant, recovery, and
  prepared-transfer records protect authorization and crash recovery.
- Browser/session leases and prepared transfers are bounded and expire.

Home Assistant administrators can access integration configuration,
diagnostics, controls, and the Nitrado Servers panel. Other authenticated users
may see entity states according to Home Assistant permissions.

## Retention and removal

Ordinary unload/reload retains configuration and recovery state. Removing the
config entry purges integration-owned local journals, grants, prepared
transfers, recovery metadata/blobs, Repairs, devices, and entities for that
entry. Home Assistant recorder/history and backups follow Home Assistant's own
retention and are not rewritten by this integration.

Verified remote recovery copies created on the managed server are never deleted
implicitly. Removing the integration cannot safely prove that a remote backup
is disposable. Review and remove those through Nitrado only after confirming
they are no longer needed.

Remove every Nitrado Game Server config entry before uninstalling the code from
HACS. HACS code removal cannot execute integration cleanup after the code is
gone. If code was removed first, reinstall the same or newer version, remove the
entries, then uninstall again.

See [LIFECYCLE.md](LIFECYCLE.md) for the exact lifecycle contract.
