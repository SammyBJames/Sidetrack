import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from app import library_fields
from app.models import (
    TV,
    Episode,
    Game,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.templatetags import app_tags
from events.models import Event
from users.forms import UserUpdateForm


class MediaListViewTests(TestCase):
    """Test the media list view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.external_credentials = {
            "username": "test2",
            "password": "12345",
            "profile_private": True,
        }
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.external_user = get_user_model().objects.create_user(
            **self.external_credentials
        )
        self.client.login(**self.credentials)

        movies_id = ["278", "238", "129", "424", "680"]
        num_completed = 3
        for i in range(1, 6):
            item = Item.objects.create(
                media_id=movies_id[i - 1],
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Test Movie {i}",
                image="http://example.com/image.jpg",
            )
            status = (
                Status.COMPLETED.value
                if i < num_completed
                else Status.IN_PROGRESS.value
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=status,
                progress=1 if i < num_completed else 0,
                score=i,
            )

    def test_media_list_view(self):
        """Test the media list view displays media items."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        self.assertIn("sort_choices", response.context)
        self.assertIn("status_choices", response.context)
        self.assertEqual(response.context["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(
            response.context["media_type_plural"],
            app_tags.media_type_readable_plural(MediaTypes.MOVIE.value).lower(),
        )

    def sort_keys(self, media_type, **params):
        """Return the sort choices a media type's list offers, by key."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, media_type]),
            params,
        )
        return [choice["value"] for choice in response.context["sort_choices"]]

    def test_sort_choices_have_their_menu_headings(self):
        """Each listed sort has the heading the sort menu groups it under."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
        )
        groups = {
            choice["value"]: choice["group"]
            for choice in response.context["sort_choices"]
        }
        self.assertEqual(groups["recent"], "Your activity")
        self.assertEqual(groups["score"], "Your activity")
        self.assertEqual(groups["title"], "About the title")
        fields = library_fields.get_fields(MediaTypes.MOVIE.value)
        self.assertEqual(fields["provider_score"].group, "Ratings")
        self.assertEqual(fields["runtime"].group, "About the title")

    def test_sort_and_filters_are_separate_controls(self):
        """The toolbar has a sort menu, then an icon button for the filter panel.

        The panel has no sort controls.
        """
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
        )
        html = response.content.decode()
        self.assertIn('role="menuitemradio"', html)
        self.assertLess(
            html.index('x-ref="sortButton"'),
            html.index('x-ref="panelButton"'),
        )

        button = html[html.index('x-ref="panelButton"') :]
        button = button[: button.index("</button>")]
        self.assertIn('aria-label="Filters"', button)
        self.assertNotIn(">Filters</span>", button)
        self.assertIn("<polygon", button)
        self.assertIn('x-text="filterCount"', button)

        panel = html[html.index('role="dialog"') :]
        panel = panel[: panel.index("</form>")]
        self.assertIn(">Filters</h2>", panel)
        self.assertNotIn("sortBy(", panel)
        self.assertNotIn("Sort", panel)

    def test_rows_open_their_media_and_act_like_cards(self):
        """A list row's title link covers the row, and its action is the cards' chip."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
            {"layout": "table"},
        )
        html = response.content.decode()
        row = html[html.index('class="media-row') :]
        row = row[: row.index('class="media-row', 1)]
        self.assertIn("after:absolute after:inset-0", row)
        self.assertIn("card-chip", row)
        self.assertIn('hx-target="closest .media-row"', row)
        self.assertIn('hx-include="#filter-form"', row)
        self.assertIn('"card": "row"', row)

    def test_sort_choices_are_the_media_types_sorts(self):
        """The sort dropdown lists the sorts of the list's media type."""
        for media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.BOOK.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
        ):
            response = self.client.get(
                reverse("medialist", args=[self.user.username, media_type]),
            )
            choices = {
                choice["value"]: choice for choice in response.context["sort_choices"]
            }
            with self.subTest(media_type=media_type):
                # Fields about the title wait for a title with a value (below)
                self.assertEqual(
                    list(choices),
                    [
                        field.key
                        for field in library_fields.get_sortable_fields(media_type)
                        if field.key not in library_fields.DETAIL_KEYS
                    ],
                )
                self.assertEqual(choices["title"]["order"], "asc")
                self.assertEqual(choices["title"]["order_labels"], ("A to Z", "Z to A"))

        self.assertNotIn("progress", self.sort_keys(MediaTypes.MOVIE.value))

    def test_sorts_about_the_title_need_a_value_in_the_library(self):
        """A detail sort is listed once a title has a value, or while it is the sort."""
        movie = self.sort_keys(MediaTypes.MOVIE.value)
        for key in ("imdb", "runtime", "release", "provider_score"):
            self.assertNotIn(key, movie)
        # Your own entries' sorts are always listed
        self.assertIn("score", movie)

        Item.objects.filter(media_id="278").update(imdb_score=8.1, runtime=120)
        # Another user's titles don't count
        Movie.objects.bulk_create(
            [
                Movie(
                    item=Item.objects.create(
                        media_id="other",
                        source=Sources.MANUAL.value,
                        media_type=MediaTypes.MOVIE.value,
                        title="Theirs",
                        metacritic=80,
                    ),
                    user=self.external_user,
                    status=Status.COMPLETED.value,
                ),
            ],
        )
        movie = self.sort_keys(MediaTypes.MOVIE.value)
        self.assertIn("imdb", movie)
        self.assertIn("runtime", movie)
        self.assertNotIn("metacritic", movie)

        # The current sort stays listed with nothing to sort by
        self.assertIn(
            "metacritic",
            self.sort_keys(MediaTypes.MOVIE.value, sort="metacritic"),
        )

    def test_tv_has_no_rotten_tomatoes_or_metacritic(self):
        """Shows have neither, and a saved sort of either falls back to the default."""
        for key in ("rotten_tomatoes", "metacritic"):
            tv = {f.key for f in library_fields.get_sortable_fields("tv")}
            self.assertNotIn(key, tv)
            get_user_model().objects.filter(pk=self.user.pk).update(tv_sort=key)

            response = self.client.get(
                reverse("medialist", args=[self.user.username, MediaTypes.TV.value]),
            )

            self.assertEqual(response.context["current_sort"], "score")

    def test_saved_sort_the_media_type_lacks_loads_the_default(self):
        """A movie list saved as progress loads sorted by the default."""
        get_user_model().objects.filter(pk=self.user.pk).update(
            movie_sort="progress",
            movie_sort_order="asc",
        )
        url = reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_order"], "desc")
        self.assertEqual(response.context["current_sort_label"], "Your rating")
        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_sort, "score")

        # Asked for as well, it's the same
        response = self.client.get(url, {"sort": "progress"})
        self.assertEqual(response.context["current_sort"], "score")

    def test_saved_sort_of_another_users_list_falls_back_unsaved(self):
        """Someone else's list falls back too, leaving their preference alone."""
        get_user_model().objects.filter(pk=self.external_user.pk).update(
            profile_private=False,
            movie_sort="progress",
        )

        response = self.client.get(
            reverse(
                "medialist",
                args=[self.external_user.username, MediaTypes.MOVIE.value],
            ),
        )

        self.assertEqual(response.context["current_sort"], "score")
        self.external_user.refresh_from_db()
        self.assertEqual(self.external_user.movie_sort, "progress")

    def test_season_list_opens_the_tv_list(self):
        """Seasons have no list of their own, so theirs opens their shows'."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.SEASON.value]),
            {"status": Status.IN_PROGRESS.value},
        )

        self.assertRedirects(
            response,
            reverse("medialist", args=[self.user.username, MediaTypes.TV.value])
            + "?status=In+progress",
        )

    def test_seasons_are_not_in_the_sidebar(self):
        """The sidebar lists shows but not their seasons."""
        media_types = [
            item["media_type"] for item in app_tags.get_sidebar_media_types(self.user)
        ]

        self.assertIn(MediaTypes.TV.value, media_types)
        self.assertNotIn(MediaTypes.SEASON.value, media_types)

    def test_media_list_with_filters(self):
        """Test the media list view with filters."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?status=Completed&sort=score&layout=table",
        )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            response.context["current_status"],
            Status.COMPLETED.value,
        )
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_layout"], "table")

        self.assertEqual(response.context["media_list"].paginator.count, 2)

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_status, Status.COMPLETED.value)
        self.assertEqual(self.user.movie_sort, "score")
        self.assertEqual(self.user.movie_layout, "table")

    def test_status_filter_goes_by_the_latest_entry(self):
        """A planned rewatch is listed as one, not as completed by its earlier play."""
        first = Movie.objects.get(user=self.user, item__title="Test Movie 1")
        Movie.objects.create(
            item=first.item,
            user=self.user,
            status=Status.PLANNING_REPEAT.value,
        )
        url = reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])

        completed = self.client.get(url, {"status": Status.COMPLETED.value})
        rewatch = self.client.get(url, {"status": Status.PLANNING_REPEAT.value})

        self.assertEqual(
            [movie.item.title for movie in completed.context["media_list"]],
            ["Test Movie 2"],
        )
        planned = list(rewatch.context["media_list"])
        self.assertEqual([movie.item.title for movie in planned], ["Test Movie 1"])
        self.assertEqual(planned[0].repeats, 2)

    def test_status_filter_uses_the_media_types_words(self):
        """The movie list's statuses are named in its words, each with its count."""
        Movie.objects.filter(item__title="Test Movie 3").update(
            status=Status.REPEATING.value,
        )

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
        )

        choices = {
            choice["value"]: (choice["label"], choice["count"])
            for choice in response.context["status_choices"]
        }
        self.assertEqual(choices[Status.REPEATING.value], ("Rewatching", 1))
        self.assertEqual(choices["All"], ("All", 5))
        # Statuses without media are left out
        self.assertNotIn(Status.DROPPED.value, choices)

    def test_media_list_htmx_request(self):
        """Filtering gets the results alone, scrolling the next page's entries."""
        url = reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])

        response = self.client.get(f"{url}?layout=grid", headers={"hx-request": "true"})
        self.assertTemplateUsed(response, "app/components/library_results.html")
        self.assertTemplateNotUsed(response, "app/media_list.html")

        for page in range(1, 6):
            Movie.objects.create(
                item=Item.objects.create(
                    media_id=f"9{page}",
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"More {page}",
                    image="http://example.com/image.jpg",
                ),
                user=self.user,
                status=Status.PLANNING.value,
            )
        with patch("app.views.LIST_PAGE_SIZE", 4):
            grid = self.client.get(f"{url}?page=2", headers={"hx-request": "true"})
            rows = self.client.get(
                f"{url}?page=2&layout=table",
                headers={"hx-request": "true"},
            )
        self.assertTemplateUsed(grid, "app/components/media_grid_items.html")
        self.assertTemplateNotUsed(grid, "app/components/library_results.html")
        self.assertTemplateUsed(rows, "app/components/media_list_rows.html")

    def test_media_list_soft_navigation_returns_full_page(self):
        """Soft-navigation body swaps (after an edit modal) get the full page."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
            headers={"hx-request": "true", "x-soft-navigation": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

    def test_public_media_list_ignores_invalid_filters(self):
        """Test invalid public filters fall back to the target user's preferences."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
            + "?status=invalid&sort=bad_field&layout=invalid",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["current_status"], self.external_user.movie_status
        )
        self.assertEqual(
            response.context["current_sort"], self.external_user.movie_sort
        )
        self.assertEqual(
            response.context["current_layout"], self.external_user.movie_layout
        )

    def test_anonymous_user_can_view_public_media_list(self):
        """Test anonymous users can view public media lists."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])
        self.client.logout()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)

    def test_profile_private_defaults_to_true(self):
        """Test new users have private profiles by default."""
        user = get_user_model().objects.create_user(
            username="private-default",
        )

        self.assertTrue(user.profile_private)

    def test_private_media_list(self):
        """Test the private media list view."""
        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 404)

        form = UserUpdateForm(
            data={"username": "test2", "profile_private": False},
            instance=self.external_user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        external_user = form.save()
        external_user.refresh_from_db()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)


class GameCardTests(TestCase):
    """Test the details line of the library's game cards."""

    def test_game_card_shows_time_played(self):
        """Away from home, a game in progress shows its time played."""
        credentials = {"username": "test", "password": "12345"}
        user = get_user_model().objects.create_user(**credentials)
        self.client.login(**credentials)
        item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Played Game",
            image="http://example.com/image.jpg",
        )
        Game.objects.create(
            item=item,
            user=user,
            status=Status.IN_PROGRESS.value,
            progress=150,
        )

        response = self.client.get(
            reverse("medialist", args=[user.username, MediaTypes.GAME.value]),
            {"layout": "grid"},
        )

        self.assertContains(response, "2h 30min")


class TVCardCountsTests(TestCase):
    """Test the seasons and episodes on the TV list's cards."""

    def setUp(self):
        """Log in and track a show with two released seasons of 3 and 2 episodes."""
        credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**credentials)
        self.client.login(**credentials)
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
            image="http://example.com/show.jpg",
        )
        self.tv = TV(item=tv_item, user=self.user, status=Status.COMPLETED.value)
        TV.save_base(self.tv)
        aired = timezone.now() - timedelta(days=30)
        # Season 3 hasn't aired yet
        upcoming = timezone.now() + timedelta(days=30)
        for number, episodes, when in ((1, 3, aired), (2, 2, aired), (3, 8, upcoming)):
            item = Item.objects.create(
                media_id="1",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Show",
                season_number=number,
            )
            Event.objects.create(item=item, content_number=episodes, datetime=when)

    def card_text(self):
        """Return the TV list's page."""
        return self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.TV.value]),
        ).content.decode()

    def test_shows_count_what_is_out(self):
        """A show lists its released seasons and episodes, shortened to fit."""
        content = self.card_text()

        self.assertIn("2 Season", content)
        self.assertIn("5 Episodes", content)
        self.assertIn("2 S<", content)
        self.assertIn("5 E<", content)
        self.assertNotIn("Eps", content)

    def test_shows_bold_their_seasons(self):
        """A show's seasons count is bold like a season's number on other cards."""
        content = re.sub(r"\s+", " ", self.card_text())

        self.assertIn(
            '<span class="font-semibold text-gray-100">'
            '<span class="@[9.5rem]:hidden">2 S</span>'
            '<span class="hidden @[9.5rem]:inline">2 Seasons</span></span>',
            content,
        )

    def test_counts_cost_no_query_per_show(self):
        """The TV list costs the same number of queries for more shows."""

        def queries():
            """Return how many queries one render of the TV list runs."""
            with CaptureQueriesContext(connection) as captured:
                self.card_text()
            return len(captured)

        few = queries()
        for number in range(2, 5):
            item = Item.objects.create(
                media_id=str(number),
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title=f"Show {number}",
                image="http://example.com/show.jpg",
            )
            TV.save_base(TV(item=item, user=self.user, status=Status.COMPLETED.value))

        self.assertEqual(queries(), few)

    def test_shows_in_progress_count_the_same(self):
        """A show in progress lists its seasons and episodes the same way."""
        self.tv.status = Status.IN_PROGRESS.value
        TV.save_base(self.tv)

        content = self.card_text()
        self.assertIn("2 Seasons", content)
        self.assertIn("5 Episodes", content)

    def test_caught_up_show_waits_outside_in_progress(self):
        """A show watched up to its latest episode is caught up, not in progress."""
        self.tv.status = Status.IN_PROGRESS.value
        TV.save_base(self.tv)
        for number, episodes in ((1, 3), (2, 2)):
            season = Season(
                item=Item.objects.get(
                    media_type=MediaTypes.SEASON.value,
                    season_number=number,
                ),
                user=self.user,
                related_tv=self.tv,
                status=Status.COMPLETED.value,
            )
            Season.save_base(season)
            Episode.objects.bulk_create(
                Episode(
                    related_season=season,
                    item=Item.objects.create(
                        media_id="1",
                        source=Sources.TMDB.value,
                        media_type=MediaTypes.EPISODE.value,
                        title="Show",
                        season_number=number,
                        episode_number=episode_number,
                    ),
                    end_date=timezone.now(),
                )
                for episode_number in range(1, episodes + 1)
            )
        url = reverse("medialist", args=[self.user.username, MediaTypes.TV.value])

        def listed(status):
            """Return the ids of the TV shows listed for a status filter."""
            response = self.client.get(url, {"status": status})
            return [media.id for media in response.context["media_list"]]

        self.assertEqual(listed(Status.IN_PROGRESS.value), [])
        self.assertEqual(listed("caught_up"), [self.tv.id])
        self.assertEqual(listed("All"), [self.tv.id])

    def test_show_waiting_on_its_new_season_shows_when_it_airs(self):
        """A planned show with only unaired seasons left offers its release date."""
        self.tv.status = Status.PLANNING.value
        TV.save_base(self.tv)
        for number, status in ((1, Status.COMPLETED.value), (3, Status.PLANNING.value)):
            Season.save_base(
                Season(
                    item=Item.objects.get(
                        media_type=MediaTypes.SEASON.value,
                        season_number=number,
                    ),
                    user=self.user,
                    related_tv=self.tv,
                    status=status,
                ),
            )

        content = self.card_text()

        self.assertIn('aria-label="Add to calendar: S3E8', content)
        self.assertNotIn("Mark S3E1 watched", content)
