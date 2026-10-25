from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from app.models import Item, MediaTypes
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
