import datetime
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from events.models import Event, SentinelDatetime
from lists.models import CustomList, CustomListItem


class ListsViewTests(TestCase):
    """Tests for the lists view."""

    def setUp(self):
        """Set up test data for lists view tests."""
        self.factory = RequestFactory()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )

        # Create some test lists
        self.list1 = CustomList.objects.create(
            name="Test List 1",
            description="Description 1",
            owner=self.user,
        )
        self.list2 = CustomList.objects.create(
            name="Test List 2",
            description="Description 2",
            owner=self.user,
        )

        # Add collaborator to one list
        self.list1.collaborators.add(self.collaborator)

        # Create some items
        self.item1 = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
        )
        self.item2 = Item.objects.create(
            media_id="2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
        )

        # Add items to lists
        CustomListItem.objects.create(
            custom_list=self.list1,
            item=self.item1,
        )
        CustomListItem.objects.create(
            custom_list=self.list2,
            item=self.item2,
        )

    def test_lists_owner_view(self):
        """Test the lists view response and context for owner."""
        self.client.login(**self.credentials)
        response = self.client.get(reverse("lists"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/custom_lists.html")
        self.assertIn("custom_lists", response.context)
        self.assertIn("form", response.context)

    def test_lists_collaborator_view(self):
        """Test the lists view response and context for a collaborator."""
        self.client.login(**self.collaborator_credentials)
        response = self.client.get(reverse("lists"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/custom_lists.html")
        self.assertIn("custom_lists", response.context)
        self.assertIn("form", response.context)

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_search_filter(self, mock_update_preference):
        """Test the lists view with search filter."""
        mock_update_preference.return_value = "name"
        self.client.login(**self.credentials)

        # Test search by name
        response = self.client.get(reverse("lists") + "?q=List 1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["custom_lists"]), 1)
        self.assertEqual(response.context["custom_lists"][0].name, "Test List 1")

        # Test search by description
        response = self.client.get(reverse("lists") + "?q=Description 2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["custom_lists"]), 1)
        self.assertEqual(response.context["custom_lists"][0].name, "Test List 2")

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_sorting(self, mock_update_preference):
        """Test the lists view with different sorting options."""
        self.client.login(**self.credentials)

        # Test name sorting
        mock_update_preference.return_value = "name"
        response = self.client.get(reverse("lists") + "?sort=name")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "name")

        # Test items_count sorting
        mock_update_preference.return_value = "items_count"
        response = self.client.get(reverse("lists") + "?sort=items_count")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "items_count")

        # Test newest_first sorting
        mock_update_preference.return_value = "newest_first"
        response = self.client.get(reverse("lists") + "?sort=newest_first")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "newest_first")

        # Test default sorting (last_item_added)
        mock_update_preference.return_value = "last_item_added"
        response = self.client.get(reverse("lists"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "last_item_added")

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_htmx_request(self, mock_update_preference):
        """Test the lists view with HTMX request."""
        mock_update_preference.return_value = "name"
        self.client.login(**self.credentials)

        # Make an HTMX request
        response = self.client.get(reverse("lists"), headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/list_grid.html")

        self.assertIn("custom_lists", response.context)

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_pagination(self, mock_update_preference):
        """Test the lists view pagination."""
        mock_update_preference.return_value = "name"
        self.client.login(**self.credentials)

        # Create more lists to test pagination
        for i in range(25):  # Create 25 more lists (27 total)
            CustomList.objects.create(
                name=f"Paginated List {i}",
                owner=self.user,
            )

        # Test first page
        response = self.client.get(reverse("lists"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["custom_lists"]), 20)  # 20 per page

        # Test second page
        response = self.client.get(reverse("lists") + "?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["custom_lists"]), 7)  # 7 remaining items


class ListDetailViewTests(TestCase):
    """Tests for the list_detail view."""

    def setUp(self):
        """Set up test data."""
        self.factory = RequestFactory()
        self.credentials = {"username": "testuser", "password": "testpassword"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.other_credentials = {
            "username": "otheruser",
            "password": "testpassword",
        }
        self.other_user = get_user_model().objects.create_user(
            **self.other_credentials,
        )
        self.client.login(**self.credentials)

        # Create a test list
        self.custom_list = CustomList.objects.create(
            name="Test List",
            description="Test Description",
            owner=self.user,
        )

        # Create some items with different media types
        self.movie_item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
        )
        self.tv_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
        )
        self.anime_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
        )

        # Add items to the list
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.movie_item,
        )
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.tv_item,
        )
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.anime_item,
        )

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        # Create Movie instance
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        # Create TV instance
        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        # Create Anime instance
        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test the view
        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/list_detail.html")

        # Check context data
        self.assertEqual(response.context["custom_list"], self.custom_list)
        self.assertEqual(len(response.context["items"]), 3)
        self.assertEqual(response.context["current_sort"], "date_added")
        self.assertEqual(response.context["items_count"], 3)

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_unauthorized(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view when user is not authorized."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = False

        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))
        self.assertEqual(response.status_code, 404)

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_filter_by_media_type(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with media type filter."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test the view with media type filter
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id])
            + f"?type={MediaTypes.MOVIE.value}",
        )
        self.assertEqual(response.status_code, 200)

        # Should only have the movie item
        self.assertEqual(len(response.context["items"]), 1)
        self.assertEqual(
            response.context["items"][0].media_type,
            MediaTypes.MOVIE.value,
        )

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_filter_by_status(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with status filter."""
        mock_update_preference.side_effect = ["date_added", Status.PLANNING.value]
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test the view with status filter
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id])
            + f"?status={Status.PLANNING.value}",
        )
        self.assertEqual(response.status_code, 200)

        # Check that filters are applied
        self.assertEqual(
            response.context["current_status"],
            Status.PLANNING.value,
        )
        # Should only have the PLANNING item of media type ANIME
        self.assertEqual(len(response.context["items"]), 1)
        self.assertEqual(
            response.context["items"][0].media_type,
            MediaTypes.ANIME.value,
        )

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_search(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with search filter."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test the view with search filter
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?q=Anime",
        )
        self.assertEqual(response.status_code, 200)

        # Should only have the anime item
        self.assertEqual(len(response.context["items"]), 1)
        self.assertEqual(response.context["items"][0].title, "Test Anime")

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_sorting(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with different sorting options."""
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test title sorting
        mock_update_preference.side_effect = ["title", None]
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=title",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")

        # Test media_type sorting
        mock_update_preference.side_effect = ["media_type", None]
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=media_type",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "media_type")

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_htmx_request(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with HTMX request."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Make an HTMX request
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]),
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/media_grid.html")
        self.assertNotIn("form", response.context)

    @patch.object(get_user_model(), "update_preference")
    def test_list_detail_view_with_watched_episode(self, mock_update_preference):
        """A list holding a watched episode draws its card with the year."""
        mock_update_preference.side_effect = ["date_added", None]
        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            season_number=1,
        )
        episode_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Test TV Show",
            season_number=1,
            episode_number=1,
            release_date=datetime.date(2004, 9, 22),
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        Episode.objects.bulk_create(
            [Episode(item=episode_item, related_season=season)],
        )
        CustomListItem.objects.create(custom_list=self.custom_list, item=episode_item)

        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<span>2004</span>")


class ListEpisodeCardTests(TestCase):
    """Tests for the episode cards of a custom list."""

    def setUp(self):
        """Create a user with an empty list."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.custom_list = CustomList.objects.create(name="List", owner=self.user)

    def add_episode(
        self,
        media_id,
        number,
        *,
        season=None,
        watched=False,
        status=Status.COMPLETED.value,
        season_number=1,
    ):
        """Put an episode in the list, with a watch in `season` when `watched`."""
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_number,
            episode_number=number,
            defaults={"title": f"Show {media_id}"},
        )
        CustomListItem.objects.get_or_create(custom_list=self.custom_list, item=item)
        if watched:
            Episode.objects.bulk_create(
                [Episode(item=item, related_season=season, status=status)]
            )
        return item

    def add_season(self, media_id, number=1, *, user=None, status=None):
        """Track a season of a show, for `user` (the viewer by default)."""
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=number,
            defaults={"title": f"Show {media_id}"},
        )
        return Season.objects.create(
            item=item,
            user=user or self.user,
            status=status or Status.IN_PROGRESS.value,
        )

    def get_page(self):
        """Return the list page's markup."""
        return self.client.get(
            reverse("list_detail", args=[self.custom_list.id]),
        ).content.decode()

    def test_cards_show_the_action_of_their_episode(self):
        """Watched, unwatched and untracked-season episodes each get their action."""
        season = self.add_season("1")
        self.add_episode("1", 1, season=season, watched=True)
        self.add_episode("1", 2, season=season)
        self.add_episode("2", 1)

        content = self.get_page()

        for media_id, number, instance_id, action in (
            ("1", 1, season.id, "plan_repeat"),
            ("1", 2, season.id, "watch"),
            ("2", 1, "", "watch"),
        ):
            self.assertRegex(
                content,
                rf'"media_id": "{media_id}".*"episode_number": "{number}", '
                rf'"instance_id": "{instance_id}", "action": "{action}", '
                r'"card": "grid"',
            )
        # Tracking is edited on the episode's page
        self.assertNotIn("Edit tracking", content)
        self.assertNotIn(reverse("track_modal", args=["tmdb", "episode", "1"]), content)

    def test_unaired_episode_shows_no_action(self):
        """An episode that is not out yet, by its calendar event, can't be watched."""
        season = self.add_season("1")
        Event.objects.create(
            item=season.item,
            content_number=3,
            datetime=datetime.datetime(2099, 1, 1, 12, tzinfo=datetime.UTC),
        )
        Event.objects.create(
            item=season.item,
            content_number=2,
            datetime=timezone.now() - datetime.timedelta(days=30),
        )
        self.add_episode("1", 3, season=season)
        self.add_episode("1", 2, season=season)

        content = self.get_page()

        self.assertRegex(
            content, r'"episode_number": "2", "instance_id": "\d+", "action": "watch"'
        )
        self.assertNotRegex(
            content, r'"episode_number": "3", "instance_id": "\d+", "action": '
        )
        self.assertIn("E3 \u00b7 January 1, 2099", content)

    def test_episode_with_unknown_date_can_be_watched(self):
        """An episode whose event holds the unknown-date sentinel is not unaired."""
        season = self.add_season("1")
        Event.objects.create(
            item=season.item,
            content_number=1,
            datetime=SentinelDatetime.max_datetime(),
        )
        self.add_episode("1", 1, season=season)

        self.assertRegex(
            self.get_page(),
            r'"episode_number": "1", "instance_id": "\d+", "action": "watch"',
        )

    def test_planned_rewatch_of_an_episode_offers_to_watch_it_again(self):
        """A watched episode with a planned rewatch is watched again from its card."""
        season = self.add_season("1", status=Status.PLANNING_REPEAT.value)
        self.add_episode("1", 1, season=season, watched=True)
        self.add_episode(
            "1",
            1,
            season=season,
            watched=True,
            status=Status.PLANNING_REPEAT.value,
        )

        content = self.get_page()

        self.assertRegex(
            content,
            rf'"episode_number": "1", "instance_id": "{season.id}", "action": "watch"',
        )

    def test_other_users_entries_do_not_leak_into_cards(self):
        """Another user's watch and season entry leave the viewer's card untouched."""
        season = self.add_season("1")
        other = get_user_model().objects.create_user(
            username="other",
            password="12345",  # noqa: S106
        )
        other_season = self.add_season("1", user=other)
        self.add_episode("1", 1, season=other_season, watched=True)

        content = self.get_page()

        self.assertRegex(
            content,
            rf'"episode_number": "1", "instance_id": "{season.id}", "action": "watch"',
        )

    def test_episodes_of_two_seasons_use_their_own_season(self):
        """Episodes of one show's seasons take the entry and events of their own."""
        season_one = self.add_season("1")
        Event.objects.create(
            item=season_one.item,
            content_number=1,
            datetime=datetime.datetime(2099, 1, 1, 12, tzinfo=datetime.UTC),
        )
        self.add_episode("1", 1, season=season_one)
        self.add_episode("1", 2, season=season_one, watched=True)
        self.add_episode("1", 1, season_number=2)

        content = self.get_page()

        self.assertRegex(
            content,
            r'"season_number": "1", "episode_number": "2", '
            rf'"instance_id": "{season_one.id}", "action": "plan_repeat"',
        )
        self.assertRegex(
            content,
            r'"season_number": "2", "episode_number": "1", '
            r'"instance_id": "", "action": "watch"',
        )

    def test_query_count_does_not_grow_with_episode_cards(self):
        """Loading the state of more episode cards costs no more queries."""
        season = self.add_season("1")
        self.add_episode("1", 1, season=season, watched=True)
        self.get_page()  # warm up per-process caches
        with CaptureQueriesContext(connection) as one:
            self.get_page()

        self.add_episode("1", 2, season=season, watched=True)
        self.add_episode("1", 3, season=season)
        other = self.add_season("2")
        self.add_episode("2", 1, season=other, watched=True)
        self.add_episode("3", 1)
        with CaptureQueriesContext(connection) as many:
            content = self.get_page()

        self.assertEqual(len(re.findall(r'"episode_number"', content)), 5)
        self.assertEqual(len(many), len(one))
        self.assertEqual(len(re.findall("media-card ", content)), 5)


class ListReleaseCardTests(TestCase):
    """Tests for list cards of media waiting on a release."""

    def setUp(self):
        """Create a user with an empty list."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.custom_list = CustomList.objects.create(name="List", owner=self.user)

    def add_entry(self, model, media_type, events, **fields):
        """Put an entry with release `events` of (number, datetime) in the list."""
        item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value
            if media_type == MediaTypes.ANIME.value
            else Sources.TMDB.value,
            media_type=media_type,
            title="Title",
        )
        for number, when in events:
            Event.objects.create(item=item, content_number=number, datetime=when)
        CustomListItem.objects.create(custom_list=self.custom_list, item=item)
        return model.objects.create(item=item, user=self.user, **fields)

    def get_page(self):
        """Return the list page's markup."""
        return self.client.get(
            reverse("list_detail", args=[self.custom_list.id]),
        ).content.decode()

    def test_planned_unreleased_movie_shows_its_release(self):
        """A planned movie out only in 2099 shows its release, as in the library."""
        self.add_entry(
            Movie,
            MediaTypes.MOVIE.value,
            [(None, datetime.datetime(2099, 1, 1, 12, tzinfo=datetime.UTC))],
            status=Status.PLANNING.value,
        )

        content = self.get_page()

        self.assertIn("Add to calendar", content)
        self.assertIn("January 1, 2099", content)
        self.assertIn(
            "Add to calendar",
            self.client.get(
                reverse("medialist", args=["test", MediaTypes.MOVIE.value])
            ).content.decode(),
        )

    def test_caught_up_entry_shows_its_next_release(self):
        """An anime in progress with all that is out watched shows its next release."""
        self.add_entry(
            Anime,
            MediaTypes.ANIME.value,
            [
                (1, timezone.now() - datetime.timedelta(days=7)),
                (2, datetime.datetime(2099, 1, 1, 12, tzinfo=datetime.UTC)),
            ],
            status=Status.IN_PROGRESS.value,
            progress=1,
        )

        content = self.get_page()

        self.assertIn("Add to calendar", content)
        self.assertIn("January 1, 2099", content)


class CreateListViewTest(TestCase):
    """Test case for the create list view."""

    def setUp(self):
        """Set up test data for create list view tests."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_create_list(self):
        """Test creating a new custom list."""
        self.client.post(
            reverse("list_create"),
            {"name": "New List", "description": "New Description"},
        )
        self.assertEqual(CustomList.objects.count(), 1)
        new_list = CustomList.objects.first()
        self.assertEqual(new_list.name, "New List")
        self.assertEqual(new_list.description, "New Description")
        self.assertEqual(new_list.owner, self.user)


class EditListViewTest(TestCase):
    """Test case for the edit list view."""

    def setUp(self):
        """Set up test data for edit list view tests."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )
        self.list = CustomList.objects.create(name="Test List", owner=self.user)
        self.list.collaborators.add(self.collaborator)

    def test_edit_list(self):
        """Test editing an existing custom list."""
        self.client.login(**self.credentials)
        self.client.post(
            reverse("list_edit"),
            {
                "list_id": self.list.id,
                "name": "Updated List",
                "description": "Updated Description",
            },
        )
        self.list.refresh_from_db()
        self.assertEqual(self.list.name, "Updated List")
        self.assertEqual(self.list.description, "Updated Description")

    def test_edit_list_collaborator(self):
        """Test editing an existing custom list as a collaborator."""
        self.client.login(**self.collaborator_credentials)
        self.client.post(
            reverse("list_edit"),
            {
                "list_id": self.list.id,
                "name": "Updated List",
                "description": "Updated Description",
            },
        )
        self.list.refresh_from_db()
        self.assertEqual(self.list.name, "Updated List")
        self.assertEqual(self.list.description, "Updated Description")


class DeleteListViewTest(TestCase):
    """Test the delete view."""

    def setUp(self):
        """Create a user, log in, and create a list."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )
        self.list = CustomList.objects.create(name="Test List", owner=self.user)
        self.list.collaborators.add(self.collaborator)

    def test_delete_list(self):
        """Test deleting a list."""
        self.client.login(**self.credentials)
        self.client.post(reverse("list_delete"), {"list_id": self.list.id})
        self.assertEqual(CustomList.objects.count(), 0)

    def test_delete_list_collaborator(self):
        """Test deleting a list as a collaborator."""
        self.client.login(**self.collaborator_credentials)
        self.client.post(reverse("list_delete"), {"list_id": self.list.id})
        self.assertEqual(CustomList.objects.count(), 1)


class ListsModalViewTests(TestCase):
    """Tests for the lists_modal view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        # Create some test lists
        self.list1 = CustomList.objects.create(
            name="Test List 1",
            owner=self.user,
        )
        self.list2 = CustomList.objects.create(
            name="Test List 2",
            owner=self.user,
        )

    def test_lists_modal_view(self):
        """Test the basic lists_modal view."""
        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.MOVIE.value, 10494],
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/fill_lists.html")
        self.assertIn("item", response.context)
        self.assertIn("custom_lists", response.context)

    @patch("app.providers.services.get_media_metadata")
    @patch("lists.models.CustomList.objects.get_user_lists_with_item")
    def test_lists_modal_view_with_existing_item(
        self,
        mock_get_lists,
        mock_get_metadata,
    ):
        """Test the lists_modal view with an existing item."""
        # Create an existing item
        Item.objects.create(
            media_id="123",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Existing Movie",
            image="http://example.com/image.jpg",
        )

        # Mock the get_user_lists_with_item method
        mock_get_lists.return_value = [self.list1, self.list2]

        # Mock the get_media_metadata method
        mock_get_metadata.return_value = {
            "title": "Existing Movie",
            "image": "http://example.com/image.jpg",
        }

        # Test the view
        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.MOVIE.value, "123"],
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/fill_lists.html")

        # Check context data
        self.assertEqual(response.context["item"].media_id, "123")
        self.assertEqual(response.context["item"].title, "Existing Movie")
        self.assertEqual(len(response.context["custom_lists"]), 2)

    @patch("app.providers.services.get_media_metadata")
    @patch("lists.models.CustomList.objects.get_user_lists_with_item")
    def test_lists_modal_view_with_new_item(self, mock_get_lists, mock_get_metadata):
        """Test the lists_modal view with a new item."""
        # Mock the get_user_lists_with_item method
        mock_get_lists.return_value = [self.list1, self.list2]

        # Mock the get_media_metadata method
        mock_get_metadata.return_value = {
            "title": "New Movie",
            "image": "http://example.com/new_image.jpg",
        }

        # Test the view
        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.MOVIE.value, "999"],
            ),
        )
        self.assertEqual(response.status_code, 200)

        # Check that a new item was created
        self.assertTrue(
            Item.objects.filter(media_id="999", source=Sources.TMDB.value).exists(),
        )
        new_item = Item.objects.get(media_id="999", source=Sources.TMDB.value)
        self.assertEqual(new_item.title, "New Movie")
        self.assertEqual(new_item.image, "http://example.com/new_image.jpg")

    @patch("app.providers.services.get_media_metadata")
    @patch("lists.models.CustomList.objects.get_user_lists_with_item")
    def test_lists_modal_view_with_season(self, mock_get_lists, mock_get_metadata):
        """Test the lists_modal view with a season."""
        # Mock the get_user_lists_with_item method
        mock_get_lists.return_value = [self.list1, self.list2]

        # Mock the get_media_metadata method
        mock_get_metadata.return_value = {
            "title": "TV Show Season 1",
            "image": "http://example.com/season.jpg",
        }

        # Test the view
        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.SEASON.value, "123", "1"],
            ),
        )
        self.assertEqual(response.status_code, 200)

        # Check that a new item was created with season_number
        self.assertTrue(
            Item.objects.filter(
                media_id="123",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=1,
            ).exists(),
        )


class ListItemToggleTests(TestCase):
    """Tests for the list_item_toggle view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()

        # Create users
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )

        self.other_credentials = {
            "username": "otheruser",
            "password": "testpassword",
        }
        self.other_user = get_user_model().objects.create_user(
            **self.other_credentials,
        )

        # Create lists
        self.list = CustomList.objects.create(name="Test List", owner=self.user)
        self.list.collaborators.add(self.collaborator)

        # Create an item
        self.item = Item.objects.create(
            media_id=1,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    def test_list_item_owner_toggle(self):
        """Test adding an item to a list as owner."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.item, self.list.items.all())

    def test_list_item_owner_toggle_remove(self):
        """Test removing an item from a list as owner."""
        self.client.login(**self.credentials)
        self.list.items.add(self.item)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.item, self.list.items.all())

    def test_list_item_collaborator_toggle(self):
        """Test adding an item to a list as collaborator."""
        self.client.login(**self.collaborator_credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.item, self.list.items.all())

    def test_list_item_collaborator_toggle_remove(self):
        """Test removing an item from a list as collaborator."""
        self.client.login(**self.collaborator_credentials)
        self.list.items.add(self.item)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.item, self.list.items.all())

    def test_list_item_toggle_nonexistent_list(self):
        """Test toggling an item on a nonexistent list."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": 999,  # Nonexistent list
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_list_item_toggle_nonexistent_item(self):
        """Test toggling a nonexistent item."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": 999,  # Nonexistent item
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_list_item_toggle_unauthorized_list(self):
        """Test toggling an item on a list the user doesn't have access to."""
        self.client.login(**self.credentials)

        # Create a list owned by another user
        other_list = CustomList.objects.create(
            name="Other User's List",
            owner=self.other_user,
        )

        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": other_list.id,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_list_item_toggle_template_context(self):
        """Test the context data in the response template."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/list_item_button.html")

        # Check context data
        self.assertEqual(response.context["custom_list"], self.list)
        self.assertEqual(response.context["item"], self.item)
        self.assertTrue(response.context["has_item"])  # Item was added

        # Toggle again to remove
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_item"])  # Item was removed
