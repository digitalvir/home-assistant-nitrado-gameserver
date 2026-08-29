# Install, upgrade, removal, and retained data

## Install

HACS installs the `custom_components/nitrado_gameserver` subtree from the
published annotated tag archive. Manual installation copies the same subtree.
Restart Home Assistant and add the integration through Devices & services.

## Upgrade

Create a Home Assistant backup first. Upgrade through HACS, restart Home
Assistant, and verify the config entry, imported devices, Repairs, and Nitrado
Servers panel. Migrations are forward-only.

## Reload versus removal

- **Reload/unload** stops tasks, revokes leases, closes prepared transfers, and
  removes runtime surfaces. Persistent configuration and recovery records stay.
- **Remove config entry** performs the permanent local purge for that account:
  integration-owned grants, journals, recovery blobs/metadata, prepared work,
  Repairs, devices, and entities are removed.
- **HACS uninstall** removes code only. It cannot run cleanup after deletion.

Home Assistant recorder history and Home Assistant backups are outside the
integration's storage and remain subject to Home Assistant retention.

## Correct uninstall order

1. Resolve or preserve any active recovery incident.
2. Remove every Nitrado Game Server config entry.
3. Confirm its devices and Repairs are gone.
4. Uninstall through HACS.
5. Restart Home Assistant.

Remote provider recovery backups are intentionally retained. Delete them only
through Nitrado after independently confirming they are no longer needed.

## Code removed first

Reinstall the same or a newer version. Home Assistant can then load the config
entry and execute supported removal/purge logic. Removing `.storage` files by
hand is unsupported and can damage unrelated Home Assistant state.

## Reinstall

After a proper config-entry removal, reinstall starts clean. After an ordinary
unload or code-only uninstall, reinstall resumes the retained configuration and
recovery truth rather than pretending it was deleted.
