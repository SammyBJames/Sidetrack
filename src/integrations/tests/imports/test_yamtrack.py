from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

import app.forms  # noqa: F401  # ensure app.forms is importable in isolation
from app.models import (
    TV,
    Anime,
    Book,
    Episode,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from integrations import exports
from integrations.imports import (
    yamtrack,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class ImportYamtrack(TestCase):
    """Test importing media from Yamtrack CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with Path(mock_path / "import_yamtrack.csv").open("rb") as file:
            self.import_results = yamtrack.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported media."""
        self.assertEqual(Anime.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Manga.objects.filter(user=self.user).count(), 1)
        self.assertEqual(TV.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Season.objects.filter(user=self.user).count(), 1)
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            24,
        )

    def test_historical_records(self):
        """Test historical records creation during import."""
        anime = Anime.objects.filter(user=self.user).first()
        self.assertEqual(anime.history.count(), 1)
        self.assertEqual(
            anime.history.first().history_date,
            datetime(2024, 2, 9, 10, 0, 0, tzinfo=UTC),
        )

        movie = Movie.objects.filter(user=self.user).first()
        self.assertEqual(movie.history.count(), 1)
        self.assertEqual(
            movie.history.first().history_date,
            datetime(2024, 2, 9, 15, 30, 0, tzinfo=UTC),
        )

        tv = TV.objects.filter(user=self.user).first()
        self.assertEqual(tv.history.count(), 1)
        self.assertEqual(
            tv.history.first().history_date,
            datetime(2024, 2, 9, 12, 0, 0, tzinfo=UTC),
        )

    def test_multiline_notes_are_preserved(self):
        """Test that quoted multiline notes keep embedded newlines."""
        csv_content = b""""media_id","source","media_type","title","image","season_number","episode_number","score","status","notes","start_date","end_date","progress","created_at","progressed_at"
"123192","tmdb","tv","The Crowded Room","https://image.tmdb.org/t/p/w500/vRmopCFp0j1eJGbILLsYsYzxmL8.jpg","","","","Completed","test
test","2026-05-14 11:09:00+00:00","2026-05-14 11:09:00+00:00","10","2026-05-14 11:09:26.617144+00:00","2026-05-14 11:09:00+00:00"
"""  # noqa: E501

        yamtrack.importer(BytesIO(csv_content), self.user, "new")

        tv = TV.objects.get(user=self.user, item__media_id="123192")
        self.assertEqual(tv.notes, "test\ntest")

    def test_missing_metadata_handling(self):
        """Test _handle_missing_metadata method directly."""
        test_rows = [
            # TV Show
            {
                "media_id": "1668",
                "source": "tmdb",
                "media_type": "tv",
                "title": "",
                "image": "",
                "season_number": "",
                "episode_number": "",
            },
            {
                "media_id": "1668",
                "source": "tmdb",
                "media_type": "season",
                "title": "",
                "image": "",
                "season_number": "2",
                "episode_number": "",
            },
            # Episode
            {
                "media_id": "1668",
                "source": "tmdb",
                "media_type": "episode",
                "title": "",
                "image": "",
                "season_number": "2",
                "episode_number": "5",
            },
        ]

        importer = yamtrack.YamtrackImporter(None, self.user, "new")

        for row in test_rows:
            # Make copies of original rows to verify they're modified
            original_row = row.copy()

            # Call the method directly
            importer._handle_missing_metadata(
                row,
                row["media_type"],
                row["season_number"],
                row["episode_number"],
            )

            self.assertNotEqual(row["title"], original_row["title"])
            self.assertNotEqual(row["image"], original_row["image"])


class ImportYamtrackEpisodeHistoryDate(TestCase):
    """Test that episode history dates fall back to end_date (issue #990)."""

    def setUp(self):
        """Create a user with no existing media."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def test_episode_history_date_falls_back_to_end_date(self):
        """Episodes have no progressed_at, so history_date should use end_date.

        Real Yamtrack exports leave the progressed_at column blank for episodes
        (the model has no such field). Without this fallback the activity
        heatmap credits the import day instead of the day watched (issue #990).
        """
        importer = yamtrack.YamtrackImporter(None, self.user, "new")
        row = {
            "media_id": "1668",
            "source": "tmdb",
            "media_type": "episode",
            "title": "Friends",
            "image": "http://image.tmdb.org/t/p/original/friends.jpg",
            "season_number": "1",
            "episode_number": "1",
            "score": "",
            "progress": "",
            "status": "",
            "start_date": "",
            "end_date": "2025-11-19 19:00:00+00:00",
            "notes": "",
            "progressed_at": "",
        }

        importer._process_row(row)

        episode_instance = importer.bulk_media["episode"][0]
        self.assertEqual(
            episode_instance._history_date,
            datetime(2025, 11, 19, 19, 0, 0, tzinfo=UTC),
        )


class ImportYamtrackEpisodeScore(TestCase):
    """Test that episode watches keep their ratings through an import."""

    def setUp(self):
        """Create a user with no existing media."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def import_episode(self, score):
        """Import one watch of an episode with the given score, and return it."""
        importer = yamtrack.YamtrackImporter(None, self.user, "new")
        importer._process_row(
            {
                "media_id": "1668",
                "source": "tmdb",
                "media_type": "episode",
                "title": "Friends",
                "image": "http://image.tmdb.org/t/p/original/friends.jpg",
                "season_number": "1",
                "episode_number": "1",
                "score": score,
                "progress": "",
                "status": "",
                "start_date": "",
                "end_date": "2025-11-19 19:00:00+00:00",
                "notes": "",
                "progressed_at": "",
            },
        )
        return importer.bulk_media["episode"][0]

    def test_episode_score_is_imported(self):
        """A rated watch keeps its rating."""
        self.assertEqual(self.import_episode("7.5").score, Decimal("7.5"))

    def test_invalid_episode_score_is_left_out(self):
        """A rating that isn't a valid score leaves the watch unrated."""
        self.assertIsNone(self.import_episode("11").score)
        self.assertIsNone(self.import_episode("good").score)


class ImportYamtrackPartials(TestCase):
    """Test importing yamtrack media with no ID."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with Path(mock_path / "import_yamtrack_partials.csv").open("rb") as file:
            self.import_results = yamtrack.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported media."""
        self.assertEqual(Book.objects.filter(user=self.user).count(), 3)
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)

    def test_season_episode_search_by_title(self):
        """Test that seasons and episodes can be resolved by title (no media_id).

        This test verifies the fix that allows searching for SEASON and EPISODE
        media types by searching for the parent TV show on TMDB. Before the fix,
        services.search() didn't handle SEASON/EPISODE types and would fail with:
        UnboundLocalError: cannot access local variable 'response'
        """
        test_rows = [
            # Season with title only (no media_id)
            {
                "media_id": "",
                "source": "",
                "media_type": "season",
                "title": "Friends",
                "image": "",
                "season_number": "1",
                "episode_number": "",
            },
            # Episode with title only (no media_id)
            {
                "media_id": "",
                "source": "",
                "media_type": "episode",
                "title": "Friends",
                "image": "",
                "season_number": "1",
                "episode_number": "1",
            },
        ]

        importer = yamtrack.YamtrackImporter(None, self.user, "new")

        for row in test_rows:
            original_row = row.copy()

            importer._handle_missing_metadata(
                row,
                row["media_type"],
                int(row["season_number"]) if row["season_number"] else None,
                int(row["episode_number"]) if row["episode_number"] else None,
            )

            # Verify media_id was resolved from TMDB search
            self.assertNotEqual(row["media_id"], original_row["media_id"])
            self.assertEqual(str(row["media_id"]), "1668")  # Friends TV show ID
            self.assertEqual(row["source"], "tmdb")
            # Title and image should be populated from TMDB
            self.assertNotEqual(row["title"], "")
            self.assertNotEqual(row["image"], "")

    def test_end_dates(self):
        """Test end dates during import."""
        book = Book.objects.filter(user=self.user).first()
        self.assertEqual(book.history.count(), 1)
        bookqs = Book.objects.filter(
            user=self.user,
            item__title="Warlock",
        ).order_by("-end_date")
        books = list(bookqs)

        self.assertEqual(len(books), 3)
        self.assertEqual(
            books[0].end_date,
            datetime(2024, 5, 9, 0, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(
            books[1].end_date,
            datetime(2024, 4, 9, 0, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(
            books[2].end_date,
            datetime(2024, 3, 9, 0, 0, 0, tzinfo=UTC),
        )


class ImportYamtrackNewMode(TestCase):
    """Test that "new" mode only imports what is missing from Yamtrack."""

    HEADER = (
        '"media_id","source","media_type","title","image","season_number",'
        '"episode_number","score","progress","status","start_date","end_date",'
        '"notes","progressed_at"\n'
    )

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def get_item(self, media_type, media_id, season_number=None, episode_number=None):
        """Get or create an item."""
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value
            if media_type != MediaTypes.ANIME.value
            else Sources.MAL.value,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
            defaults={"title": "Title", "image": "https://example.com/image.jpg"},
        )
        return item

    def create_show(self, status, episodes):
        """Create an existing show with watched episodes of season 1."""
        tv = TV(
            item=self.get_item(MediaTypes.TV.value, "1668"),
            user=self.user,
            status=status,
        )
        TV.objects.bulk_create([tv])
        season = Season(
            item=self.get_item(MediaTypes.SEASON.value, "1668", 1),
            user=self.user,
            related_tv=tv,
            status=status,
        )
        Season.objects.bulk_create([season])
        Episode.objects.bulk_create(
            [
                Episode(
                    item=self.get_item(MediaTypes.EPISODE.value, "1668", 1, number),
                    related_season=season,
                    end_date=end_date,
                )
                for number, end_date in episodes
            ],
        )

    def row(self, media_type, media_id="1668", **fields):
        """Return a CSV row of the export."""
        values = {
            "media_id": media_id,
            "source": Sources.MAL.value
            if media_type == MediaTypes.ANIME.value
            else Sources.TMDB.value,
            "media_type": media_type,
            "title": "Title",
            "image": "https://example.com/image.jpg",
            "season_number": "",
            "episode_number": "",
            "score": "",
            "progress": "",
            "status": "",
            "start_date": "",
            "end_date": "",
            "notes": "",
            "progressed_at": "",
        }
        values.update(fields)
        return ",".join(f'"{value}"' for value in values.values()) + "\n"

    def run_import(self, *rows):
        """Run a Yamtrack import in "new" mode."""
        csv = self.HEADER + "".join(rows)
        return yamtrack.importer(BytesIO(csv.encode()), self.user, "new")

    def test_new_episode_added_to_existing_show(self):
        """Only the episodes missing from an existing show are imported."""
        watched = datetime(2024, 2, 9, 12, 0, tzinfo=UTC)
        self.create_show(Status.IN_PROGRESS.value, [(1, watched)])

        rows = (
            self.row(MediaTypes.TV.value, status=Status.IN_PROGRESS.value),
            self.row(
                MediaTypes.SEASON.value,
                season_number=1,
                status=Status.IN_PROGRESS.value,
                score="8",
            ),
            self.row(
                MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=1,
                end_date="2024-02-09 12:02:00+00:00",
            ),
            self.row(
                MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=2,
                end_date="2024-02-10 12:00:00+00:00",
            ),
        )
        imported_counts, _ = self.run_import(*rows)
        self.run_import(*rows)

        self.assertEqual(imported_counts, {MediaTypes.EPISODE.value: 1})
        self.assertEqual(TV.objects.filter(user=self.user).count(), 1)
        season = Season.objects.get(user=self.user)
        self.assertEqual(season.score, 8)
        self.assertEqual(
            Episode.objects.filter(related_season=season).count(),
            2,
        )

    def test_new_season_reopens_completed_show(self):
        """A new season of a completed show is added and reopens the show."""
        self.create_show(
            Status.COMPLETED.value,
            [(1, datetime(2024, 2, 9, tzinfo=UTC))],
        )

        self.run_import(
            self.row(MediaTypes.TV.value, status=Status.IN_PROGRESS.value),
            self.row(
                MediaTypes.SEASON.value,
                season_number=2,
                status=Status.IN_PROGRESS.value,
            ),
            self.row(
                MediaTypes.EPISODE.value,
                season_number=2,
                episode_number=1,
                end_date="2024-03-01 12:00:00+00:00",
            ),
        )

        tv = TV.objects.get(user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        season = Season.objects.get(user=self.user, item__season_number=2)
        self.assertEqual(season.related_tv, tv)
        self.assertEqual(Episode.objects.filter(related_season=season).count(), 1)
        # The season with no new episodes stays completed
        self.assertEqual(
            Season.objects.get(user=self.user, item__season_number=1).status,
            Status.COMPLETED.value,
        )

    def test_movie_rewatch_is_added_once(self):
        """Only the watches of a movie missing from Yamtrack are imported."""
        Movie.objects.bulk_create(
            [
                Movie(
                    item=self.get_item(MediaTypes.MOVIE.value, "10494"),
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=datetime(2024, 2, 9, tzinfo=UTC),
                ),
            ],
        )

        rows = [
            self.row(
                MediaTypes.MOVIE.value,
                "10494",
                status=Status.COMPLETED.value,
                score="9",
                end_date=end_date,
            )
            for end_date in ("2024-02-09 00:00:00+00:00", "2024-06-01 00:00:00+00:00")
        ]
        self.run_import(*rows)
        self.run_import(*rows)

        movies = Movie.objects.filter(user=self.user)
        self.assertEqual(movies.count(), 2)
        self.assertTrue(all(movie.score == 9 for movie in movies))  # noqa: PLR2004

    def test_open_anime_is_completed(self):
        """An anime still open in Yamtrack is completed by the file."""
        Anime.objects.bulk_create(
            [
                Anime(
                    item=self.get_item(MediaTypes.ANIME.value, "1"),
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=5,
                    notes="Mine",
                ),
            ],
        )

        self.run_import(
            self.row(
                MediaTypes.ANIME.value,
                "1",
                status=Status.COMPLETED.value,
                progress="26",
                score="8",
                notes="Theirs",
                end_date="2024-02-09 00:00:00+00:00",
            ),
        )

        anime = Anime.objects.get(user=self.user)
        self.assertEqual(anime.status, Status.COMPLETED.value)
        self.assertEqual(anime.progress, 26)
        self.assertEqual(anime.score, 8)
        self.assertEqual(anime.notes, "Mine")
        self.assertEqual(anime.end_date, datetime(2024, 2, 9, tzinfo=UTC))

    @patch("integrations.imports.yamtrack.services.search")
    def test_title_only_row_matches_existing_media(self, mock_search):
        """A row without an ID is resolved before checking if it exists."""
        mock_search.return_value = {
            "results": [
                {
                    "title": "Perfect Blue",
                    "source": Sources.TMDB.value,
                    "media_id": 10494,
                    "image": "https://example.com/image.jpg",
                },
            ],
        }
        Movie.objects.bulk_create(
            [
                Movie(
                    item=self.get_item(MediaTypes.MOVIE.value, "10494"),
                    user=self.user,
                    status=Status.PLANNING.value,
                ),
            ],
        )

        self.run_import(
            self.row(
                MediaTypes.MOVIE.value,
                "",
                source="",
                title="Perfect Blue",
                image="",
                status=Status.COMPLETED.value,
                end_date="2024-02-09 00:00:00+00:00",
            ),
        )

        movie = Movie.objects.get(user=self.user)
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    def test_undated_entry_matches_dated_row(self):
        """A completed entry without end date gets the date of the same watch."""
        Movie.objects.bulk_create(
            [
                Movie(
                    item=self.get_item(MediaTypes.MOVIE.value, "10494"),
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                ),
            ],
        )

        self.run_import(
            self.row(
                MediaTypes.MOVIE.value,
                "10494",
                status=Status.COMPLETED.value,
                end_date="2024-02-09 00:00:00+00:00",
            ),
        )

        movie = Movie.objects.get(user=self.user)
        self.assertEqual(movie.end_date, datetime(2024, 2, 9, tzinfo=UTC))

    def test_dated_entry_matches_undated_row(self):
        """A completed row without end date is the same as a dated entry."""
        Anime.objects.bulk_create(
            [
                Anime(
                    item=self.get_item(MediaTypes.ANIME.value, "1"),
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=26,
                    end_date=datetime(2024, 2, 9, tzinfo=UTC),
                ),
            ],
        )

        self.run_import(
            self.row(
                MediaTypes.ANIME.value,
                "1",
                status=Status.COMPLETED.value,
                progress="26",
            ),
        )

        self.assertEqual(Anime.objects.filter(user=self.user).count(), 1)

    def test_existing_item_metadata_is_kept(self):
        """The title and image of an existing item aren't replaced by the file."""
        item = self.get_item(MediaTypes.MOVIE.value, "10494")
        Movie.objects.bulk_create(
            [Movie(item=item, user=self.user, status=Status.COMPLETED.value)],
        )

        self.run_import(
            self.row(
                MediaTypes.MOVIE.value,
                "10494",
                title="Old Title",
                image="https://example.com/old.jpg",
                status=Status.COMPLETED.value,
            ),
        )

        item.refresh_from_db()
        self.assertEqual(item.title, "Title")
        self.assertEqual(item.image, "https://example.com/image.jpg")

    @patch("integrations.imports.yamtrack.services.search")
    def test_title_only_row_overwrites_existing_media(self, mock_search):
        """In overwrite mode, a row without an ID replaces the matched media."""
        mock_search.return_value = {
            "results": [
                {
                    "title": "Perfect Blue",
                    "source": Sources.TMDB.value,
                    "media_id": 10494,
                    "image": "https://example.com/image.jpg",
                },
            ],
        }
        item = self.get_item(MediaTypes.MOVIE.value, "10494")
        Movie.objects.bulk_create(
            [
                Movie(item=item, user=self.user, status=Status.COMPLETED.value)
                for _ in range(2)
            ],
        )

        csv = self.HEADER + self.row(
            MediaTypes.MOVIE.value,
            "",
            source="",
            title="Perfect Blue",
            image="",
            status=Status.PLANNING.value,
        )
        yamtrack.importer(BytesIO(csv.encode()), self.user, "overwrite")

        movie = Movie.objects.get(user=self.user)
        self.assertEqual(movie.status, Status.PLANNING.value)


class ImportYamtrackAddedDates(TestCase):
    """Test that imported entries keep when they were first added."""

    HEADER = (
        '"media_id","source","media_type","title","image","season_number",'
        '"episode_number","score","progress","status","start_date","end_date",'
        '"notes","progressed_at","created_at"\n'
    )

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def row(self, media_type, status, created_at, season="", episode=""):
        """Return a CSV row of the export with when it was added."""
        values = [
            "1668",
            Sources.TMDB.value,
            media_type,
            "Title",
            "https://example.com/image.jpg",
            season,
            episode,
            "",
            "1" if status == Status.COMPLETED.value else "",
            status,
            "",
            "2024-02-09" if media_type == MediaTypes.EPISODE.value else "",
            "",
            "",
            created_at,
        ]
        return ",".join(f'"{value}"' for value in values) + "\n"

    def run_import(self, *rows):
        """Run a Yamtrack import in "overwrite" mode."""
        csv = self.HEADER + "".join(rows)
        return yamtrack.importer(BytesIO(csv.encode()), self.user, "overwrite")

    def test_entries_keep_their_added_dates(self):
        """A rewatched movie's entries keep their dates, the newest last."""
        self.run_import(
            self.row(
                MediaTypes.MOVIE.value,
                Status.IN_PROGRESS.value,
                "2025-03-01 10:00:00+00:00",
            ),
            self.row(
                MediaTypes.MOVIE.value,
                Status.COMPLETED.value,
                "2023-05-04 08:30:00+00:00",
            ),
        )

        dates = list(
            Movie.objects.filter(user=self.user)
            .order_by("-created_at")
            .values_list("status", "created_at"),
        )
        self.assertEqual(
            dates,
            [
                (Status.IN_PROGRESS.value, datetime(2025, 3, 1, 10, tzinfo=UTC)),
                (Status.COMPLETED.value, datetime(2023, 5, 4, 8, 30, tzinfo=UTC)),
            ],
        )

    def test_episodes_are_added_now(self):
        """Episode watches are dated by the import, a show and season by the file."""
        self.run_import(
            self.row(
                MediaTypes.TV.value,
                Status.IN_PROGRESS.value,
                "2023-05-04 08:30:00+00:00",
            ),
            self.row(
                MediaTypes.SEASON.value,
                Status.IN_PROGRESS.value,
                "2023-05-04 08:30:00+00:00",
                season="1",
            ),
            self.row(
                MediaTypes.EPISODE.value,
                "",
                "2023-05-04 08:30:00+00:00",
                season="1",
                episode="1",
            ),
        )

        added = datetime(2023, 5, 4, 8, 30, tzinfo=UTC)
        self.assertEqual(TV.objects.get(user=self.user).created_at, added)
        self.assertEqual(Season.objects.get(user=self.user).created_at, added)
        self.assertGreater(
            Episode.objects.get(related_season__user=self.user).created_at,
            added,
        )

    def test_invalid_added_date_is_left_out(self):
        """An entry whose added date can't be read is dated by the import."""
        self.run_import(
            self.row(MediaTypes.MOVIE.value, Status.COMPLETED.value, "2023-13-45"),
        )

        self.assertGreater(
            Movie.objects.get(user=self.user).created_at,
            datetime(2025, 1, 1, tzinfo=UTC),
        )


class ImportYamtrackEpisodeStatuses(TestCase):
    """Test that episode entries keep their statuses through an import."""

    HEADER = ImportYamtrackNewMode.HEADER
    setUp = ImportYamtrackNewMode.setUp
    get_item = ImportYamtrackNewMode.get_item
    create_show = ImportYamtrackNewMode.create_show
    row = ImportYamtrackNewMode.row
    run_import = ImportYamtrackNewMode.run_import

    def episode_row(self, number, status="", **fields):
        """Return a CSV row of an entry of an episode of season 1."""
        return self.row(
            MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=number,
            status=status,
            **fields,
        )

    def show_rows(self):
        """Return the CSV rows of the show and its first season."""
        return (
            self.row(MediaTypes.TV.value, status=Status.IN_PROGRESS.value),
            self.row(
                MediaTypes.SEASON.value,
                season_number=1,
                status=Status.IN_PROGRESS.value,
            ),
        )

    def entries(self):
        """Return the user's episode entries as (number, status), in order."""
        return sorted(
            Episode.objects.filter(related_season__user=self.user).values_list(
                "item__episode_number",
                "status",
            ),
        )

    def test_statuses_and_start_dates_are_imported(self):
        """Rows keep their status and start date, and those without are watches."""
        _, warnings = self.run_import(
            *self.show_rows(),
            self.episode_row(1, end_date="2024-02-09 12:00:00+00:00"),
            self.episode_row(
                2,
                Status.IN_PROGRESS.value,
                start_date="2024-02-10 12:00:00+00:00",
            ),
            self.episode_row(3, Status.PLANNING.value),
            # Not a status, so the row is left out with a warning
            self.episode_row(4, "Watched"),
        )

        self.assertEqual(
            self.entries(),
            [
                (1, Status.COMPLETED.value),
                (2, Status.IN_PROGRESS.value),
                (3, Status.PLANNING.value),
            ],
        )
        started = Episode.objects.get(item__episode_number=2)
        self.assertEqual(started.start_date, datetime(2024, 2, 10, 12, tzinfo=UTC))
        self.assertIn("Watched", warnings)

    def test_a_watch_and_a_planned_rewatch_of_an_episode_are_both_kept(self):
        """The plans imported with a watch aren't fulfilled by it."""
        self.run_import(
            *self.show_rows(),
            self.episode_row(1, end_date="2024-02-09 12:00:00+00:00"),
            self.episode_row(1, Status.PLANNING_REPEAT.value),
        )

        self.assertEqual(
            self.entries(),
            [(1, Status.COMPLETED.value), (1, Status.PLANNING_REPEAT.value)],
        )

    def test_planned_entries_are_not_watches_in_new_mode(self):
        """A planned entry already there neither hides a watch nor is added twice."""
        self.create_show(Status.IN_PROGRESS.value, [])
        season = Season.objects.get(user=self.user)
        Episode.objects.bulk_create(
            [
                Episode(
                    item=self.get_item(MediaTypes.EPISODE.value, "1668", 1, number),
                    related_season=season,
                    status=Status.PLANNING.value,
                )
                for number in (1, 2)
            ],
        )

        self.run_import(
            *self.show_rows(),
            self.episode_row(1, end_date=""),
            self.episode_row(2, Status.PLANNING.value),
        )

        # The watch fulfils the first episode's plan, and the second stays planned
        self.assertEqual(
            self.entries(),
            [(1, Status.COMPLETED.value), (2, Status.PLANNING.value)],
        )

    def test_plans_match_existing_ones_of_their_kind_in_new_mode(self):
        """A plan already there in another status, or of a watched episode, is kept."""
        self.create_show(Status.PAUSED.value, [(3, None)])
        season = Season.objects.get(user=self.user)
        Episode.objects.bulk_create(
            [
                Episode(
                    item=self.get_item(MediaTypes.EPISODE.value, "1668", 1, number),
                    related_season=season,
                    status=Status.PAUSED.value,
                )
                for number in (1, 2)
            ],
        )

        self.run_import(
            *self.show_rows(),
            *(self.episode_row(number, Status.PLANNING.value) for number in (1, 2, 3)),
            # A plan to watch the third again is of another kind
            self.episode_row(3, Status.PLANNING_REPEAT.value),
        )

        self.assertEqual(
            self.entries(),
            [
                (1, Status.PAUSED.value),
                (2, Status.PAUSED.value),
                (3, Status.COMPLETED.value),
                (3, Status.PLANNING_REPEAT.value),
            ],
        )

    def test_reimported_export_keeps_episode_statuses(self):
        """An export imported again over the same library gives the same entries."""
        self.run_import(
            *self.show_rows(),
            self.episode_row(1, end_date="2024-02-09 12:00:00+00:00"),
            self.episode_row(1, Status.PLANNING_REPEAT.value),
            self.episode_row(
                2,
                Status.PAUSED.value,
                start_date="2024-02-10 12:00:00+00:00",
            ),
            self.episode_row(3, Status.PLANNING.value),
        )
        before = sorted(
            Episode.objects.values_list("item__episode_number", "status", "start_date"),
        )
        exported = "".join(exports.generate_rows(self.user))

        yamtrack.importer(BytesIO(exported.encode()), self.user, "overwrite")

        self.assertEqual(
            sorted(
                Episode.objects.values_list(
                    "item__episode_number",
                    "status",
                    "start_date",
                ),
            ),
            before,
        )
