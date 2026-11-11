"""Attachment malware scanning.

The protocol tests talk to a real TCP server that speaks clamd's INSTREAM framing, so the
client's wire format is exercised for real. (A run against an actual ClamAV daemon is
recorded in docs/SECURITY_REVIEW.md.) The fake flags any stream containing ``MARKER``.
"""

from __future__ import annotations

import io
import socketserver
import struct
import threading

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from billing.models import Plan, Subscription
from billing.services import ensure_default_plans
from kernel.models import AuditLog, Notification
from queues.models import Job
from tasks import scanning
from tasks.models import Attachment

pytestmark = pytest.mark.django_db

MARKER = b"FAKE-MALWARE-MARKER"


class FakeClamd(socketserver.BaseRequestHandler):
    mode = "normal"
    received: list[bytes] = []

    def _exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.request.recv(count - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def handle(self):
        command = b""
        while not command.endswith(b"\0"):
            byte = self.request.recv(1)
            if not byte:
                return
            command += byte
        if command == b"zPING\0":
            self.request.sendall(b"PONG\0")
            return
        assert command == b"zINSTREAM\0", command

        body = b""
        while True:
            (size,) = struct.unpack("!L", self._exact(4))
            if size == 0:
                break
            body += self._exact(size)
        type(self).received.append(body)

        if type(self).mode == "error":
            self.request.sendall(b"INSTREAM size limit exceeded. ERROR\0")
        elif type(self).mode == "hangup":
            return
        elif MARKER in body:
            self.request.sendall(b"stream: Win.Test.FAKE-1 FOUND\0")
        else:
            self.request.sendall(b"stream: OK\0")


@pytest.fixture
def clamd(settings):
    FakeClamd.mode = "normal"
    FakeClamd.received = []
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeClamd)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.ATTACHMENT_SCAN_BACKEND = "clamav"
    settings.CLAMAV_HOST = "127.0.0.1"
    settings.CLAMAV_PORT = server.server_address[1]
    settings.CLAMAV_TIMEOUT_SECONDS = 5
    yield FakeClamd
    server.shutdown()
    server.server_close()


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def team(organization):
    """Attachments are a paid-plan feature."""

    ensure_default_plans()
    Subscription.objects.update_or_create(
        organization=organization,
        defaults={"plan": Plan.objects.get(code="team"), "status": Subscription.Status.ACTIVE},
    )
    return organization


def upload(client, task, content=b"hello world", name="notes.txt"):
    return client.post(
        f"/tasks/{task.pk}/attachments/",
        {"file": SimpleUploadedFile(name, content, content_type="text/plain")},
    )


def run_jobs():
    from queues.tasks import dispatch_available_jobs

    return dispatch_available_jobs()


class TestProtocol:
    def test_clean_stream(self, clamd):
        assert scanning.scan_stream(io.BytesIO(b"just text")) == scanning.ScanResult(clean=True)

    def test_infected_stream_reports_the_signature(self, clamd):
        result = scanning.scan_stream(io.BytesIO(b"aaa" + MARKER + b"bbb"))
        assert not result.clean and result.signature == "Win.Test.FAKE-1"

    def test_whole_file_is_sent_in_chunks(self, clamd):
        payload = bytes(range(256)) * 1024  # 256 KiB: four full chunks
        scanning.scan_stream(io.BytesIO(payload))
        assert clamd.received[-1] == payload

    def test_empty_file_is_scannable(self, clamd):
        assert scanning.scan_stream(io.BytesIO(b"")).clean

    def test_scanner_error_is_not_a_clean_verdict(self, clamd):
        clamd.mode = "error"
        with pytest.raises(scanning.ScanError):
            scanning.scan_stream(io.BytesIO(b"data"))

    def test_dropped_connection_is_not_a_clean_verdict(self, clamd):
        clamd.mode = "hangup"
        with pytest.raises(scanning.ScanError):
            scanning.scan_stream(io.BytesIO(b"data"))

    def test_unreachable_scanner_raises(self, settings):
        settings.ATTACHMENT_SCAN_BACKEND = "clamav"
        settings.CLAMAV_HOST, settings.CLAMAV_PORT, settings.CLAMAV_TIMEOUT_SECONDS = "127.0.0.1", 1, 1
        with pytest.raises(scanning.ScanError):
            scanning.scan_stream(io.BytesIO(b"data"))
        assert scanning.ping() is False

    def test_ping(self, clamd):
        assert scanning.ping() is True

    @pytest.mark.parametrize(
        ("reply", "clean"),
        [("stream: OK", True), ("stream: Eicar-Signature FOUND", False), ("stream: Win.Trojan.X-1 FOUND", False)],
    )
    def test_reply_parsing(self, reply, clean):
        assert scanning.parse_reply(reply).clean is clean

    @pytest.mark.parametrize(
        "reply", ["", "garbage", "INSTREAM size limit exceeded. ERROR", "stream: OK ERROR", "OK-ish"]
    )
    def test_unrecognised_replies_are_errors(self, reply):
        with pytest.raises(scanning.ScanError):
            scanning.parse_reply(reply)


class TestUploadLifecycle:
    def test_scanning_off_leaves_uploads_available(self, client_owner, team, task, media):
        assert upload(client_owner, task).status_code == 302
        attachment = Attachment.objects.get()
        assert attachment.scan_status == Attachment.ScanStatus.NOT_SCANNED
        assert client_owner.get(f"/attachments/{attachment.pk}/").status_code == 200
        assert not Job.objects.filter(handler="internal:scan_attachment").exists()

    def test_upload_is_held_until_scanned(
        self, client_owner, team, task, media, clamd, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            upload(client_owner, task)
        attachment = Attachment.objects.get()
        assert attachment.scan_status == Attachment.ScanStatus.PENDING

        held = client_owner.get(f"/attachments/{attachment.pk}/")
        assert held.status_code == 409 and held["Retry-After"]
        assert b"Scanning for malware" in client_owner.get(f"/tasks/{task.pk}/").content

    def test_clean_file_becomes_downloadable(
        self, client_owner, team, task, media, clamd, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            upload(client_owner, task, b"quarterly numbers")
        run_jobs()

        attachment = Attachment.objects.get()
        assert attachment.scan_status == Attachment.ScanStatus.CLEAN and attachment.scanned_at
        response = client_owner.get(f"/attachments/{attachment.pk}/")
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == b"quarterly numbers"

    def test_infected_file_is_deleted_and_reported(
        self, client_owner, team, task, media, clamd, user, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            upload(client_owner, task, b"x" + MARKER, name="invoice.pdf")
        stored = Attachment.objects.get().file.name
        run_jobs()

        attachment = Attachment.objects.get()
        assert attachment.scan_status == Attachment.ScanStatus.INFECTED
        assert attachment.scan_signature == "Win.Test.FAKE-1"
        assert not attachment.file.storage.exists(stored)
        assert client_owner.get(f"/attachments/{attachment.pk}/").status_code == 410
        assert b"malware detected" in client_owner.get(f"/tasks/{task.pk}/").content
        entry = AuditLog.objects.get(action="attachment.infected")
        assert entry.metadata["signature"] == "Win.Test.FAKE-1" and entry.organization == team
        assert Notification.objects.filter(user=user, title="An attachment was removed").exists()

    def test_scanner_outage_keeps_the_file_held_and_retries(
        self, client_owner, team, task, media, clamd, django_capture_on_commit_callbacks
    ):
        clamd.mode = "error"
        with django_capture_on_commit_callbacks(execute=True):
            upload(client_owner, task)
        result = run_jobs()

        attachment = Attachment.objects.get()
        assert attachment.scan_status == Attachment.ScanStatus.PENDING
        assert client_owner.get(f"/attachments/{attachment.pk}/").status_code == 409
        assert result["retried"] >= 1
        job = Job.objects.get(handler="internal:scan_attachment")
        assert job.status in Job.CLAIMABLE_STATUSES

    def test_scan_is_idempotent(
        self, client_owner, team, task, media, clamd, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            upload(client_owner, task)
        attachment = Attachment.objects.get()
        scanning.queue_scan(attachment)
        scanning.queue_scan(attachment)
        assert Job.objects.filter(handler="internal:scan_attachment").count() == 1

        run_jobs()
        attachment.refresh_from_db()
        assert scanning.scan_attachment(attachment) is None  # verdict stands; not rescanned
        assert len(clamd.received) == 1

    def test_job_for_a_deleted_attachment_is_a_no_op(
        self, client_owner, team, task, media, clamd, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            upload(client_owner, task)
        Attachment.objects.all().delete()
        assert run_jobs()["succeeded"] >= 1

    def test_job_cannot_scan_another_workspaces_attachment(
        self, client_owner, team, task, media, clamd, other_organization, django_capture_on_commit_callbacks
    ):
        from queues.services import enqueue_job, get_internal_queue

        with django_capture_on_commit_callbacks(execute=True):
            upload(client_owner, task)
        attachment = Attachment.objects.get()
        Job.objects.all().delete()
        enqueue_job(
            organization=other_organization,
            queue=get_internal_queue(other_organization),
            source="forged",
            handler_type=Job.HandlerType.INTERNAL,
            handler="internal:scan_attachment",
            payload={"attachment_id": attachment.pk},
            idempotency_key="forged",
        )
        run_jobs()
        attachment.refresh_from_db()
        assert attachment.scan_status == Attachment.ScanStatus.PENDING and clamd.received == []

    def test_turning_scanning_on_holds_older_uploads(self, client_owner, team, task, media, settings):
        upload(client_owner, task)
        attachment = Attachment.objects.get()
        settings.ATTACHMENT_SCAN_BACKEND = "clamav"
        assert client_owner.get(f"/attachments/{attachment.pk}/").status_code == 409

    def test_backfill_command_queues_unresolved_attachments(self, client_owner, team, task, media, clamd):
        from django.core.management import call_command

        Attachment.objects.create(
            organization=team,
            task=task,
            file=SimpleUploadedFile("old.txt", b"old"),
            original_name="old.txt",
            size_bytes=3,
        )
        call_command("scan_attachments")
        run_jobs()
        assert Attachment.objects.get().scan_status == Attachment.ScanStatus.CLEAN

    def test_backfill_command_refuses_without_a_scanner(self, settings):
        from django.core.management import CommandError, call_command

        settings.ATTACHMENT_SCAN_BACKEND = ""
        with pytest.raises(CommandError):
            call_command("scan_attachments")

    def test_infected_file_stays_blocked_even_with_scanning_off(self, settings):
        attachment = Attachment(scan_status=Attachment.ScanStatus.INFECTED)
        settings.ATTACHMENT_SCAN_BACKEND = ""
        assert not attachment.is_downloadable


class TestDeployChecks:
    def test_warns_when_attachments_are_unscanned(self, settings):
        from tasks.checks import attachments_are_scanned

        settings.ATTACHMENT_SCAN_BACKEND = ""
        assert [w.id for w in attachments_are_scanned(None)] == ["tasks.W001"]
        settings.ATTACHMENT_SCAN_BACKEND = "clamav"
        assert attachments_are_scanned(None) == []
