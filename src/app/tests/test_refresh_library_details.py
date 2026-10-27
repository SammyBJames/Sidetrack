from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from app import item_details
from app.models import (
    Episode,
    Game,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.providers import services
from app.tasks import refresh_library_details

METADATA = {"score": 7.5, "score_count": 10, "details": {"runtime": "1h 30m"}}


class RefreshLibraryDetailsTests(TestCase):
    """Test the refresh of library items' details."""

    def setUp(self):
        """Create a user and patch the provider calls."""
        self.user = get_user_model().objects.create_user(username="test")
        patcher = patch("app.item_details.services.get_media_metadata")
        self.get_metadata = patcher.start()
        self.get_metadata.return_value = METADATA
        self.addCleanup(patcher.stop)
        patcher = patch("app.item_details.helpers.get_media_ratings", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps = 0

    def movie(self, media_id, *, tracked=True, updated_days_ago=None):
        """Create a movie item, tracked by the user, and return it."""
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=f"Movie {media_id}",
            image="http://example.com/image.jpg",
            details_updated_at=(
                timezone.now() - timedelta(days=updated_days_ago)
                if updated_days_ago is not None
                else None
            ),
        )
        if tracked:
            Movie.objects.create(
                item=item, user=self.user, status=Status.PLANNING.value
            )
        return item

    def tracked_episode(self):
        """Create an episode someone watched, which has an entry but isn't library."""
        season_item = Item.objects.create(
            media_id="show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Show",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        # Tracking a season also tracks its show, which reads its metadata
        self.get_metadata.return_value = {
            "title": "Show",
            "image": "http://example.com/image.jpg",
            "details": {"seasons": 1},
        }
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        episode_item = Item.objects.create(
            media_id="show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Show",
            image="http://example.com/image.jpg",
            season_number=1,
            episode_number=1,
        )
        # bulk_create skips the save that reads the season's metadata
        Episode.objects.bulk_create([Episode(item=episode_item, related_season=season)])
        # The season and its show are library items, tracked or not
        Item.objects.filter(media_id="show").exclude(pk=episode_item.pk).update(
            details_updated_at=timezone.now(),
        )
        self.get_metadata.reset_mock()
        self.get_metadata.return_value = METADATA

    def rate_limited(self):
        """Raise the error of a real 429 asking for a two hour wait.

        The request goes through `services.api_request`, and its sleeps are
        counted in `self.sleeps`.
        """
        response = MagicMock(status_code=429, headers={"Retry-After": "7623"})
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "429",
            response=response,
        )
        with (
            patch("app.providers.services.session.get", return_value=response),
            patch("app.providers.services.time.sleep") as sleep,
        ):
            try:
                services.api_request("TMDB", "GET", "https://example.com")
            except requests.exceptions.HTTPError as error:
                self.sleeps += sleep.call_count
                raise services.ProviderAPIError(Sources.TMDB.value, error) from None

    def fetched_ids(self):
        """Return the media IDs the providers were asked for, in order."""
        return [call.args[1] for call in self.get_metadata.call_args_list]

    def test_only_library_items_that_need_it_by_age(self):
        """Untracked, episode and fresh items are skipped, the rest go by age."""
        self.movie("never")
        self.movie("old", updated_days_ago=30)
        self.movie("older", updated_days_ago=60)
        self.movie("fresh", updated_days_ago=2)
        self.movie("untracked", tracked=False)
        self.tracked_episode()

        result = item_details.refresh_library_details()

        self.assertEqual(result, (3, 0))
        self.assertEqual(self.fetched_ids(), ["never", "older", "old"])

    def test_limit_keeps_never_fetched_first(self):
        """With a budget, never-fetched items are taken before old ones."""
        self.movie("old", updated_days_ago=30)
        self.movie("never-1")
        self.movie("never-2")

        item_details.refresh_library_details(2)

        self.assertCountEqual(self.fetched_ids(), ["never-1", "never-2"])

    def test_refreshed_items_have_their_details(self):
        """A refreshed item stores its details and is fresh afterwards."""
        item = self.movie("1")

        item_details.refresh_library_details()
        item.refresh_from_db()

        self.assertEqual(item.runtime, 90)
        self.assertEqual(item_details.refresh_library_details(), (0, 0))

    def test_season_is_refreshed_by_its_own_item(self):
        """A tracked season is a library item, fetched from its show's metadata."""
        item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=2,
        )
        # Tracking a season also tracks its show, which reads its metadata
        self.get_metadata.return_value = {
            "title": "Friends",
            "image": "http://example.com/image.jpg",
            "details": {"seasons": 10},
        }
        Season.objects.create(item=item, user=self.user, status=Status.PLANNING.value)
        self.get_metadata.reset_mock()
        self.get_metadata.return_value = {"season/2": {"details": {"episodes": 24}}}

        item_details.refresh_library_details()

        item.refresh_from_db()
        self.assertEqual(item.length, 24)
        self.get_metadata.assert_any_call(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [2],
        )

    def test_failing_provider_skips_the_item_and_continues(self):
        """A provider error is logged, the item is stamped and the run goes on."""
        failing = self.movie("failing")
        self.movie("working")

        url = "https://api.themoviedb.org/3/movie/failing?api_key=SECRETKEY"

        def metadata(_media_type, media_id, _source):
            """Raise for the "failing" item, else return the usual metadata."""
            if media_id == "failing":
                error = requests.exceptions.HTTPError(f"404 for url: {url}")
                raise services.ProviderAPIError(Sources.TMDB.value, error) from error
            return METADATA

        self.get_metadata.side_effect = metadata

        with self.assertLogs("app.item_details", "WARNING") as logs:
            result = item_details.refresh_library_details()

        self.assertEqual(result, (1, 1))
        # One line naming the item and the error, without the URL and its key
        line = f"Failed to refresh the details of {failing}: ProviderAPIError"
        self.assertEqual(logs.output, [f"WARNING:app.item_details:{line}"])
        self.assertIsNone(logs.records[0].exc_info)
        failing.refresh_from_db()
        self.assertIsNotNone(failing.details_updated_at)
        self.assertIsNone(failing.runtime)
        self.assertEqual(Item.objects.get(media_id="working").runtime, 90)

    def test_any_error_skips_the_item_and_stamps_it(self):
        """Odd metadata that raises doesn't stop the run or leave the item unstamped."""
        odd = self.movie("odd")
        self.movie("working")

        def metadata(_media_type, media_id, _source):
            """Raise for the "odd" item and return the usual metadata for the rest."""
            if media_id == "odd":
                raise KeyError(media_id)
            return METADATA

        self.get_metadata.side_effect = metadata

        with self.assertLogs("app.item_details", "ERROR"):
            result = item_details.refresh_library_details()

        self.assertEqual(result, (1, 1))
        odd.refresh_from_db()
        working = Item.objects.get(media_id="working")
        self.assertIsNotNone(odd.details_updated_at)
        self.assertIsNotNone(working.details_updated_at)
        self.assertEqual(working.runtime, 90)

    def test_request_errors_are_skipped_too(self):
        """A network error skips the item like an API error."""
        self.movie("1")
        self.get_metadata.side_effect = requests.exceptions.ConnectionError

        with self.assertLogs("app.item_details", "WARNING"):
            self.assertEqual(item_details.refresh_library_details(), (0, 1))

    def test_rate_limited_provider_is_not_stamped_and_skipped(self):
        """A wait over the cap leaves the item for next run and skips its provider."""
        limited = self.movie("limited")
        self.movie("limited-2")
        other = Item.objects.create(
            media_id="game",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Game",
            image="http://example.com/image.jpg",
        )
        Game.objects.create(item=other, user=self.user, status=Status.PLANNING.value)

        def metadata(_media_type, media_id, _source):
            """Go over the cap for the first TMDB title, as a 429 would."""
            if media_id == "limited":
                self.rate_limited()
            return METADATA

        self.get_metadata.side_effect = metadata

        with self.assertLogs("app.item_details", "WARNING"):
            result = item_details.refresh_library_details()

        # One failure, the other TMDB title never asked for, the game refreshed
        self.assertEqual(result, (1, 1))
        self.assertEqual(self.sleeps, 0)
        self.assertEqual(self.fetched_ids(), ["limited", "game"])
        limited.refresh_from_db()
        self.assertIsNone(limited.details_updated_at)
        self.assertIsNone(Item.objects.get(media_id="limited-2").details_updated_at)
        other.refresh_from_db()
        self.assertIsNotNone(other.details_updated_at)

    def test_rate_limited_provider_does_not_use_up_the_budget(self):
        """The items skipped for a limited provider leave the budget to the others."""
        for media_id in ("limited", "skipped-1", "skipped-2"):
            self.movie(media_id)
        for media_id in ("a", "b"):
            item = Item.objects.create(
                media_id=media_id,
                source=Sources.IGDB.value,
                media_type=MediaTypes.GAME.value,
                title=media_id,
                image="http://example.com/image.jpg",
            )
            Game.objects.create(
                item=item,
                user=self.user,
                status=Status.PLANNING.value,
            )

        def metadata(_media_type, media_id, _source):
            """Go over the cap for the first TMDB title."""
            if media_id == "limited":
                self.rate_limited()
            return METADATA

        self.get_metadata.side_effect = metadata

        with self.assertLogs("app.item_details", "WARNING"):
            result = item_details.refresh_library_details(3)

        self.assertEqual(result, (2, 1))
        self.assertEqual(self.fetched_ids(), ["limited", "a", "b"])

    @override_settings(LIBRARY_DETAILS_BATCH=2)
    def test_task_stops_at_the_budget(self):
        """The task refreshes only as many items as the setting allows."""
        for media_id in "abc":
            self.movie(media_id)

        self.assertEqual(refresh_library_details(), 2)

    @override_settings(LIBRARY_DETAILS_BATCH=1)
    def test_command_all_ignores_the_budget(self):
        """The command stops at the budget, except with --all."""
        for media_id in "abc":
            self.movie(media_id)

        out = StringIO()
        call_command("refresh_library_details", stdout=out)
        self.assertIn("Refreshed 1 items", out.getvalue())

        out = StringIO()
        call_command("refresh_library_details", "--all", stdout=out)
        self.assertIn("Refreshed 2 items", out.getvalue())
