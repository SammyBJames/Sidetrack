from collections import defaultdict
from csv import DictReader
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from app.models import MediaTypes
from integrations.imports.helpers import parse_added_date


class Command(BaseCommand):
    """Give entries imported before their added dates were kept those of the file."""

    help = (
        "Set when a user's entries were added from the Yamtrack export they were "
        "imported from. Episodes are left as they are."
    )

    def add_arguments(self, parser):
        """Take the user, the export file and whether to only report."""
        parser.add_argument("username")
        parser.add_argument("file", type=Path)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without saving it.",
        )

    def handle(self, *_args, **options):
        """Match the file's rows to the user's entries and date them."""
        user = get_user_model().objects.filter(username=options["username"]).first()
        if user is None:
            msg = f"No user named {options['username']}"
            raise CommandError(msg)

        try:
            with options["file"].open(encoding="utf-8", newline="") as file:
                dates = read_added_dates(DictReader(file))
        except OSError as error:
            raise CommandError(str(error)) from error

        updated, skipped = backfill(user, dates, dry_run=options["dry_run"])

        verb = "Would date" if options["dry_run"] else "Dated"
        self.stdout.write(f"{verb} {updated} entries.")
        for media_type, source, media_id, season_number in skipped:
            season = f" season {season_number}" if season_number is not None else ""
            self.stdout.write(
                f"Skipped {media_type} {source}/{media_id}{season}: the file has "
                "more entries of it than are tracked.",
            )


def read_added_dates(rows):
    """Return each media's added dates in the file's order, by its key.

    The key is the media type, source, ID and season number.
    """
    dates = defaultdict(list)
    for row in rows:
        if row.get("media_type") == MediaTypes.EPISODE.value:
            continue
        created_at = parse_added_date(row.get("created_at"))
        if created_at is None:
            continue
        season_number = row["season_number"]
        key = (
            row["media_type"],
            row["source"],
            row["media_id"],
            int(season_number) if season_number else None,
        )
        dates[key].append(created_at)
    return dates


def backfill(user, dates, *, dry_run=False):
    """Date the user's entries by the file's, and return how many and what's skipped.

    An import adds a media's entries in the file's order, so they're matched in
    the order they were added. Entries added since come after and are left as
    they are, while a media with fewer entries than the file is skipped.
    """
    to_update = defaultdict(list)
    skipped = []
    for key, added_dates in dates.items():
        media_type, source, media_id, season_number = key
        if media_type not in MediaTypes.values:
            continue
        model = apps.get_model(app_label="app", model_name=media_type)
        entries = list(
            model.objects.filter(
                user=user,
                item__media_type=media_type,
                item__source=source,
                item__media_id=media_id,
                item__season_number=season_number,
            ).order_by("id"),
        )
        if len(entries) < len(added_dates):
            skipped.append(key)
            continue
        for entry, created_at in zip(entries, added_dates, strict=False):
            entry.created_at = created_at
            to_update[model].append(entry)

    if not dry_run:
        for model, entries in to_update.items():
            model.objects.bulk_update(entries, ["created_at"], batch_size=500)
    return sum(len(entries) for entries in to_update.values()), skipped
