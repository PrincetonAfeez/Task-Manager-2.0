"""Queue scans for attachments that have no verdict.

Needed once when scanning is switched on for an existing deployment: files uploaded before
that are ``not_scanned`` and become undownloadable until they have been checked.
"""

from django.core.management.base import BaseCommand, CommandError

from tasks.models import Attachment
from tasks.scanning import queue_scan, scanning_enabled


class Command(BaseCommand):
    help = "Queue a malware scan for every attachment without a verdict."

    def handle(self, *args, **options):
        if not scanning_enabled():
            raise CommandError("ATTACHMENT_SCAN_BACKEND is not set; there is no scanner to use.")

        unresolved = Attachment.objects.filter(
            scan_status__in=[Attachment.ScanStatus.NOT_SCANNED, Attachment.ScanStatus.PENDING]
        ).select_related("organization")
        count = 0
        for attachment in unresolved.iterator():
            Attachment.objects.filter(pk=attachment.pk).update(scan_status=Attachment.ScanStatus.PENDING)
            queue_scan(attachment)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Queued {count} attachment scan(s)."))
