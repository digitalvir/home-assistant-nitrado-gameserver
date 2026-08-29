# Support and recovery

This is an unofficial volunteer integration, not Nitrado or Home Assistant
support. The newest release and its declared minimum Home Assistant version are
the supported combination.

## Before filing an issue

1. Confirm Home Assistant and the integration meet [COMPATIBILITY.md](COMPATIBILITY.md).
2. Check **Settings -> System -> Repairs**.
3. Reload the config entry once. Do not repeatedly restart a server or retry a
   file operation.
4. Download Home Assistant diagnostics and inspect the redacted copy yourself.
5. Remove tokens, passwords, public addresses, player names, service/account
   IDs, saves, provider configuration, and unrelated logs.

## Common first-run failures

- **Token rejected:** create a new Nitrado token and use Reconfigure. Do not
  paste the token into an issue.
- **`/account` unavailable:** supported when `/services` succeeds. The
  integration uses a one-way fallback identity and should load quietly.
- **No services discovered:** verify that the Nitrado account owns a service and
  that the token can list `/services`.
- **Profile mismatch:** the generic Nitrado cockpit must remain available. File
  a sanitized issue with the game/folder metadata, never the token or save.
- **Provider outage:** cached state is labeled stale and controls fail closed.
  Wait for Nitrado recovery, then reload or refresh.
- **Provider-file Repair:** do not delete recovery data blindly. Follow the
  Repair text and preserve any remote recovery backup until the incident is
  understood.

## Upgrades and rollback

Back up Home Assistant before upgrading. Downgrades are not supported because
config-entry and storage migrations are forward-only. Restore the Home
Assistant backup taken before the upgrade if rollback is required.

## Safe issue content

Use the repository issue form once the repository is published. Include Home
Assistant version, integration version, sanitized diagnostics, exact steps,
expected behavior, actual behavior, and confirmation that secrets/personal data
were removed. Never upload a save file unless a maintainer explicitly requests
a synthetic reproduction in a private channel.
