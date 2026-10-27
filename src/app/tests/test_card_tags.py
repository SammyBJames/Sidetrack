from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from app.models import Item, MediaTypes, Sources, Status
from app.templatetags import app_tags
from events.models import Event, SentinelDatetime

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def release_at(*args):
    """Return an unsaved release at the given UTC date and time."""
    return Event(datetime=datetime(*args, tzinfo=UTC))


def date_only(year, month, day):
    """Return an unsaved release on a date without a time."""
    return release_at(
        year,
        month,
        day,
        SentinelDatetime.HOUR,
        SentinelDatetime.MINUTE,
        SentinelDatetime.SECOND,
        SentinelDatetime.MICROSECOND,
    )


@override_settings(TIME_ZONE="UTC")
@patch("django.utils.timezone.now", return_value=NOW)
class ReleaseDayTests(TestCase):
    """Test the release_day filter that words release dates on cards."""

    user = SimpleNamespace(time_format="H:i")

    def test_later_this_year_shows_month_and_day(self, _):
        """A release later this year shows the month and day only."""
        self.assertEqual(
            app_tags.release_day(date_only(2026, 10, 6), self.user), "October 6"
        )

    def test_next_year_includes_the_year(self, _):
        """A release in another year includes the year."""
        self.assertEqual(
            app_tags.release_day(date_only(2027, 3, 2), self.user),
            "March 2, 2027",
        )

    def test_today_and_tomorrow_show_the_time(self, _):
        """Releases today or tomorrow with a known time show it."""
        self.assertEqual(
            app_tags.release_day(release_at(2026, 10, 1, 21, 0), self.user),
            "Today 21:00",
        )
        self.assertEqual(
            app_tags.release_day(release_at(2026, 10, 2, 21, 0), self.user),
            "Tomorrow 21:00",
        )

    def test_placeholder_time_is_hidden(self, _):
        """Releases without a known time never show the placeholder time."""
        self.assertEqual(
            app_tags.release_day(date_only(2026, 10, 2), self.user), "Tomorrow"
        )

    def test_no_release(self, _):
        """No release gives an empty string."""
        self.assertEqual(app_tags.release_day(None, self.user), "")


class ReleaseDayTimezoneTests(TestCase):
    """Test that release days follow the local date, not the UTC one."""

    @override_settings(TIME_ZONE="America/New_York")
    @patch("django.utils.timezone.now", return_value=NOW)
    def test_evening_release_is_today_in_local_time(self, _):
        """A release late in the local evening is today, though it's tomorrow in UTC."""
        user = SimpleNamespace(time_format="H:i")

        self.assertEqual(
            app_tags.release_day(release_at(2026, 10, 2, 2, 0), user),
            "Today 22:00",
        )


@override_settings(TIME_ZONE="UTC")
@patch("django.utils.timezone.now", return_value=NOW)
class ReleaseLabelTests(TestCase):
    """Test the release_label filter that adds the episode to the day."""

    user = SimpleNamespace(time_format="H:i")

    def test_numbered_release_shows_its_number(self, _):
        """An episode release starts with its number."""
        release = release_at(2026, 10, 2, 21, 0)
        release.item = Item(media_type=MediaTypes.ANIME.value)
        release.content_number = 4

        self.assertEqual(
            app_tags.release_label(release, self.user),
            "E4 \u00b7 Tomorrow 21:00",
        )

    def test_release_without_number_is_just_the_day(self, _):
        """A movie release is only its day."""
        self.assertEqual(
            app_tags.release_label(date_only(2026, 10, 6), self.user),
            "October 6",
        )


class ReleaseYearTests(TestCase):
    """Test the release_year filter that gives movie cards their year."""

    def movie(self):
        """Return a saved movie item."""
        return Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    def test_search_results_keep_their_year(self):
        """Search results already carry their year."""
        self.assertEqual(app_tags.release_year({"release_year": 1999}), 1999)

    def test_tracked_movie_year_comes_from_its_release(self):
        """A tracked movie's year comes from its release event."""
        item = self.movie()
        Event.objects.create(item=item, datetime=datetime(1999, 3, 31, tzinfo=UTC))

        self.assertEqual(app_tags.release_year(item), 1999)

    def test_prefetched_releases_are_used(self):
        """Prefetched releases are used, skipping unknown placeholder dates."""
        item = self.movie()
        item.prefetched_events = [
            Event(datetime=SentinelDatetime.min_datetime()),
            Event(datetime=datetime(2010, 7, 16, tzinfo=UTC)),
        ]

        with self.assertNumQueries(0):
            self.assertEqual(app_tags.release_year(item), 2010)

    def test_unknown_release_date_has_no_year(self):
        """A movie whose release date is unknown has no year."""
        item = self.movie()
        Event.objects.create(item=item, datetime=SentinelDatetime.max_datetime())

        self.assertIsNone(app_tags.release_year(item))

    def test_other_media_has_no_year(self):
        """Only movies get a year from their releases."""
        item = Item(media_type=MediaTypes.ANIME.value)

        self.assertIsNone(app_tags.release_year(item))


class CardTitleTests(TestCase):
    """Test the card_title tag that splits seasons from their show's title."""

    def test_season_gets_its_own_subtitle(self):
        """A season card shows the show's title with the season below."""
        item = Item(
            title="Breaking Bad",
            media_type=MediaTypes.SEASON.value,
            season_number=2,
        )

        heading = app_tags.card_title(item, item)

        self.assertEqual(heading["title"], "Breaking Bad")
        self.assertEqual(heading["subtitle"], "Season 2")
        self.assertEqual(heading["full"], "Breaking Bad \u2013 Season 2")
        self.assertEqual(heading["short_subtitle"], "S2")

    def test_season_zero_is_specials(self):
        """Season 0 is labeled as specials."""
        item = Item(title="Lost", media_type=MediaTypes.SEASON.value, season_number=0)

        self.assertEqual(app_tags.card_title(item, item)["subtitle"], "Specials")

    def test_custom_titles_are_kept(self):
        """Cards given another title, like a season's own name, keep it."""
        item = Item(
            title="Breaking Bad",
            media_type=MediaTypes.SEASON.value,
            season_number=2,
        )

        heading = app_tags.card_title(item, "Season 2")

        self.assertEqual(heading["title"], "Season 2")
        self.assertEqual(heading["subtitle"], "")

    def test_other_media_is_unchanged(self):
        """Non-season cards keep their title."""
        item = Item(title="Inception", media_type=MediaTypes.MOVIE.value)

        self.assertEqual(app_tags.card_title(item, item)["title"], item)


class CardDetailsTests(TestCase):
    """Test the card_details tag that words the line under a card's title."""

    def details(self, item, media=None, **kwargs):
        """Return the tag's result, failing if it runs a query."""
        with self.assertNumQueries(0):
            return app_tags.card_details(item, media, **kwargs)

    def item(self, media_type, **fields):
        """Return an unsaved item of the media type."""
        return Item(title="Show", media_type=media_type, **fields)

    def media(self, **fields):
        """Return a stand-in for tracked media."""
        fields.setdefault("status", Status.IN_PROGRESS.value)
        fields.setdefault("progress", 0)
        fields.setdefault("formatted_progress", str(fields["progress"]))
        return SimpleNamespace(**fields)

    def test_started_media_shows_progress_with_its_unit(self):
        """Media in progress shows how far it is, in its long unit."""
        book = self.item(MediaTypes.BOOK.value)

        parts = self.details(book, self.media(progress=123, max_progress=368))

        self.assertEqual(
            parts["part"],
            {"kind": "progress", "text": "123 / 368", "unit": "Pages"},
        )

    def test_unit_follows_the_total_or_the_progress(self):
        """Without a total the progress is counted, and one is singular."""
        anime = self.item(MediaTypes.ANIME.value)

        one = self.details(anime, self.media(progress=1))
        several = self.details(anime, self.media(progress=4))

        self.assertEqual(one["part"]["unit"], "Episode")
        self.assertEqual(several["part"]["unit"], "Episodes")
        total = self.details(anime, self.media(progress=1, max_progress=10))
        self.assertEqual(total["part"]["unit"], "Episodes")

    def test_every_started_status_shows_progress(self):
        """Paused, dropped and repeated media show progress like media in progress."""
        manga = self.item(MediaTypes.MANGA.value)
        for status in (Status.PAUSED, Status.DROPPED, Status.REPEATING):
            with self.subTest(status=status):
                parts = self.details(manga, self.media(progress=3, status=status.value))
                self.assertEqual(parts["part"]["kind"], "progress")

    def test_unstarted_media_shows_no_progress(self):
        """Planned and finished media, and media at zero, fall through to the year."""
        book = self.item(MediaTypes.BOOK.value, release_date=date(2001, 5, 1))
        for status, progress in (
            (Status.PLANNING, 5),
            (Status.COMPLETED, 368),
            (Status.IN_PROGRESS, 0),
        ):
            with self.subTest(status=status):
                parts = self.details(
                    book,
                    self.media(progress=progress, status=status.value),
                )
                self.assertEqual(parts["part"], {"kind": "plain", "text": 2001})

    def test_progress_can_be_left_to_the_row_columns(self):
        """Rows have a column for progress, so they skip it."""
        book = self.item(MediaTypes.BOOK.value, release_date=date(2001, 5, 1))

        parts = self.details(book, self.media(progress=5), progress=False)

        self.assertEqual(parts["part"], {"kind": "plain", "text": 2001})

    def test_game_shows_time_played_except_on_home(self):
        """Games show their time played without a unit, and not on the home page."""
        game = self.item(MediaTypes.GAME.value, release_date=date(2019, 3, 1))
        media = self.media(progress=150, formatted_progress="2:30")

        elsewhere = self.details(game, media)
        home = self.details(game, media, home=True)

        self.assertEqual(
            elsewhere["part"],
            {"kind": "progress", "text": "2:30", "unit": ""},
        )
        self.assertEqual(home["part"], {"kind": "plain", "text": 2019})

    def test_season_shows_its_episode_count(self):
        """A season that hasn't started shows how many episodes it has."""
        season = self.item(MediaTypes.SEASON.value, season_number=1, length=10)

        planned = self.details(season, self.media(status=Status.PLANNING.value))
        untracked = self.details(season)
        home = self.details(
            season,
            self.media(status=Status.PLANNING.value, episode_count=8),
            home=True,
        )

        self.assertEqual(planned["part"], {"kind": "plain", "text": "10 Episodes"})
        self.assertEqual(untracked["part"], planned["part"])
        self.assertEqual(home["part"], {"kind": "plain", "text": "8 Episodes"})

    def test_season_dict_counts_from_max_progress(self):
        """Seasons of a show's page come as metadata with their episode count."""
        parts = self.details(
            {"media_type": MediaTypes.SEASON.value, "max_progress": 1},
            None,
        )

        self.assertEqual(parts["part"], {"kind": "plain", "text": "1 Episode"})

    def test_airing_season_counts_all_its_episodes_until_started(self):
        """An unstarted season counts announced episodes, a started one what's out."""
        stored = self.item(MediaTypes.SEASON.value, season_number=1, length=10)
        listed = {"media_type": MediaTypes.SEASON.value, "max_progress": 10}
        planned = self.media(status=Status.PLANNING.value, max_progress=4)
        started = self.media(progress=2, max_progress=4)

        for item in (stored, listed):
            with self.subTest(item=item):
                self.assertEqual(
                    self.details(item, planned)["part"],
                    {"kind": "plain", "text": "10 Episodes"},
                )
                self.assertEqual(
                    self.details(item, started)["part"],
                    {"kind": "progress", "text": "2 / 4", "unit": "Episodes"},
                )

    def test_tracked_show_counts_what_is_out(self):
        """A tracked show's counts are its released seasons and episodes."""
        tv = self.item(MediaTypes.TV.value, season_count=9, length=90)

        parts = self.details(
            tv,
            self.media(released_seasons=4, max_progress=44, progress=9),
        )

        self.assertEqual(
            parts["part"],
            {"kind": "tv", "seasons": 4, "episodes": 44},
        )

    def test_stored_show_counts_come_from_its_item(self):
        """A show that isn't tracked uses the counts stored on its item."""
        tv = self.item(MediaTypes.TV.value, season_count=2, length=12)

        parts = self.details(tv)

        self.assertEqual(parts["part"], {"kind": "tv", "seasons": 2, "episodes": 12})

    def test_show_without_counts_shows_its_year(self):
        """A show whose counts are unknown shows its year, or nothing."""
        tv = self.item(MediaTypes.TV.value, release_date=date(2011, 4, 17))

        self.assertEqual(
            self.details(tv)["part"],
            {"kind": "plain", "text": 2011},
        )
        self.assertEqual(self.details(self.item(MediaTypes.TV.value))["part"], None)

    def test_year_comes_from_the_release_date_or_metadata(self):
        """Any media shows the year of its release date, and dicts their year."""
        movie = self.item(MediaTypes.MOVIE.value, release_date=date(1999, 3, 31))

        self.assertEqual(self.details(movie)["part"], {"kind": "plain", "text": 1999})
        self.assertEqual(
            self.details({"media_type": "movie", "release_year": 2008})["part"],
            {"kind": "plain", "text": 2008},
        )

    def test_repeats_count_from_two(self):
        """Media repeated more than once shows its count, otherwise none."""
        movie = self.item(MediaTypes.MOVIE.value)
        movie.prefetched_events = []

        self.assertEqual(self.details(movie, self.media(repeats=3))["repeats"], 3)
        self.assertEqual(self.details(movie, self.media(repeats=1))["repeats"], 0)
        self.assertEqual(self.details(movie)["repeats"], 0)
