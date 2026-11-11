"""Malware scanning for uploaded attachments.

An upload is stored, marked ``pending`` and scanned by a durable job. Until the scanner has
called it clean, nobody can download it — the check is on the download path, so a file that
is still pending, or whose scan keeps failing, is simply unavailable rather than served
unscanned. An infected file is deleted from storage; the database row stays as the record
of what happened.

The scanner is ClamAV's daemon, spoken to over its documented ``INSTREAM`` protocol on a
plain socket. That keeps the dependency to a network service with no client library.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from ops.logging import get_logger

logger = get_logger("taskmanager.tasks.scanning")

CHUNK_BYTES = 64 * 1024
MAX_REPLY_BYTES = 4096
HANDLER = "internal:scan_attachment"


class ScanError(Exception):
    """The scanner could not give a verdict (down, timed out, refused the stream)."""


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    signature: str = ""


def scanning_enabled() -> bool:
    return getattr(settings, "ATTACHMENT_SCAN_BACKEND", "") == "clamav"


def _connect() -> socket.socket:
    return socket.create_connection(
        (settings.CLAMAV_HOST, settings.CLAMAV_PORT), timeout=settings.CLAMAV_TIMEOUT_SECONDS
    )


def _read_reply(sock: socket.socket) -> str:
    reply = b""
    while b"\0" not in reply and len(reply) < MAX_REPLY_BYTES:
        chunk = sock.recv(1024)
        if not chunk:
            break
        reply += chunk
    return reply.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def parse_reply(reply: str) -> ScanResult:
    """Turn clamd's one-line answer into a verdict. Anything unrecognised is an error."""

    if reply.endswith("FOUND"):
        signature = reply.rsplit(":", 1)[-1].replace("FOUND", "").strip() if ":" in reply else ""
        return ScanResult(clean=False, signature=signature[:200] or "unknown")
    if reply.endswith("OK") and "ERROR" not in reply:
        return ScanResult(clean=True)
    # Includes "INSTREAM size limit exceeded. ERROR": a file the scanner would not read is
    # not a clean file.
    raise ScanError(f"Scanner did not return a verdict: {reply[:120]!r}")


def scan_stream(stream) -> ScanResult:
    """Send ``stream`` to clamd and return its verdict."""

    try:
        with _connect() as sock:
            sock.sendall(b"zINSTREAM\0")
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
            reply = _read_reply(sock)
    except OSError as exc:
        raise ScanError(f"Scanner unreachable: {type(exc).__name__}") from exc
    return parse_reply(reply)


def ping() -> bool:
    try:
        with _connect() as sock:
            sock.sendall(b"zPING\0")
            return _read_reply(sock) == "PONG"
    except OSError:
        return False


# --------------------------------------------------------------------------------------
# Attachment lifecycle
# --------------------------------------------------------------------------------------


def initial_status() -> str:
    from .models import Attachment

    return Attachment.ScanStatus.PENDING if scanning_enabled() else Attachment.ScanStatus.NOT_SCANNED


def queue_scan(attachment) -> None:
    """Enqueue the scan. Idempotent per attachment, so a double submit scans once."""

    from queues.models import Job
    from queues.services import enqueue_job, get_internal_queue

    enqueue_job(
        organization=attachment.organization,
        queue=get_internal_queue(attachment.organization),
        source="attachment",
        handler_type=Job.HandlerType.INTERNAL,
        handler=HANDLER,
        payload={"attachment_id": attachment.pk},
        idempotency_key=f"scan-attachment:{attachment.pk}",
    )


def is_downloadable(attachment) -> bool:
    from .models import Attachment

    if attachment.scan_status == Attachment.ScanStatus.INFECTED:
        return False
    if scanning_enabled():
        return attachment.scan_status == Attachment.ScanStatus.CLEAN
    return True


def scan_attachment(attachment) -> ScanResult | None:
    """Scan one stored attachment and record the outcome. Raises :class:`ScanError`."""

    from kernel.services import record_audit

    from .models import Attachment
    from .notifications import deliver_notification

    if attachment.scan_status in (Attachment.ScanStatus.CLEAN, Attachment.ScanStatus.INFECTED):
        return None

    try:
        with attachment.file.open("rb") as stream:
            result = scan_stream(stream)
    except FileNotFoundError:
        logger.warning("attachment file missing at scan time", extra={"attachment_id": attachment.pk})
        return None

    now = timezone.now()
    if result.clean:
        Attachment.objects.filter(pk=attachment.pk).update(
            scan_status=Attachment.ScanStatus.CLEAN, scanned_at=now, scan_signature=""
        )
        return result

    # Quarantine by deletion: there is no reason to keep known malware in customer storage.
    stored_name = attachment.file.name
    attachment.file.storage.delete(stored_name)
    Attachment.objects.filter(pk=attachment.pk).update(
        scan_status=Attachment.ScanStatus.INFECTED, scanned_at=now, scan_signature=result.signature
    )
    logger.warning(
        "infected attachment removed",
        extra={"attachment_id": attachment.pk, "organization_id": attachment.organization_id, "signature": result.signature},
    )
    record_audit(
        action="attachment.infected",
        organization=attachment.organization,
        actor=attachment.uploaded_by,
        target_type="attachment",
        target_id=attachment.pk,
        metadata={"signature": result.signature, "name": attachment.original_name, "task_id": attachment.task_id},
    )
    if attachment.uploaded_by is not None:
        deliver_notification(
            organization=attachment.organization,
            user=attachment.uploaded_by,
            title="An attachment was removed",
            body=f"“{attachment.original_name}” was identified as malware ({result.signature}) and deleted.",
            natural_key=f"attachment-infected:{attachment.pk}",
            link_url=f"/tasks/{attachment.task_id}/",
        )
    return result
