"""End-to-end backend HTTP contract for private save-bundle jobs."""

from __future__ import annotations

import asyncio
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.nitrado_gameserver.cockpit import (
    cockpit_state,
    issue_cockpit_lease,
    issue_editable_preview_grant,
    issue_save_bundle_preview_grant,
)
from custom_components.nitrado_gameserver.const import DOMAIN
from custom_components.nitrado_gameserver.filesystem import FileTransportError, FileTreeManifest
from custom_components.nitrado_gameserver.save_bundle_jobs import require_save_bundle_job, start_save_bundle_job
from custom_components.nitrado_gameserver.views import (
    CockpitCapabilityView,
    CockpitSaveBundleDownloadView,
    CockpitSaveBundleInspectView,
    ProfileEditableFileApplyView,
    ProfileEditableFileRollbackView,
)


def payload(value: object) -> dict[str, object]:
    """Decode either the no-HA fallback mapping or an aiohttp response."""

    if isinstance(value, dict):
        return value
    return json.loads(str(value.text))


class Content:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class Request:
    def __init__(
        self,
        hass: Hass,
        body: dict[str, object] | None = None,
        *,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.app = {"hass": hass}
        self._body = body or {}
        self._user = SimpleNamespace(id="admin", is_admin=True)
        self.headers = headers or {}
        self.content = Content(chunks)
        self.content_length = sum(map(len, chunks)) if chunks else None
        self.content_type = "application/zip"

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._user if key == "hass_user" else default

    async def json(self) -> dict[str, object]:
        return self._body


class Hass:
    def __init__(self, coordinator: Coordinator) -> None:
        self.data: dict[str, object] = {DOMAIN: {coordinator.account_entry_id: coordinator}}
        self.tasks: list[asyncio.Task[None]] = []

    def async_create_task(self, coroutine, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        return task


class Export:
    def __init__(self, content: bytes) -> None:
        self.filename = "save.zip"
        self.stream = io.BytesIO(content)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.stream.close()


class Preview:
    key = "world"
    name = "World"
    world_id = "world-id"
    target_root = "/save/world-id"
    changed = False
    current = FileTreeManifest((), 0)
    proposed = FileTreeManifest((), 0)
    added = ()
    replaced = ()
    unchanged = ()
    preserved = ()
    upload = SimpleNamespace(portable_manifest=True, source_prefix="")

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Coordinator:
    account_entry_id = "entry-1"

    def __init__(self) -> None:
        profile = SimpleNamespace(profile_id="palworld")
        self.runtime = SimpleNamespace(profile=profile, profile_manifest=SimpleNamespace(cockpit=None))
        self.services = {"service-1": self.runtime}
        self.token = (1, "palworld", 2, 3, 4)
        self.export: Export | None = None
        self.preview: Preview | None = None
        self.uploaded = b""
        self.fail_export = False
        self.mutation_calls: list[str] = []

    def get_runtime(self, service_id: str) -> object:
        return self.services[service_id]

    def profile_dispatch_token(self, service_id: str) -> tuple[int, str, int, int, int]:
        self.get_runtime(service_id)
        return self.token

    def require_profile_dispatch_token(self, service_id: str, token: object) -> object:
        if token != self.token:
            raise RuntimeError("stale")
        return self.get_runtime(service_id)

    async def async_export_save_bundle(self, *_args: object, **_kwargs: object) -> Export:
        if self.fail_export:
            raise FileTransportError("private socket detail")
        self.export = Export(b"PK\x03\x04fixture-zip")
        return self.export

    async def async_inspect_save_bundle(self, _service: str, _key: str, stream, **_kwargs: object) -> Preview:
        self.uploaded = stream.read()
        self.preview = Preview()
        return self.preview

    async def async_apply_editable_file(self, *_args: object, **_kwargs: object) -> object:
        self.mutation_calls.append("editable-apply")
        raise AssertionError("disabled editor gate must reject before mutation dispatch")

    async def async_apply_save_bundle(self, *_args: object, **_kwargs: object) -> object:
        self.mutation_calls.append("save-bundle-apply")
        raise AssertionError("disabled restore gate must reject before mutation dispatch")

    async def async_require_editable_file_recovery(self, *_args: object, **_kwargs: object) -> object:
        self.mutation_calls.append("editable-rollback")
        raise AssertionError("disabled editor gate must reject before recovery lookup")


class StreamResponse:
    def __init__(self, *, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self.body = bytearray()
        self.prepared = False
        self.finished = False

    async def prepare(self, _request: object) -> None:
        self.prepared = True

    async def write(self, chunk: bytes) -> None:
        self.body.extend(chunk)

    async def write_eof(self) -> None:
        self.finished = True


class SaveBundleHttpLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.coordinator = Coordinator()
        self.hass = Hass(self.coordinator)
        self.lease = issue_cockpit_lease(
            self.hass,
            user_id="admin",
            coordinator=self.coordinator,
            service_id="service-1",
            declaration=None,
            mount_epoch="mount-1",
        )

    async def capability(self, capability: str, **body: object) -> dict[str, object]:
        response = await CockpitCapabilityView().post(
            Request(self.hass, {"lease": self.lease.token, **body}),
            "entry-1",
            "service-1",
            capability,
        )
        return payload(response)

    async def assert_capability_disabled(self, capability: str, message: str, **body: object) -> None:
        with self.assertRaises(Exception) as raised:  # aiohttp and the source-only fallback use different types.
            await CockpitCapabilityView().post(
                Request(self.hass, {"lease": self.lease.token, **body}),
                "entry-1",
                "service-1",
                capability,
            )
        self.assertEqual(getattr(raised.exception, "status", 400), 400)
        self.assertIn(message, str(getattr(raised.exception, "text", raised.exception)))

    async def test_disabled_write_gates_preserve_grants_and_never_dispatch(self) -> None:
        editable = issue_editable_preview_grant(
            self.hass,
            lease=self.lease,
            file_key="settings",
            declared_path="/game/settings.ini",
            source_revision="1" * 64,
            proposed_revision="2" * 64,
        )
        await self.assert_capability_disabled(
            "editable-apply",
            "Editable-file writes are disabled",
            confirm=True,
            file_key="settings",
            source_revision="1" * 64,
            operations=[{"op": "set", "key": "difficulty", "raw_value": "2"}],
            preview_token=editable.token,
        )
        await self.assert_capability_disabled(
            "editable-rollback",
            "Editable-file rollback is disabled",
            confirm=True,
            file_key="settings",
            recovery_id="recovery-1",
        )

        preview = Preview()
        save = issue_save_bundle_preview_grant(
            self.hass,
            lease=self.lease,
            preview=preview,
            expected_current_digest="3" * 64,
            proposed_digest="4" * 64,
        )
        await self.assert_capability_disabled(
            "save-bundle-apply",
            "Save-game restore is disabled",
            confirm=True,
            bundle_key="world",
            preview_token=save.token,
        )

        state = cockpit_state(self.hass)
        self.assertIs(state.editable_previews[editable.token], editable)
        self.assertIs(state.save_bundle_previews[save.token], save)
        self.assertFalse(preview.closed)
        self.assertEqual(self.coordinator.mutation_calls, [])

        for view, operation in (
            (ProfileEditableFileApplyView(), "writes"),
            (ProfileEditableFileRollbackView(), "rollback"),
        ):
            with self.assertRaises(Exception) as raised:
                await view.post(Request(self.hass, {"confirm": True}), "service-1", "settings")
            self.assertEqual(getattr(raised.exception, "status", 400), 400)
            self.assertIn(f"Editable-file {operation}", str(getattr(raised.exception, "text", raised.exception)))

    async def test_download_upload_review_and_error_cleanup(self) -> None:
        transfer = await self.capability("save-bundle-transfer", bundle_key="world", action="download")
        started = payload(
            await CockpitSaveBundleDownloadView().post(
                Request(self.hass, headers={"X-Nitrado-Save-Transfer": str(transfer["transfer_token"])}),
                "entry-1",
                "service-1",
                "world",
            )
        )
        self.assertEqual(started["status"], "running")
        await self.hass.tasks[-1]

        ready = await self.capability(
            "save-bundle-job-status",
            bundle_key="world",
            action="download",
            job_id=started["job_id"],
        )
        self.assertEqual(ready["progress"]["stage"], "ready")
        with patch(
            "custom_components.nitrado_gameserver.views.web",
            SimpleNamespace(StreamResponse=StreamResponse),
        ):
            downloaded = await CockpitSaveBundleDownloadView().post(
                Request(
                    self.hass,
                    headers={
                        "X-Nitrado-Save-Transfer": str(ready["transfer_token"]),
                        "X-Nitrado-Save-Job": str(started["job_id"]),
                    },
                ),
                "entry-1",
                "service-1",
                "world",
            )
        self.assertEqual(bytes(downloaded.body), b"PK\x03\x04fixture-zip")
        self.assertTrue(downloaded.prepared and downloaded.finished)
        self.assertTrue(self.coordinator.export and self.coordinator.export.closed)
        with self.assertRaises(ValueError):
            require_save_bundle_job(
                self.hass,
                job_id=str(started["job_id"]),
                user_id="admin",
                account_entry_id="entry-1",
                service_id="service-1",
                bundle_key="world",
                action="download",
            )

    async def test_running_status_returns_current_structured_progress(self) -> None:
        release = asyncio.Event()

        async def work(progress):
            progress(
                "excluding_history",
                "Skipping profile-excluded save history — only editor-facing files belong in this ZIP.",
                {"files_done": 2, "bytes_done": 1024},
            )
            await release.wait()
            return Export(b"PK\x03\x04fixture-zip")

        job = start_save_bundle_job(
            self.hass,
            user_id="admin",
            account_entry_id="entry-1",
            service_id="service-1",
            bundle_key="world",
            action="download",
            operation=work,
        )
        await asyncio.sleep(0)
        running = await self.capability(
            "save-bundle-job-status",
            bundle_key="world",
            action="download",
            job_id=job.job_id,
        )
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["progress"]["stage"], "excluding_history")
        self.assertEqual(running["progress"]["files_done"], 2)
        self.assertEqual(running["progress"]["bytes_done"], 1024)
        release.set()
        await job.task

        upload_bytes = b"PK\x03\x04uploaded-zip"
        transfer = await self.capability("save-bundle-transfer", bundle_key="world", action="inspect")
        inspected = payload(
            await CockpitSaveBundleInspectView().post(
                Request(
                    self.hass,
                    headers={"X-Nitrado-Save-Transfer": str(transfer["transfer_token"])},
                    chunks=(upload_bytes[:5], upload_bytes[5:]),
                ),
                "entry-1",
                "service-1",
                "world",
            )
        )
        await self.hass.tasks[-1]
        reviewed = await self.capability(
            "save-bundle-job-status",
            bundle_key="world",
            action="inspect",
            job_id=inspected["job_id"],
        )
        self.assertEqual(self.coordinator.uploaded, upload_bytes)
        self.assertEqual(
            reviewed["preview"]["counts"], {"added": 0, "replaced": 0, "unchanged": 0, "preserved": 0, "deleted": 0}
        )
        self.assertIsNone(reviewed["preview_token"])
        self.assertFalse(reviewed["restore_enabled"])
        self.assertTrue(self.coordinator.preview and self.coordinator.preview.closed)

        self.coordinator.fail_export = True
        transfer = await self.capability("save-bundle-transfer", bundle_key="world", action="download")
        failed_start = payload(
            await CockpitSaveBundleDownloadView().post(
                Request(self.hass, headers={"X-Nitrado-Save-Transfer": str(transfer["transfer_token"])}),
                "entry-1",
                "service-1",
                "world",
            )
        )
        await self.hass.tasks[-1]
        failed = await self.capability(
            "save-bundle-job-status",
            bundle_key="world",
            action="download",
            job_id=failed_start["job_id"],
        )
        self.assertEqual(failed["status"], "error")
        self.assertIn("No server data was changed", str(failed["message"]))
        self.assertNotIn("private socket", str(failed["message"]))
        with self.assertRaises(ValueError):
            require_save_bundle_job(
                self.hass,
                job_id=str(failed_start["job_id"]),
                user_id="admin",
                account_entry_id="entry-1",
                service_id="service-1",
                bundle_key="world",
                action="download",
            )


if __name__ == "__main__":
    unittest.main()
