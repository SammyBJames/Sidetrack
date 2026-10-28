from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app import quick_actions
from app.models import (
    TV,
    Anime,
    Book,
    Game,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.templatetags import app_tags


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
            home_status=Status.PLANNING.value,
        )

        self.assertEqual(response["HX-Reswap"], "delete")
        self.assertEqual(response["HX-Trigger"], "mediaChanged")
        self.assertEqual(response["HX-Retarget"], f"#home-media-movie-{movie.id}")

    def test_details_page_reloads(self):
        """On a details page the action reloads the page, keeping its message."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.client.post(
            f"{reverse('quick_action')}?next=/details/page",
            {
                "media_id": self.movie_item.media_id,
                "source": self.movie_item.source,
                "media_type": self.movie_item.media_type,
                "action": quick_actions.COMPLETE,
                "instance_id": movie.id,
                "card": "details",
            },
            headers={"HX-Request": "true"},
        )

        self.assertNotIn("HX-Refresh", response)
        self.assertIn('"/details/page"', response["HX-Location"])
        movie.refresh_from_db()
        self.assertEqual(movie.status, Status.COMPLETED.value)
        # Shown by the reloaded page, so not consumed by this response
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Test Movie marked as watched."],
        )

    @patch("app.models.Item.fetch_releases")
    @patch("app.providers.services.get_media_metadata")
    def test_start_planned_tv(self, mock_metadata, _):
        """A planned show with no seasons to plan is started instead."""
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

        response = self.post(item, quick_actions.NEXT, instance_id=tv.id)

        self.assertEqual(response.status_code, 200)
        tv.refresh_from_db()
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

    @patch("app.models.Item.fetch_releases")
    @patch("app.providers.services.get_media_metadata")
    def test_watchlist_tv_plans_its_seasons(self, mock_metadata, _):
        """Putting a show on the watchlist plans its seasons, but not specials."""
        mock_metadata.return_value = {
            "title": "Test Show",
            "image": "http://example.com/show.jpg",
            "max_progress": 10,
            "related": {
                "seasons": [
                    {"season_number": number, "image": "http://example.com/s.jpg"}
                    for number in (0, 1, 2)
                ],
            },
            # Its seasons, when they're asked for
            **{f"season/{number}": {"episodes": []} for number in (1, 2)},
        }
        item = Item.objects.create(
            media_id="3",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test Show",
            image="http://example.com/show.jpg",
        )

        self.post(item, quick_actions.PLAN, title="Test Show")

        tv = TV.objects.get(item=item, user=self.user)
        self.assertEqual(
            sorted(
                (season.item.season_number, season.status)
                for season in tv.seasons.select_related("item")
            ),
            [(1, Status.PLANNING.value), (2, Status.PLANNING.value)],
        )

    @patch("app.models.Item.fetch_releases")
    @patch("app.providers.services.get_media_metadata")
    def test_show_planned_without_seasons_watches_its_first_episode(
        self,
        mock_metadata,
        _,
    ):
        """A show planned before its seasons were starts with its first episode."""
        mock_metadata.return_value = {
            "title": "Test Show",
            "image": "http://example.com/show.jpg",
            "max_progress": 4,
            "related": {
                "seasons": [
                    {"season_number": number, "image": "http://example.com/s.jpg"}
                    for number in (0, 1, 2)
                ],
            },
            # Each season, when one is asked for
            "season/1": {"episodes": [{"episode_number": 1}, {"episode_number": 2}]},
            "season/2": {"episodes": []},
            "episodes": [{"episode_number": 1}, {"episode_number": 2}],
        }
        item = Item.objects.create(
            media_id="3",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test Show",
            image="http://example.com/show.jpg",
        )
        tv = TV(item=item, user=self.user, status=Status.PLANNING.value)
        TV.save_base(tv)

        action = quick_actions.get_quick_action(MediaTypes.TV.value, tv)
        self.assertEqual(
            (action.key, action.label),
            (quick_actions.NEXT, "Mark S1E1 watched"),
        )

        self.post(item, quick_actions.NEXT, instance_id=tv.id)

        tv.refresh_from_db()
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        season = tv.seasons.get(item__season_number=1)
        self.assertEqual(season.status, Status.IN_PROGRESS.value)
        # The planned second episode is still to watch
        self.assertEqual(
            list(season.episodes.values_list("item__episode_number", "status")),
            [(1, Status.COMPLETED.value), (2, Status.PLANNING.value)],
        )
        self.assertEqual(
            tv.seasons.get(item__season_number=2).status,
            Status.PLANNING.value,
        )

    @patch("app.models.Item.fetch_releases")
    @patch("app.providers.services.get_media_metadata")
    def test_tracking_form_watchlist_plans_seasons(self, mock_metadata, _):
        """Adding a show as planned through the tracking form plans its seasons."""
        mock_metadata.return_value = {
            "title": "Test Show",
            "image": "http://example.com/show.jpg",
            "max_progress": 10,
            "related": {
                "seasons": [{"season_number": 1, "image": "http://example.com/s.jpg"}],
            },
            # Its season, when it's asked for
            "season/1": {"episodes": []},
        }

        self.client.post(
            reverse("media_save"),
            {
                "media_id": "3",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "status": Status.PLANNING.value,
            },
        )

        tv = TV.objects.get(item__media_id="3", user=self.user)
        self.assertEqual(
            list(tv.seasons.values_list("status", flat=True)),
            [Status.PLANNING.value],
        )

    def show_with_seasons(self, status, season_statuses):
        """Track a show with seasons of the given statuses, without their cascades.

        Returns the show, and stubs its metadata with those seasons and one more.
        """
        item = Item.objects.create(
            media_id="3",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test Show",
            image="http://example.com/show.jpg",
        )
        tv = TV(item=item, user=self.user, status=status)
        TV.save_base(tv)
        for number, season_status in enumerate(season_statuses, start=1):
            season_item = Item.objects.create(
                media_id="3",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Test Show",
                image="http://example.com/s.jpg",
                season_number=number,
            )
            Season.save_base(
                Season(
                    item=season_item,
                    user=self.user,
                    related_tv=tv,
                    status=season_status,
                ),
            )
        return tv

    def season_statuses(self, tv):
        """Return the show's seasons' statuses by number."""
        return {
            season.item.season_number: season.status
            for season in tv.seasons.select_related("item")
        }

    @patch("app.models.Item.fetch_releases")
    @patch("app.providers.services.get_media_metadata")
    def test_tracking_form_watchlist_leaves_dropped_seasons(self, mock_metadata, _):
        """A show put back on the watchlist plans its seasons, not dropped ones."""
        tv = self.show_with_seasons(
            Status.IN_PROGRESS.value,
            [Status.IN_PROGRESS.value, Status.DROPPED.value],
        )
        mock_metadata.return_value = {
            "related": {
                "seasons": [
                    {"season_number": number, "image": "http://example.com/s.jpg"}
                    for number in (1, 2, 3)
                ],
            },
            # Its seasons, when they're asked for
            **{f"season/{number}": {"episodes": []} for number in (1, 3)},
        }

        self.client.post(
            reverse("media_save"),
            {
                "instance_id": tv.id,
                "media_id": "3",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "status": Status.PLANNING.value,
            },
        )

        self.assertEqual(
            self.season_statuses(tv),
            {
                1: Status.PLANNING.value,
                2: Status.DROPPED.value,
                3: Status.PLANNING.value,
            },
        )

    @patch("app.models.Item.fetch_releases")
    @patch("app.models.TV.plan_seasons")
    def test_resaving_a_planned_show_leaves_its_seasons(self, mock_plan, _):
        """Only a show newly put on the watchlist plans its seasons."""
        tv = self.show_with_seasons(Status.PLANNING.value, [Status.IN_PROGRESS.value])

        self.client.post(
            reverse("media_save"),
            {
                "instance_id": tv.id,
                "media_id": "3",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "status": Status.PLANNING.value,
                "notes": "Someday",
            },
        )

        mock_plan.assert_not_called()
        tv.refresh_from_db()
        self.assertEqual(tv.notes, "Someday")

    @patch("app.models.Item.fetch_releases")
    @patch("app.providers.services.get_media_metadata")
    def test_watchlist_is_undone_when_seasons_cant_be_planned(self, mock_metadata, _):
        """A show isn't left planned without its seasons, so trying again works."""
        item = Item.objects.create(
            media_id="3",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test Show",
            image="http://example.com/show.jpg",
        )
        mock_metadata.side_effect = RuntimeError("TMDB is down")

        with self.assertRaises(RuntimeError):
            self.post(item, quick_actions.PLAN, title="Test Show")

        self.assertFalse(TV.objects.filter(item=item, user=self.user).exists())

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

    def test_list_row_is_returned_as_a_row(self):
        """A list row gets its updated row back, dated by the list's sort."""
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
            sort="added",
            card="row",
        )

        self.assertTemplateUsed(response, "app/components/media_row.html")
        self.assertNotContains(response, "media-card")
        self.assertEqual(response.context["sorted_field"].label, "Date added")
        self.assertEqual(
            response.context["media"].sorted_value,
            app_tags.date_format(movie.created_at, self.user),
        )
        # The list's status counts change with it
        self.assertEqual(response["HX-Trigger"], "libraryChanged")

    def test_search_row_is_returned_as_a_row_without_the_library_columns(self):
        """A row from a search, for a result not tracked, gets its row back."""
        response = self.post(
            self.movie_item,
            quick_actions.PLAN,
            card="row",
            title="Test Movie",
        )

        self.assertTemplateUsed(response, "app/components/media_row.html")
        self.assertNotContains(response, "media-card")
        self.assertContains(response, "Test Movie")
        self.assertContains(response, "Planning")
        self.assertContains(response, '"action": "complete"')
        self.assertNotContains(response, "filter-form")
        self.assertNotIn("sorted_field", response.context)
        self.assertNotIn("HX-Trigger", response)

    def test_card_stays_caught_up_while_in_progress(self):
        """A list of caught up media keeps an entry still in progress."""
        anime = Anime.objects.create(
            item=self.anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=1,
        )

        response = self.post(
            self.anime_item,
            quick_actions.NEXT,
            instance_id=anime.id,
            status="caught_up",
        )

        self.assertNotIn("HX-Reswap", response)

    def test_card_stays_caught_up_while_repeating(self):
        """A list of caught up media keeps an entry still being repeated."""
        anime = Anime.objects.create(
            item=self.anime_item,
            user=self.user,
            status=Status.REPEATING.value,
            progress=1,
        )

        response = self.post(
            self.anime_item,
            quick_actions.NEXT,
            instance_id=anime.id,
            status="caught_up",
        )

        self.assertNotIn("HX-Reswap", response)

    def test_completed_card_leaves_caught_up_list(self):
        """Finishing media removes it from a list of caught up media."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
            status="caught_up",
        )

        self.assertEqual(response["HX-Reswap"], "delete")
        self.assertEqual(response["HX-Trigger"], "libraryChanged")

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
        """The new card keeps the release year and highlight of the old one."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.COMPLETE,
            instance_id=movie.id,
            release_year="1999",
            active="true",
        )

        self.assertContains(response, "<span>1999</span>", html=True)
        self.assertContains(response, "ring-indigo-400")

    def test_planning_a_rewatch_keeps_the_finished_watch(self):
        """A finished movie's rewatch is a new entry, and marking it is a new play."""
        first = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
            end_date=datetime(2020, 1, 1, tzinfo=UTC),
        )

        response = self.post(
            self.movie_item,
            quick_actions.PLAN_REPEAT,
            instance_id=first.id,
        )

        first.refresh_from_db()
        self.assertEqual(first.status, Status.COMPLETED.value)
        rewatch = Movie.objects.exclude(pk=first.pk).get(item=self.movie_item)
        self.assertEqual(rewatch.status, Status.PLANNING_REPEAT.value)
        self.assertIsNone(rewatch.end_date)
        self.assertContains(response, "Mark rewatched")
        self.assertContains(response, "Planning to rewatch Test Movie.")

        self.post(self.movie_item, quick_actions.REPEAT, instance_id=rewatch.id)

        rewatch.refresh_from_db()
        self.assertEqual(rewatch.status, Status.COMPLETED.value)
        self.assertEqual(timezone.localdate(rewatch.end_date), timezone.localdate())
        self.assertEqual(Movie.objects.filter(item=self.movie_item).count(), 2)

    def test_stale_card_plans_one_rewatch(self):
        """A finished movie's card left open plans its rewatch only once."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        self.post(self.movie_item, quick_actions.PLAN_REPEAT, instance_id=movie.id)
        response = self.post(
            self.movie_item,
            quick_actions.PLAN_REPEAT,
            instance_id=movie.id,
        )

        self.assertEqual(
            sorted(
                Movie.objects.filter(item=self.movie_item).values_list(
                    "status",
                    flat=True,
                ),
            ),
            [Status.COMPLETED.value, Status.PLANNING_REPEAT.value],
        )
        self.assertContains(response, "This entry has already changed")

    @patch("app.providers.services.get_media_metadata")
    def test_planned_reread_is_started_not_finished(self, mock_metadata):
        """A planned reread of a book starts as a repeat, ready for logging."""
        mock_metadata.return_value = {"max_progress": None}
        item = Item.objects.create(
            media_id="3",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Test Book",
            image="http://example.com/book.jpg",
        )
        book = Book.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING_REPEAT.value,
        )

        response = self.post(
            item,
            quick_actions.START_REPEAT,
            instance_id=book.id,
        )

        book.refresh_from_db()
        self.assertEqual(book.status, Status.REPEATING.value)
        self.assertIsNotNone(book.start_date)
        self.assertContains(response, "Started rereading Test Book.")
        self.assertContains(response, "Log pages")

    def test_planned_rewatch_leaves_the_completed_list(self):
        """A rewatch planned from the completed list moves its card out of it."""
        movie = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        response = self.post(
            self.movie_item,
            quick_actions.PLAN_REPEAT,
            instance_id=movie.id,
            status=Status.COMPLETED.value,
        )

        self.assertEqual(response["HX-Reswap"], "delete")
