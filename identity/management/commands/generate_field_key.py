from django.core.management.base import BaseCommand

from kernel.crypto import generate_key


class Command(BaseCommand):
    help = "Print a new key for FIELD_ENCRYPTION_KEYS (put the newest key first)."

    def handle(self, *args, **options):
        self.stdout.write(generate_key())
