from unittest.mock import patch
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    Anime,
    Episode,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from events.models import Event


class HomeViewTests(TestCase):
    """Test the home view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.metadata_patcher = patch("app.providers.services.get_media_metadata")
        self.mock_get_media_metadata = self.metadata_patcher.start()
        self.addCleanup(self.metadata_patcher.stop)

        def mock_get_media_metadata(
            media_type,
            _media_id,
            _source,
            season_numbers=None,
            _episode_number=None,
        ):
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "details": {"seasons": 1},
                    "related": {
                        "seasons": [
                            {
                                "season_number": 1,
                                "image": "http://example.com/image.jpg",
                            },
                        ],
                    },
                }

            if media_type == "tv_with_seasons":
                season_number = season_numbers[0]
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "details": {"seasons": 1},
                    f"season/{season_number}": {
                        "episodes": [{"id": i} for i in range(1, 11)],
                    },
                    "related": {
                        "seasons": [
                            {
                                "season_number": season_number,
                                "image": "http://example.com/image.jpg",
                            },
                        ],
                    },
                }

            if media_type == MediaTypes.SEASON.value:
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 10,
                    "season/1": {
                        "episodes": [{"id": i} for i in range(1, 11)],
                    },
                }

            if media_type == MediaTypes.ANIME.value:
                return {
                    "title": "Test Anime",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 24,
                }

            if media_type == MediaTypes.MOVIE.value:
                return {
                    "title": "Planned Movie",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 1,
                }

            return {"max_progress": None}

        self.mock_get_media_metadata.side_effect = mock_get_media_metadata

        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        base_watched_at = timezone.now()
        for i in range(1, 6):  # Create 5 episodes
            episode_item = Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Test TV Show",
                image="http://example.com/image.jpg",
                season_number=1,
                episode_number=i,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=base_watched_at - timezone.timedelta(days=6 - i),
            )

        anime_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )
        Anime.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=10,
        )
        Event.objects.create(
            item=anime_item,
            content_number=16,
            datetime=timezone.now() - timezone.timedelta(days=1),
        )
        Event.objects.create(
            item=anime_item,
            content_number=17,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

        movie_item = Item.objects.create(
            media_id="10",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Planned Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Event.objects.create(
            item=season_item,
            content_number=6,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

    def rows_by_key(self, response):
        """Return the home rows of a response by status and media type."""
        return {
            (row["status"], row["media_type"]): row
            for row in response.context["home_rows"]
        }

    def test_home_view(self):
        """The home page has a row per status and media type with entries."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/home.html")

        rows = self.rows_by_key(response)
        self.assertEqual(
            list(rows),
            [
                (Status.IN_PROGRESS.value, MediaTypes.SEASON.value),
                (Status.IN_PROGRESS.value, MediaTypes.ANIME.value),
                (Status.PLANNING.value, MediaTypes.MOVIE.value),
            ],
        )

        season_row = rows[(Status.IN_PROGRESS.value, MediaTypes.SEASON.value)]
        self.assertEqual(season_row["total"], 1)
        self.assertEqual(season_row["items"][0].progress, 5)

    def test_row_headers_open_their_list_in_the_same_order(self):
        """Each row's header opens its media list filtered and ordered like the row.

        Seasons have no list of their own, so theirs opens their shows' list.
        """
        response = self.client.get(reverse("home"))

        for row in response.context["home_rows"]:
            is_season = row["media_type"] == MediaTypes.SEASON.value
            list_type = MediaTypes.TV.value if is_season else row["media_type"]
            link = (
                reverse("medialist", args=[self.user.username, list_type])
                + f"?status={quote(row['status'])}&sort=recent"
            )
            self.assertContains(response, f'href="{link}"')

            list_response = self.client.get(link)

            self.assertEqual(list_response.status_code, 200, link)
            if is_season:
                continue
            listed = [media.id for media in list_response.context["media_list"]]
            self.assertEqual(listed[: len(row["items"])], [m.id for m in row["items"]])

    def test_tv_list_can_be_sorted_by_recent(self):
        """Shows, whose last activity comes from their episodes, sort by Recent."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.TV.value]),
            {"sort": "recent"},
        )

        self.assertEqual(response.status_code, 200)

    def test_rows_list_recent_activity_first(self):
        """A row lists the most recently updated entries first."""
        quiet = Item.objects.create(
            media_id="quiet",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Zero Episodes Anime",
            image="http://example.com/image.jpg",
        )
        Anime.objects.create(
            item=quiet, user=self.user, status=Status.IN_PROGRESS.value
        )

        def first_anime():
            response = self.client.get(reverse("home"))
            row = self.rows_by_key(response)[
                (Status.IN_PROGRESS.value, MediaTypes.ANIME.value)
            ]
            return row["items"][0].item.media_id

        # The newest entry has no releases or progress, so only Recent puts it first
        self.assertEqual(first_anime(), "quiet")

        busy = Anime.objects.get(item__media_id="1", user=self.user)
        busy.progress = 11
        busy.save()

        self.assertEqual(first_anime(), "1")

    def test_paused_entries_have_no_row(self):
        """Only in progress and planned entries get rows."""
        paused = Item.objects.create(
            media_id="paused",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Paused Manga",
            image="http://example.com/image.jpg",
        )
        Manga.objects.create(item=paused, user=self.user, status=Status.PAUSED.value)

        response = self.client.get(reverse("home"))

        self.assertNotIn(
            MediaTypes.MANGA.value,
            [row["media_type"] for row in response.context["home_rows"]],
        )

    def test_empty_home_page(self):
        """Someone tracking nothing gets an empty state instead of rows."""
        new = {"username": "new", "password": self.credentials["password"]}
        get_user_model().objects.create_user(**new)
        self.client.login(**new)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.context["home_rows"], [])
        self.assertContains(response, "Nothing in progress or planned yet")

    def test_rows_hold_twenty_entries(self):
        """A row shows its first 20 entries and counts them all."""
        for i in range(25):
            item = Item.objects.create(
                media_id=f"many-{i}",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.ANIME.value,
                title=f"Anime {i}",
                image="http://example.com/image.jpg",
            )
            Anime.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

        response = self.client.get(reverse("home"))

        anime_row = self.rows_by_key(response)[
            (Status.IN_PROGRESS.value, MediaTypes.ANIME.value)
        ]
        self.assertEqual(len(anime_row["items"]), 20)
        self.assertEqual(anime_row["total"], 26)

    def test_unreleased_media_is_shown_with_the_old_hide_setting(self):
        """Unreleased media stays on the home page now that it can't be hidden."""
        self.user.home_hide_unreleased = True
        self.user.save(update_fields=["home_hide_unreleased"])
        upcoming_item = Item.objects.create(
            media_id="planning-with-release",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Upcoming Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=upcoming_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Event.objects.create(
            item=upcoming_item,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

        response = self.client.get(reverse("home"))

        movies = self.rows_by_key(response)[
            (Status.PLANNING.value, MediaTypes.MOVIE.value)
        ]
        self.assertIn(
            "planning-with-release",
            [media.item.media_id for media in movies["items"]],
        )

    def test_unreleased_card_offers_release_instead_of_action(self):
        """An unreleased planned movie shows its release date, not a status action."""
        upcoming_item = Item.objects.create(
            media_id="unreleased-card",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Unreleased Movie",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=upcoming_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        release = Event.objects.create(
            item=upcoming_item,
            datetime=timezone.now() + timezone.timedelta(days=30),
        )

        response = self.client.get(reverse("home"))

        card = response.content.decode().split(f'id="home-media-movie-{movie.id}"')[1]
        card = card.split('id="home-media-')[0]
        self.assertIn(reverse("download_event", args=[release.id]), card)
        self.assertNotIn("Mark watched", card)

    def test_movie_card_shows_its_release_year(self):
        """A movie card on the home page shows the year it came out."""
        released_item = Item.objects.create(
            media_id="released-card",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Released Movie",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=released_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Event.objects.create(
            item=released_item,
            datetime=timezone.now().replace(year=1999),
        )

        response = self.client.get(reverse("home"))

        card = response.content.decode().split(f'id="home-media-movie-{movie.id}"')[1]
        card = card.split('id="home-media-')[0]
        self.assertIn("<span>1999</span>", card)
