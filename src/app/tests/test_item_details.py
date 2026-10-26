from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from app import item_details
from app.models import AiringStatus, Item, MediaTypes, Sources

OMDB_RATINGS = [
    {"source": "tmdb", "score": 8.2, "scale": "/10", "votes": 100},
    {"source": "imdb", "score": "8.7", "scale": "/10", "votes": 2284373},
    {"source": "rotten_tomatoes", "score": "83%", "scale": "", "votes": None},
    {"source": "metacritic", "score": "73", "scale": "/100", "votes": None},
]


class ParsingTests(SimpleTestCase):
    """Test reading the formats providers use."""

    def test_partial_dates_start_their_period(self):
        """A year or month alone is the first day of it."""
        self.assertEqual(item_details.parse_date("2010-05-17"), date(2010, 5, 17))
        self.assertEqual(item_details.parse_date("2010-05"), date(2010, 5, 1))
        self.assertEqual(item_details.parse_date("2010"), date(2010, 1, 1))
        self.assertEqual(item_details.parse_date(1999), date(1999, 1, 1))
        self.assertEqual(item_details.parse_date("May 2001"), date(2001, 1, 1))
        self.assertEqual(item_details.parse_date("[2001]"), date(2001, 1, 1))

    def test_odd_dates_are_none(self):
        """Unreadable, impossible and empty dates are None."""
        for value in (None, "", "soon", "2010-13-01", "0", "0000"):
            with self.subTest(value=value):
                self.assertIsNone(item_details.parse_date(value))

    def test_durations_are_minutes(self):
        """Durations in every provider's wording are minutes."""
        for value, minutes in (
            ("2h 5m", 125),
            ("1h", 60),
            ("45m", 45),
            ("24 min", 24),
            ("1h 30m", 90),
        ):
            with self.subTest(value=value):
                self.assertEqual(item_details.parse_minutes(value), minutes)

    def test_odd_durations_are_none(self):
        """Unknown durations are None."""
        for value in (None, "", "0m", "soon", 90):
            with self.subTest(value=value):
                self.assertIsNone(item_details.parse_minutes(value))

    def test_players(self):
        """A range or a single count gives the fewest and most players."""
        self.assertEqual(item_details.parse_players("2-4 players"), (2, 4))
        self.assertEqual(item_details.parse_players("3 players"), (3, 3))
        self.assertEqual(item_details.parse_players(None), (None, None))
        self.assertEqual(item_details.parse_players("many"), (None, None))
        self.assertEqual(item_details.parse_players("0 players"), (None, None))


class DetailsFromMetadataTests(SimpleTestCase):
    """Test the columns each provider's metadata gives."""

    def details(self, media_type, source, metadata, ratings=None):
        """Return the details of metadata."""
        return item_details.details_from_metadata(
            media_type,
            source,
            metadata,
            ratings,
        )

    def test_tmdb_movie(self):
        """A movie's date, runtime, scores and genres are read."""
        metadata = {
            "genres": ["Drama", "Science Fiction"],
            "score": 8.2,
            "score_count": 25000,
            "details": {
                "release_date": "1999-03-31",
                "status": "Released",
                "runtime": "2h 16m",
            },
        }

        self.assertEqual(
            self.details(
                MediaTypes.MOVIE.value,
                Sources.TMDB.value,
                metadata,
                OMDB_RATINGS,
            ),
            {
                "release_date": date(1999, 3, 31),
                "runtime": 136,
                "provider_score": Decimal("8.2"),
                "provider_score_count": 25000,
                "genres": "|Drama|Science Fiction|",
                "imdb_score": Decimal("8.7"),
                "rotten_tomatoes": 83,
                "metacritic": 73,
            },
        )

    def test_tmdb_tv(self):
        """A show's episodes, seasons, runtime and airing status are read."""
        metadata = {
            "genres": None,
            "score": 0.0,
            "score_count": 0,
            "details": {
                "first_air_date": "2008-01-20",
                "status": "Returning Series",
                "seasons": 5,
                "episodes": 62,
                "runtime": "47m",
            },
        }

        self.assertEqual(
            self.details(MediaTypes.TV.value, Sources.TMDB.value, metadata),
            {
                "release_date": date(2008, 1, 20),
                "runtime": 47,
                "length": 62,
                "season_count": 5,
                "airing_status": AiringStatus.AIRING,
            },
        )

    def test_tmdb_season(self):
        """A season's date, episodes and average runtime are read."""
        metadata = {
            "genres": ["Drama"],
            "score": 8.9,
            "score_count": 40,
            "details": {
                "first_air_date": "2009-03-08",
                "last_air_date": "2009-05-31",
                "episodes": 13,
                "runtime": "1h 2m",
            },
        }

        self.assertEqual(
            self.details(MediaTypes.SEASON.value, Sources.TMDB.value, metadata),
            {
                "release_date": date(2009, 3, 8),
                "runtime": 62,
                "length": 13,
                "provider_score": Decimal("8.9"),
                "provider_score_count": 40,
                "genres": "|Drama|",
            },
        )

    def test_mal_anime(self):
        """An anime's partial date, episodes and status are read."""
        metadata = {
            "genres": ["Action", "Fantasy"],
            "score": 8.4,
            "score_count": 1000,
            "details": {
                "start_date": "2011-04",
                "end_date": None,
                "status": "Finished",
                "episodes": 24,
                "runtime": "24 min",
            },
        }

        self.assertEqual(
            self.details(MediaTypes.ANIME.value, Sources.MAL.value, metadata),
            {
                "release_date": date(2011, 4, 1),
                "runtime": 24,
                "length": 24,
                "airing_status": AiringStatus.ENDED,
                "provider_score": Decimal("8.4"),
                "provider_score_count": 1000,
                "genres": "|Action|Fantasy|",
            },
        )

    def test_mal_manga(self):
        """A manga's year, chapters and publishing status are read."""
        metadata = {
            "score": None,
            "score_count": 0,
            "details": {
                "start_date": "1997",
                "status": "Publishing",
                "number_of_chapters": 1100,
            },
        }

        self.assertEqual(
            self.details(MediaTypes.MANGA.value, Sources.MAL.value, metadata),
            {
                "release_date": date(1997, 1, 1),
                "length": 1100,
                "airing_status": AiringStatus.AIRING,
            },
        )

    def test_mangaupdates_manga(self):
        """MangaUpdates' year, status text and completed chapter count are read."""
        metadata = {
            "max_progress": 200,
            "genres": ["Action"],
            "score": 8.1,
            "score_count": 500,
            "details": {
                "year": "2004",
                "status_in_country_of_origin": "12 Volumes (Complete)",
            },
        }

        self.assertEqual(
            self.details(
                MediaTypes.MANGA.value,
                Sources.MANGAUPDATES.value,
                metadata,
            ),
            {
                "release_date": date(2004, 1, 1),
                "length": 200,
                "airing_status": AiringStatus.ENDED,
                "provider_score": Decimal("8.1"),
                "provider_score_count": 500,
                "genres": "|Action|",
            },
        )

    def test_mangaupdates_ongoing_without_chapters(self):
        """An ongoing manga has no chapter count and is airing."""
        metadata = {
            "max_progress": None,
            "details": {"year": "2020", "status_in_country_of_origin": "Ongoing"},
        }

        self.assertEqual(
            self.details(
                MediaTypes.MANGA.value,
                Sources.MANGAUPDATES.value,
                metadata,
            ),
            {
                "release_date": date(2020, 1, 1),
                "airing_status": AiringStatus.AIRING,
            },
        )

    def test_igdb_game(self):
        """A game's date, time to beat in minutes and platforms are read."""
        metadata = {
            "genres": ["Role-playing (RPG)", "Adventure"],
            "score": 9.1,
            "score_count": 2000,
            "details": {
                "release_date": "2015-05-18",
                "platforms": ["PC", "PlayStation 4"],
            },
            "time_to_beat": {"hastily": 100000, "normally": 180000},
        }

        self.assertEqual(
            self.details(MediaTypes.GAME.value, Sources.IGDB.value, metadata),
            {
                "release_date": date(2015, 5, 18),
                "provider_score": Decimal("9.1"),
                "provider_score_count": 2000,
                "genres": "|Role-playing (RPG)|Adventure|",
                "time_to_beat": 3000,
                "platforms": "|PC|PlayStation 4|",
            },
        )

    def test_hardcover_book(self):
        """A book's pages and publish date are read."""
        metadata = {
            "genres": ["Fantasy"],
            "score": 8.0,
            "score_count": 10,
            "details": {"number_of_pages": 412, "publish_date": "2001-02-03"},
        }

        self.assertEqual(
            self.details(MediaTypes.BOOK.value, Sources.HARDCOVER.value, metadata),
            {
                "release_date": date(2001, 2, 3),
                "length": 412,
                "provider_score": Decimal("8.0"),
                "provider_score_count": 10,
                "genres": "|Fantasy|",
            },
        )

    def test_openlibrary_book_with_unparsed_date(self):
        """A publish date Open Library couldn't parse leaves the date unset."""
        metadata = {
            "details": {"number_of_pages": 300, "publish_date": "Spring, 1999"},
        }

        self.assertEqual(
            self.details(MediaTypes.BOOK.value, Sources.OPENLIBRARY.value, metadata),
            {"release_date": date(1999, 1, 1), "length": 300},
        )

    def test_comicvine_comic(self):
        """A comic's start year and issues are read."""
        metadata = {
            "genres": None,
            "score": None,
            "score_count": None,
            "details": {"start_date": "2012", "issues_count": 52},
        }

        self.assertEqual(
            self.details(MediaTypes.COMIC.value, Sources.COMICVINE.value, metadata),
            {"release_date": date(2012, 1, 1), "length": 52},
        )

    def test_bgg_boardgame(self):
        """A board game's year, players and play time are read."""
        metadata = {
            "genres": ["Economic"],
            "score": 7.9,
            "score_count": 90000,
            "details": {"year": "2017", "players": "2-4 players", "playtime": "60 min"},
        }

        self.assertEqual(
            self.details(MediaTypes.BOARDGAME.value, Sources.BGG.value, metadata),
            {
                "release_date": date(2017, 1, 1),
                "runtime": 60,
                "min_players": 2,
                "max_players": 4,
                "provider_score": Decimal("7.9"),
                "provider_score_count": 90000,
                "genres": "|Economic|",
            },
        )

    def test_manual_item_has_no_details(self):
        """A manual item's empty metadata gives nothing."""
        metadata = {"score": None, "score_count": None, "details": {}}

        self.assertEqual(
            self.details(MediaTypes.MOVIE.value, Sources.MANUAL.value, metadata),
            {},
        )

    def test_odd_values_are_left_out(self):
        """Values of the wrong type, range or format don't fail or get stored."""
        metadata = {
            "genres": ["", "|"],
            "score": "n/a",
            "score_count": "many",
            "details": {
                "release_date": "soon",
                "runtime": "a while",
                "status": "Released",
            },
        }
        ratings = [
            {"source": "imdb", "score": "N/A"},
            {"source": "metacritic", "score": "150"},
            {"source": "unknown", "score": "5"},
        ]

        self.assertEqual(
            self.details(
                MediaTypes.MOVIE.value,
                Sources.TMDB.value,
                metadata,
                ratings,
            ),
            {},
        )

    def test_movies_have_no_airing_status(self):
        """A movie's production status isn't an airing status."""
        found = self.details(
            MediaTypes.MOVIE.value,
            Sources.TMDB.value,
            {"details": {"status": "In Production"}},
        )

        self.assertNotIn("airing_status", found)

    def test_airing_status_wording(self):
        """Each provider's status wording maps to a status or none."""
        for status, expected in (
            ("Ended", AiringStatus.ENDED),
            ("Canceled", AiringStatus.ENDED),
            ("In Production", AiringStatus.UPCOMING),
            ("Upcoming", AiringStatus.UPCOMING),
            ("On Hiatus", AiringStatus.AIRING),
            ("Something else", None),
        ):
            with self.subTest(status=status):
                found = self.details(
                    MediaTypes.TV.value,
                    Sources.TMDB.value,
                    {"details": {"status": status}},
                )
                self.assertEqual(found.get("airing_status"), expected)


class SaveItemDetailsTests(TestCase):
    """Test storing details on an item."""

    def setUp(self):
        """Create a movie item."""
        self.item = Item.objects.create(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Matrix",
            image="http://example.com/image.jpg",
        )
        self.metadata = {
            "score": 8.2,
            "score_count": 100,
            "details": {"release_date": "1999-03-31", "runtime": "2h 16m"},
        }

    def test_saves_details_and_the_time(self):
        """The details and when they were stored are saved."""
        self.assertTrue(item_details.save_item_details(self.item, self.metadata))

        self.item.refresh_from_db()
        self.assertEqual(self.item.release_date, date(1999, 3, 31))
        self.assertEqual(self.item.runtime, 136)
        self.assertEqual(self.item.provider_score, Decimal("8.2"))
        self.assertIsNotNone(self.item.details_updated_at)

    def test_unchanged_details_are_not_saved_again(self):
        """Saving the same details again writes nothing."""
        item_details.save_item_details(self.item, self.metadata)
        stored_at = Item.objects.get(pk=self.item.pk).details_updated_at

        with self.assertNumQueries(0):
            saved = item_details.save_item_details(self.item, self.metadata)

        self.assertFalse(saved)
        self.item.refresh_from_db()
        self.assertEqual(self.item.details_updated_at, stored_at)

    def test_touch_stamps_unchanged_details(self):
        """A refresh stamps the time even when nothing changed."""
        item_details.save_item_details(self.item, self.metadata)
        stored_at = Item.objects.get(pk=self.item.pk).details_updated_at

        self.assertTrue(
            item_details.save_item_details(self.item, self.metadata, touch=True),
        )

        self.item.refresh_from_db()
        self.assertGreater(self.item.details_updated_at, stored_at)

    def test_missing_values_keep_stored_ones(self):
        """A provider leaving a value out doesn't erase what was stored."""
        item_details.save_item_details(self.item, self.metadata)

        item_details.save_item_details(self.item, {"details": {}})

        self.item.refresh_from_db()
        self.assertEqual(self.item.runtime, 136)

    def test_empty_metadata_still_stamps_the_time(self):
        """An item with no known details is marked as looked at."""
        item_details.save_item_details(self.item, {"details": {}})

        self.item.refresh_from_db()
        self.assertIsNotNone(self.item.details_updated_at)
        self.assertIsNone(self.item.runtime)
        self.assertEqual(self.item.genres, "")

    @patch("app.item_details.services.get_media_metadata")
    @patch("app.item_details.helpers.get_media_ratings", return_value=OMDB_RATINGS)
    def test_refresh_fetches_metadata_and_ratings(self, mock_ratings, mock_metadata):
        """Without metadata, a refresh fetches it and adds the ratings."""
        mock_metadata.return_value = self.metadata

        item_details.refresh_item_details(self.item)

        mock_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "603",
            Sources.TMDB.value,
        )
        mock_ratings.assert_called_once_with(self.metadata)
        self.item.refresh_from_db()
        self.assertEqual(self.item.imdb_score, Decimal("8.7"))
        self.assertEqual(self.item.metacritic, 73)

    @patch("app.item_details.services.get_media_metadata")
    @patch("app.item_details.helpers.get_media_ratings", return_value=[])
    def test_refresh_reads_a_season_from_its_show(self, mock_ratings, mock_metadata):
        """A season's details come from the season part of its show's metadata."""
        season = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=2,
        )
        mock_metadata.return_value = {"season/2": {"details": {"episodes": 24}}}

        item_details.refresh_item_details(season)

        mock_metadata.assert_called_once_with(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [2],
        )
        season.refresh_from_db()
        self.assertEqual(season.length, 24)
        self.assertEqual(mock_ratings.call_count, 1)

    def test_refresh_stamps_unchanged_details(self):
        """A refresh moves the time forward even when nothing changed."""
        item_details.save_item_details(self.item, self.metadata)
        stored_at = Item.objects.get(pk=self.item.pk).details_updated_at

        with patch("app.item_details.helpers.get_media_ratings", return_value=[]):
            item_details.refresh_item_details(self.item, self.metadata)

        self.item.refresh_from_db()
        self.assertGreater(self.item.details_updated_at, stored_at)

    @patch("app.item_details.services.get_media_metadata")
    def test_refresh_uses_given_metadata(self, mock_metadata):
        """Given metadata isn't fetched again."""
        with patch("app.item_details.helpers.get_media_ratings", return_value=[]):
            item_details.refresh_item_details(self.item, self.metadata)

        mock_metadata.assert_not_called()
