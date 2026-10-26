from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app import config
from app.models import (
    Book,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)


class MediaDetailsViewTests(TestCase):
    """Test the media details views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_view(self, mock_get_metadata):
        """Test the media details view."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Test Movie")

        mock_get_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "238",
            Sources.TMDB.value,
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_view(self, mock_process_episodes, mock_get_metadata):
        """Test the season details view."""
        self.user.obfuscate_unseen_episodes = True
        self.user.save(update_fields=["obfuscate_unseen_episodes"])

        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "episodes": [],
            },
        }

        mock_process_episodes.return_value = [
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "season_number": 1,
                "episode_number": 1,
                "title": "Episode 1",
                "name": "Episode 1",
                "air_date": "2023-01-01",
                "watched": False,
            },
        ]

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Season 1")
        self.assertEqual(len(response.context["media"]["episodes"]), 1)
        # The unwatched episode's title is blurred
        self.assertRegex(
            response.content.decode(),
            r'<h3 class="[^"]*line-clamp-1[^"]*\bblur"',
        )

        mock_get_metadata.assert_called_once_with(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [1],
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_refreshes_missing_item_image(self, mock_get_metadata):
        """Item.image is updated when missing and live metadata has one."""
        live_image = "http://example.com/fresh.jpg"
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": live_image,
            "max_progress": 1,
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image=settings.IMG_NONE,
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.image, live_image)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_keeps_existing_item_image(self, mock_get_metadata):
        """Item.image is left alone when already set."""
        existing_image = "http://example.com/stored.jpg"
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/fresh.jpg",
            "max_progress": 1,
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image=existing_image,
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.image, existing_image)

    def _get_tracked_movie_details(self, mock_get_metadata, status):
        """Return the details page of a movie tracked with the given status."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(item=item, user=self.user, status=status)

        return self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_track_button_uses_status_color(self, mock_get_metadata):
        """The track button is filled with the status color of the tracked media."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.DROPPED.value,
        )

        self.assertContains(
            response,
            config.get_status_button_color(Status.DROPPED.value),
            count=1,
        )
        # Nothing follows dropped, so the status opens the form instead
        self.assertNotContains(response, '"card": "details"')

    @patch("app.providers.services.get_media_metadata")
    def test_watched_movie_plans_a_rewatch(self, mock_get_metadata):
        """A watched movie's button plans a rewatch, in the rewatch color."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.COMPLETED.value,
        )

        self.assertContains(response, '"action": "plan_repeat", "card": "details"')
        self.assertContains(
            response,
            config.get_status_button_color(Status.PLANNING_REPEAT.value),
        )
        self.assertRegex(
            response.content.decode(),
            r"</svg>\s*Plan to rewatch\s*</span>",
        )
        # Until hovered, it shows the movie is watched
        self.assertRegex(response.content.decode(), r"</svg>\s*Watched\s*</span>")

    @patch("app.providers.services.get_media_metadata")
    def test_watched_movie_counts_its_plays(self, mock_get_metadata):
        """A movie watched more than once says how often."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.COMPLETED.value,
        )
        first = Movie.objects.get(user=self.user)
        Movie.objects.create(
            item=first.item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        response = self.client.get(response.wsgi_request.path)

        self.assertRegex(
            response.content.decode(),
            r"</svg>\s*Watched 2 times\s*</span>",
        )

    @patch("app.providers.services.get_media_metadata")
    def test_movie_lists_all_its_watches(self, mock_get_metadata):
        """A movie lists every watch, the first too, under Your Watches."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.COMPLETED.value,
        )
        first = Movie.objects.get(user=self.user)
        second = Movie.objects.create(
            item=first.item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        response = self.client.get(response.wsgi_request.path)

        content = response.content.decode()
        self.assertRegex(content, r"Your Watches</h2>\s*<span[^>]*>2</span>")
        self.assertNotIn("Repeats", content)
        for movie in (first, second):
            score_url = reverse(
                "update_media_score",
                args=[MediaTypes.MOVIE.value, movie.id],
            )
            self.assertIn(f"ratingStars(null, '{score_url}'", content)

    @patch("app.providers.services.get_media_metadata")
    def test_book_lists_all_its_reads(self, mock_get_metadata):
        """A book lists every read, the current one too, under Your Reads."""
        mock_get_metadata.return_value = {
            "media_id": "377193",
            "title": "Test Book",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.HARDCOVER.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 300,
            "synopsis": "Test synopsis",
        }
        item = Item.objects.create(
            media_id="377193",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Test Book",
            image="http://example.com/image.jpg",
        )
        Book.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=300,
        )
        Book.objects.create(
            item=item,
            user=self.user,
            status=Status.REPEATING.value,
            progress=120,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.HARDCOVER.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "377193",
                    "title": "test-book",
                },
            ),
        )

        content = response.content.decode()
        self.assertRegex(content, r"Your Reads</h2>\s*<span[^>]*>2</span>")
        self.assertNotIn("Repeats", content)
        # Each read shows how far it got, the one in progress named as a reread
        self.assertRegex(content, r"120\s*pages")
        self.assertRegex(content, r"300\s*pages")
        self.assertIn("Rereading", content)

    @patch("app.providers.services.get_media_metadata")
    def test_planned_movie_has_no_watches(self, mock_get_metadata):
        """A movie that's only planned has no watches to list."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.PLANNING.value,
        )

        self.assertNotContains(response, "Your Watches")

    @patch("app.providers.services.get_media_metadata")
    def test_track_button_moves_to_the_next_status(self, mock_get_metadata):
        """The track button shows the one-click action, in its status's color."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.PLANNING.value,
        )

        self.assertContains(response, '"action": "complete", "card": "details"')
        self.assertContains(response, "Mark watched")
        # The action shows from the start, without the current status
        self.assertNotContains(
            response,
            config.get_status_button_color(Status.PLANNING.value),
        )
        self.assertContains(
            response,
            config.get_status_button_color(Status.COMPLETED.value),
            count=1,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_backdrop_and_logo_are_shown(self, mock_get_metadata):
        """A backdrop goes behind the page and a logo replaces the text title."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "backdrop": "http://example.com/backdrop.jpg",
            "logo": "http://example.com/logo.png",
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertContains(response, 'src="http://example.com/backdrop.jpg"')
        self.assertContains(response, 'src="http://example.com/logo.png"')
        # The title is only read out, behind the logo
        self.assertContains(response, '<span class="sr-only">Test Movie</span>')
        self.assertNotContains(response, ">Test Movie</h1>")

    @patch("app.providers.services.get_media_metadata")
    def test_track_button_color_ignores_progress_bar_setting(self, mock_get_metadata):
        """The track button uses the next status's color with the progress bar off."""
        self.user.progress_bar = False
        self.user.save(update_fields=["progress_bar"])

        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.PAUSED.value,
        )

        # Paused movies resume
        self.assertContains(
            response,
            config.get_status_button_color(Status.IN_PROGRESS.value),
            count=1,
        )
        self.assertNotContains(
            response,
            config.get_status_button_color(Status.PAUSED.value),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_track_button_has_no_progress_bar(self, mock_get_metadata):
        """The track button does not render a progress bar."""
        response = self._get_tracked_movie_details(
            mock_get_metadata,
            Status.IN_PROGRESS.value,
        )

        self.assertNotContains(response, "h-1.25")

    @patch("app.providers.services.get_media_metadata")
    def test_ratings_show_their_source_and_the_users_stars(self, mock_get_metadata):
        """The age rating and ratings show, and tracked media has five stars to rate.

        The age rating is the US one, as the user hasn't set a region.
        """
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "score": 8.3,
            "score_count": 1200,
            "certifications": {"US": "PG-13", "GB": "12A"},
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            score=7,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertContains(response, 'data-tooltip="TMDB · 1,200 votes"')
        self.assertContains(response, 'title="Rated PG-13">PG-13</span>')
        self.assertContains(response, "Your rating")
        self.assertContains(response, "ratingStars(7.0, ")
        # Two halves for each of the five stars, a point out of ten apiece
        for score in range(1, 11):
            self.assertContains(response, f'aria-label="Rate {score} out of 10"')
            self.assertContains(response, f'@click="setRating({score})"')

    @patch("app.providers.services.get_media_metadata")
    def test_people_videos_and_related_media_are_rows(self, mock_get_metadata):
        """Cast, crew, videos and related media each scroll in a row of their own."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/movie/238",
            "image": "http://example.com/image.jpg",
            "cast": [{"id": 1, "name": "Al Pacino", "role": "Michael", "image": ""}],
            "total_cast_count": 31,
            "crew": [{"id": 2, "name": "Coppola", "role": "Director", "image": ""}],
            "total_crew_count": 1,
            "videos": [
                {"key": "abc123", "name": "Official Trailer", "type": "Trailer"}
            ],
            "related": {
                "recommendations": [
                    {
                        "media_id": "240",
                        "source": Sources.TMDB.value,
                        "media_type": MediaTypes.MOVIE.value,
                        "title": "Part II",
                        "image": "http://example.com/2.jpg",
                    },
                ],
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        content = response.content.decode()
        for title in ("Cast", "Crew", "Videos", "Recommendations"):
            self.assertRegex(
                content,
                rf'x-data="scrollRow"[^<]*>\s*<div[^>]*>\s*<h2[^>]*>{title}</h2>',
            )
        self.assertContains(response, "Al Pacino")
        self.assertContains(response, "Director")
        self.assertContains(response, "@click=\"playing = 'abc123'\"")
        self.assertContains(response, "Part II")
        # More cast than shown links to all of them, and the whole crew shows
        self.assertContains(response, "31 cast members")
        self.assertNotContains(response, "crew members")
