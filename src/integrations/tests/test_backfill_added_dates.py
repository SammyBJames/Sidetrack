import tempfile
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from app.models import TV, Item, MediaTypes, Movie, Season, Sources, Status

HEADER = '"media_id","source","media_type","season_number","created_at"\n'


class BackfillAddedDatesTests(TestCase):
    """Test dating imported entries from the export they came from."""

    def setUp(self):
        """Create a user with a movie watched twice and a season."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        movie = Item.objects.create(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Matrix",
            image="https://example.com/image.jpg",
        )
        self.first, self.second = (
            Movie.objects.create(item=movie, user=self.user, status=status)
            for status in (Status.COMPLETED.value, Status.IN_PROGRESS.value)
        )
        show = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Friends",
            image="https://example.com/image.jpg",
        )
        tv = TV(item=show, user=self.user, status=Status.PLANNING.value)
        TV.objects.bulk_create([tv])
        season = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="https://example.com/image.jpg",
            season_number=1,
        )
        self.season = Season(
            item=season,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        Season.objects.bulk_create([self.season])

    def run_command(self, *rows, dry_run=False):
        """Run the command on a file of the rows, and return its output."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            path.write_text(HEADER + "".join(rows), encoding="utf-8")
            out = StringIO()
            args = ["backfill_added_dates", "test", str(path)]
            if dry_run:
                args.append("--dry-run")
            call_command(*args, stdout=out)
        return out.getvalue()

    def test_entries_are_dated_in_the_order_they_were_added(self):
        """The file's rows date the entries in order, seasons by their number."""
        out = self.run_command(
            '"603","tmdb","movie","","2023-05-04 08:30:00+00:00"\n',
            '"603","tmdb","movie","","2025-03-01 10:00:00+00:00"\n',
            '"1668","tmdb","season","1","2024-01-02 00:00:00+00:00"\n',
            '"1668","tmdb","episode","1","2024-01-02 00:00:00+00:00"\n',
        )

        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.season.refresh_from_db()
        self.assertEqual(self.first.created_at, datetime(2023, 5, 4, 8, 30, tzinfo=UTC))
        self.assertEqual(self.second.created_at, datetime(2025, 3, 1, 10, tzinfo=UTC))
        self.assertEqual(self.season.created_at, datetime(2024, 1, 2, tzinfo=UTC))
        self.assertIn("Dated 3 entries.", out)

    def test_entries_added_since_are_kept(self):
        """An entry added after the import keeps its date."""
        added = self.second.created_at
        self.run_command('"603","tmdb","movie","","2023-05-04 08:30:00+00:00"\n')

        self.second.refresh_from_db()
        self.assertEqual(self.second.created_at, added)

    def test_media_with_fewer_entries_is_skipped(self):
        """A file with more entries of a media than are tracked leaves it alone."""
        added = self.season.created_at
        out = self.run_command(
            '"1668","tmdb","season","1","2023-05-04 08:30:00+00:00"\n',
            '"1668","tmdb","season","1","2024-05-04 08:30:00+00:00"\n',
        )

        self.season.refresh_from_db()
        self.assertEqual(self.season.created_at, added)
        self.assertIn("Skipped season tmdb/1668 season 1", out)

    def test_dry_run_saves_nothing(self):
        """A dry run reports what it would date without saving it."""
        added = self.first.created_at
        out = self.run_command(
            '"603","tmdb","movie","","2023-05-04 08:30:00+00:00"\n',
            dry_run=True,
        )

        self.first.refresh_from_db()
        self.assertEqual(self.first.created_at, added)
        self.assertIn("Would date 1 entries.", out)

    def test_unknown_user_is_an_error(self):
        """Naming a user that doesn't exist stops the command."""
        with self.assertRaises(CommandError):
            call_command("backfill_added_dates", "nobody", "export.csv")
