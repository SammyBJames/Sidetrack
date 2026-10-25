from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app import quick_actions
from app.models import TV, Anime, Game, Item, MediaTypes, Movie, Sources, Status


class QuickActionViewTests(TestCase):
    """Test the one-click action of media cards."""

    def setUp(self):
        """Create a user, log in and add a manual movie and anime."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.movie_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/movie.jpg",
        )
        self.anime_item = Item.objects.create(
            media_id="2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/anime.jpg",
        )

    def post(self, item, action, **extra):
        """Post a quick action for an item."""
        data = {
            "media_id": item.media_id,
            "source": item.source,
            "media_type": item.media_type,
            "action": action,
            **extra,
        }
        return self.client.post(reverse("quick_action"), data)

    def test_plan_untracked_media(self):
        """Planning untracked media creates a planned entry and returns its card."""
        response = self.post(self.movie_item, quick_actions.PLAN, title="Test Movie")

        movie = Movie.objects.get(item=self.movie_item, user=self.user)
        self.assertEqual(movie.status, Status.PLANNING.value)
        self.assertContains(response, "media-card")
        self.assertContains(response, "Mark watched")
        self.assertContains(response, "Test Movie added to your watchlist.")

    @patch("app.models.Item.fetch_releases")
    @patch("app.providers.services.get_media_metadata")
    def test_plan_fetches_metadata_for_new_items(self, mock_metadata, _):
        """Planning media never seen before creates its item from metadata."""
        mock_metadata.return_value = {
            "title": "Fetched Movie",
            "image": "http://example.com/fetched.jpg",
            "max_progress": 1,
        }

        self.client.post(
            reverse("quick_action"),
            {
                "media_id": "603",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "action": quick_actions.PLAN,
            },
        )

        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.item.title, "Fetched Movie")
        self.assertEqual(movie.status, Status.PLANNING.value)

    def test_complete_planned_movie(self):
        """Completing a planned movie sets its status and end date."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        self.post(self.movie_item, quick_actions.COMPLETE, instance_id=movie.id)

        movie.refresh_from_db()
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertIsNotNone(movie.end_date)

    def test_start_planned_series(self):
        """Starting planned anime sets it in progress with a start date."""
        anime = Anime.objects.create(
            item=self.anime_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        self.post(self.anime_item, quick_actions.START, instance_id=anime.id)

        anime.refresh_from_db()
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)
        self.assertIsNotNone(anime.start_date)

    def test_next_increases_progress(self):
        """The +1 action adds one to the progress."""
        anime = Anime.objects.create(
            item=self.anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=3,
        )

        self.post(self.anime_item, quick_actions.NEXT, instance_id=anime.id)

        anime.refresh_from_db()
        self.assertEqual(anime.progress, 4)

    def test_stale_action_changes_nothing(self):
        """A stale action, e.g. from a double click, changes nothing."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
        )

        self.assertEqual(response["HX-Reswap"], "none")
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)

        response = self.post(self.movie_item, quick_actions.PLAN)
        self.assertEqual(response["HX-Reswap"], "none")
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)

    def test_other_users_entry_is_not_found(self):
        """Entries of another user cannot be changed."""
        other = get_user_model().objects.create_user(
            username="other",
            password=self.credentials["password"],
        )
        movie = Movie.objects.create(
            item=self.movie_item,
            user=other,
            status=Status.PLANNING.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
        )

        self.assertEqual(response.status_code, 404)
        movie.refresh_from_db()
        self.assertEqual(movie.status, Status.PLANNING.value)

    def test_home_card_is_removed_from_its_section(self):
        """On the home page the card leaves its section after the action."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
            card="home",
        )

        self.assertEqual(response["HX-Reswap"], "delete")
        self.assertEqual(response["HX-Trigger"], "mediaChanged")

    @patch("app.models.Item.fetch_releases")
    @patch("app.providers.services.get_media_metadata")
    def test_start_planned_tv(self, mock_metadata, _):
        """Starting a planned show works although its dates come from episodes."""
        mock_metadata.return_value = {
            "title": "Test Show",
            "image": "http://example.com/show.jpg",
            "max_progress": 10,
            "related": {"seasons": []},
        }
        item = Item.objects.create(
            media_id="3",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test Show",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(item=item, user=self.user, status=Status.PLANNING.value)

        response = self.post(item, quick_actions.START, instance_id=tv.id)

        self.assertEqual(response.status_code, 200)
        tv.refresh_from_db()
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

    def test_card_leaves_filtered_list(self):
        """A card is removed from a list filtered to a status it no longer has."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
            status=Status.PLANNING.value,
        )

        self.assertEqual(response["HX-Reswap"], "delete")

    def test_card_stays_in_unfiltered_list(self):
        """Without a status filter the updated card replaces the old one."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
            status="All",
        )

        self.assertNotIn("HX-Reswap", response)
        self.assertContains(response, "media-card")

    def test_new_card_keeps_sending_the_list_filter(self):
        """The new card on a list page still sends the list's filter."""
        response = self.post(self.movie_item, quick_actions.PLAN, status="All")

        self.assertContains(response, "Mark watched")
        self.assertContains(response, 'hx-include="#filter-form"')

    def test_card_stays_in_list_filtered_to_its_new_status(self):
        """A card stays in a list filtered to the status it moves to."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
            status=Status.COMPLETED.value,
        )

        self.assertNotIn("HX-Reswap", response)
        self.assertContains(response, "media-card")

    def test_second_click_with_an_old_action_changes_nothing(self):
        """A repeated click after the entry moved on is ignored."""
        Anime.objects.create(
            item=self.anime_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        # The card still offered to plan it, but it is planned now
        response = self.post(self.anime_item, quick_actions.PLAN)

        self.assertEqual(response["HX-Reswap"], "none")
        anime = Anime.objects.get(item=self.anime_item, user=self.user)
        self.assertEqual(anime.status, Status.PLANNING.value)

    def test_resume_keeps_the_start_date(self):
        """Resuming paused media keeps the date it was first started."""
        item = Item.objects.create(
            media_id="4",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/game.jpg",
        )
        started = datetime(2024, 1, 1, tzinfo=UTC)
        game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.PAUSED.value,
            start_date=started,
        )

        self.post(item, quick_actions.RESUME, instance_id=game.id)

        game.refresh_from_db()
        self.assertEqual(game.status, Status.IN_PROGRESS.value)
        self.assertEqual(game.start_date, started)

    @override_settings(TRACK_TIME=False)
    def test_complete_without_time_tracking_uses_the_date(self):
        """Without time tracking the end date has no time of day."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        self.post(self.movie_item, quick_actions.COMPLETE, instance_id=movie.id)

        movie.refresh_from_db()
        end = timezone.localtime(movie.end_date)
        self.assertEqual((end.hour, end.minute), (0, 0))
        self.assertEqual(end.date(), timezone.localdate())

    def test_card_keeps_what_the_server_cannot_look_up(self):
        """The new card keeps the highlight of the old one."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
            active="true",
        )

        self.assertContains(response, "ring-indigo-400")
