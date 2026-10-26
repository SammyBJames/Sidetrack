from django.conf import settings
from django.core.management.base import BaseCommand

from app import item_details


class Command(BaseCommand):
    """Fetch the details of library items that have none or old ones."""

    help = (
        "Refresh the stored details of the items in anyone's library that have "
        "none or are a week old, never-fetched first. Stops at the daily budget "
        "unless --all is given."
    )

    def add_arguments(self, parser):
        """Take whether to ignore the daily budget."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="Refresh every item that needs it, not only up to the daily budget.",
        )

    def handle(self, *_args, **options):
        """Run the refresh now and report how it went."""
        limit = None if options["all"] else settings.LIBRARY_DETAILS_BATCH
        refreshed, failed = item_details.refresh_library_details(limit)
        self.stdout.write(f"Refreshed {refreshed} items, {failed} failed.")
