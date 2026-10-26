import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from django.test import TestCase, override_settings
from django.urls import reverse

from app import helpers
from app.helpers import (
    build_absolute_app_url,
    enrich_items_with_user_data,
    form_error_messages,
    get_configured_app_url,
    minutes_to_hhmm,
    redirect_back,
)
from app.models import Item, MediaTypes, Movie, Sources, Status


class HelpersTest(TestCase):
    """Test helper functions."""

    def test_minutes_to_hhmm(self):
        """Test conversion of minutes to HH:MM format."""
        # Test minutes only
        self.assertEqual(minutes_to_hhmm(30), "30min")

        # Test hours and minutes
        self.assertEqual(minutes_to_hhmm(90), "1h 30min")
        self.assertEqual(minutes_to_hhmm(125), "2h 05min")

        # Test zero
        self.assertEqual(minutes_to_hhmm(0), "0min")

    @override_settings(URLS=["https://yamtrack.example.com:8924"])
    def test_get_configured_app_url_from_urls(self):
        """Test configured public origin from URLS."""
        self.assertEqual(
            get_configured_app_url(),
            "https://yamtrack.example.com:8924",
        )

    @override_settings(URLS=["https://yamtrack.example.com"])
    def test_build_absolute_app_url_uses_configured_origin(self):
        """Test absolute URL construction behind reverse proxies."""
        request = MagicMock()

        result = build_absolute_app_url(request, "/import/trakt/private")

        self.assertEqual(result, "https://yamtrack.example.com/import/trakt/private")
        request.build_absolute_uri.assert_not_called()

    @override_settings(URLS=[])
    def test_build_absolute_app_url_falls_back_to_request(self):
        """Test request-based absolute URL construction without URLS."""
        request = MagicMock()
        request.build_absolute_uri.return_value = (
            "http://testserver/import/trakt/private"
        )

        result = build_absolute_app_url(request, "/import/trakt/private")

        self.assertEqual(result, "http://testserver/import/trakt/private")
        request.build_absolute_uri.assert_called_once_with("/import/trakt/private")

    @patch("app.helpers.url_has_allowed_host_and_scheme")
    @patch("app.helpers.HttpResponseRedirect")
    @patch("app.helpers.redirect")
    def test_redirect_back_with_next(self, _, mock_http_redirect, mock_url_check):
        """Test redirect_back with a 'next' parameter."""
        mock_url_check.return_value = True
        mock_http_redirect.return_value = "redirected"

        request = MagicMock()
        request.headers = {}
        request.GET = {"next": "http://example.com/path?page=2&sort=name"}

        result = redirect_back(request)

        # Check that we redirected to the URL without the page parameter
        mock_http_redirect.assert_called_once()
        redirect_url = mock_http_redirect.call_args[0][0]
        self.assertEqual(redirect_url, "http://example.com/path?sort=name")
        self.assertEqual(result, "redirected")

    @patch("app.helpers.url_has_allowed_host_and_scheme")
    @patch("app.helpers.redirect")
    def test_redirect_back_without_next(self, mock_redirect, mock_url_check):
        """Test redirect_back without a 'next' parameter."""
        mock_url_check.return_value = False
        mock_redirect.return_value = "home_redirect"

        request = MagicMock()
        request.headers = {}
        request.GET = {}

        result = redirect_back(request)

        mock_redirect.assert_called_once_with("home")
        self.assertEqual(result, "home_redirect")

    @patch("app.helpers.url_has_allowed_host_and_scheme")
    def test_redirect_back_htmx_with_next(self, mock_url_check):
        """HTMX requests get an HX-Location soft navigation instead of a reload."""
        mock_url_check.return_value = True

        request = MagicMock()
        request.headers = {"HX-Request": "true"}
        request.GET = {"next": "http://example.com/path?page=2&sort=name"}

        result = redirect_back(request)

        self.assertEqual(result.status_code, 204)
        location = json.loads(result["HX-Location"])
        self.assertEqual(location["path"], "http://example.com/path?sort=name")
        self.assertEqual(location["target"], "body")
        self.assertEqual(location["headers"]["X-Soft-Navigation"], "true")

    @patch("app.helpers.url_has_allowed_host_and_scheme")
    def test_redirect_back_htmx_without_next(self, mock_url_check):
        """HTMX requests without a valid 'next' fall back to the home page."""
        mock_url_check.return_value = False

        request = MagicMock()
        request.headers = {"HX-Request": "true"}
        request.GET = {}

        result = redirect_back(request)

        self.assertEqual(result.status_code, 204)
        location = json.loads(result["HX-Location"])
        self.assertEqual(location["path"], reverse("home"))

    @patch("app.helpers.messages")
    def test_form_error_messages(self, mock_messages):
        """Test form_error_messages function."""
        form = MagicMock()
        form.errors = {
            "title": ["This field is required."],
            "release_date": ["Enter a valid date."],
        }
        request = HttpRequest()

        form_error_messages(form, request)

        # Check that error messages were added
        self.assertEqual(mock_messages.error.call_count, 2)
        mock_messages.error.assert_any_call(request, "Title: This field is required.")
        mock_messages.error.assert_any_call(
            request,
            "Release Date: Enter a valid date.",
        )


class GetMediaRatingsTest(TestCase):
    """Test the ratings shown on details pages."""

    @patch("app.helpers.omdb.get_ratings")
    def test_source_rating_comes_first_then_omdb(self, mock_ratings):
        """Movies and shows add OMDb's ratings after their source's, with logos."""
        mock_ratings.return_value = [
            {"source": "imdb", "score": "8.7", "scale": "/10", "votes": 10},
        ]
        media = {
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "score": 8.3,
            "score_count": 29063,
            "external_links": {"IMDb": "https://www.imdb.com/title/tt1/"},
        }

        ratings = helpers.get_media_ratings(media)

        self.assertEqual([r["source"] for r in ratings], ["tmdb", "imdb"])
        self.assertEqual(ratings[0]["score"], 8.3)
        self.assertEqual(ratings[0]["icon"], "app/icons/sources/tmdb.svg")
        self.assertEqual(ratings[1]["label"], "IMDb")
        mock_ratings.assert_called_once_with(media["external_links"])

    @patch("app.helpers.omdb.get_ratings")
    def test_other_media_has_only_its_source_rating(self, mock_ratings):
        """Media other than movies and shows isn't looked up on OMDb."""
        media = {
            "source": Sources.HARDCOVER.value,
            "media_type": MediaTypes.BOOK.value,
            "score": 4.1,
            "score_count": 12,
        }

        ratings = helpers.get_media_ratings(media)

        self.assertEqual(len(ratings), 1)
        # Sources without a logo show a star
        self.assertEqual(ratings[0]["icon"], "app/icons/star.svg")
        self.assertEqual(ratings[0]["label"], "Hardcover")
        mock_ratings.assert_not_called()

    @patch("app.helpers.omdb.get_ratings")
    def test_rotten_tomatometer_shows_a_splat(self, mock_ratings):
        """Rotten Tomatoes scores under 60% show the splat, others the tomato."""
        media = {"source": Sources.TMDB.value, "media_type": MediaTypes.MOVIE.value}

        icons = []
        for score in ("59%", "60%"):
            mock_ratings.return_value = [
                {
                    "source": "rotten_tomatoes",
                    "score": score,
                    "scale": "",
                    "votes": None,
                },
            ]
            icons.append(helpers.get_media_ratings(media)[0]["icon"])

        self.assertEqual(
            icons,
            [
                "app/icons/sources/rotten_tomatoes_rotten.svg",
                "app/icons/sources/rotten_tomatoes.svg",
            ],
        )

    def test_no_rating_without_votes(self):
        """Media whose source has no rating has no tile."""
        media = {"source": Sources.MANUAL.value, "media_type": MediaTypes.BOOK.value}
        self.assertEqual(helpers.get_media_ratings(media), [])


class GetCertificationTest(TestCase):
    """Test the age rating shown on details pages."""

    def test_region_rating_falls_back_to_the_us(self):
        """The user's region is used, else the US rating, else none."""
        media = {"certifications": {"US": "R", "GB": "15"}}

        self.assertEqual(helpers.get_certification(media, "GB"), "15")
        self.assertEqual(helpers.get_certification(media, "DE"), "R")
        self.assertEqual(helpers.get_certification(media, "UNSET"), "R")
        self.assertIsNone(helpers.get_certification({}, "GB"))


class EnrichItemsWithUserDataTest(TestCase):
    """Test the enrich_items_with_user_data function."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "testpass"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.request = MagicMock()
        self.request.user = self.user

        # Create test items in the database
        self.movie_item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/movie.jpg",
        )

        self.season_item = Item.objects.create(
            media_id="67890",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            image="http://example.com/show.jpg",
            season_number=1,
        )

        # Create user tracking data for the movie
        self.movie_media = Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
        )

    def test_enrich_items_with_user_data(self):
        """Test enriching items with multiple scenarios."""
        raw_items = [
            # Scenario 1: Existing movie with user tracking data
            {
                "media_id": "238",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "title": "Test Movie",
                "image": "http://example.com/movie.jpg",
                "release_date": "2023-01-01",
                "rating": 8.5,
                "genre": "Action",
            },
            # Scenario 2: Existing season without user tracking data
            {
                "media_id": "67890",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "title": "Test TV Show",
                "season_title": "Season 1",
                "season_number": 1,
                "image": "http://example.com/show.jpg",
            },
            # Scenario 3: Non-existent item (raw data only)
            {
                "media_id": "99999",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "title": "Unknown Movie",
                "image": "http://example.com/unknown.jpg",
                "description": "This movie doesn't exist in our database",
            },
        ]

        enriched_items = enrich_items_with_user_data(self.request, raw_items, "test")
        self.assertEqual(len(enriched_items), 3)

        # Scenario 1: Existing movie with user tracking data
        movie_enriched = enriched_items[0]
        self.assertEqual(movie_enriched["media"], self.movie_media)
        self.assertEqual(movie_enriched["item"]["title"], "Test Movie")
        self.assertEqual(movie_enriched["item"]["media_id"], "238")
        # Verify additional properties are preserved
        self.assertEqual(movie_enriched["item"]["release_date"], "2023-01-01")
        self.assertEqual(movie_enriched["item"]["rating"], 8.5)
        self.assertEqual(movie_enriched["item"]["genre"], "Action")

        # Scenario 2: Existing season without user tracking data
        season_enriched = enriched_items[1]
        self.assertEqual(
            season_enriched["media"],
            None,
        )  # No user tracking for this season
        self.assertEqual(
            season_enriched["item"]["season_title"],
            "Season 1",
        )  # Should use season_title
        self.assertEqual(season_enriched["item"]["season_number"], 1)

        # Scenario 3: Non-existent movie (raw data)
        unknown_movie_enriched = enriched_items[2]
        self.assertEqual(
            unknown_movie_enriched["item"]["media_id"],
            raw_items[2]["media_id"],
        )
        self.assertEqual(unknown_movie_enriched["media"], None)
        self.assertEqual(unknown_movie_enriched["item"]["title"], "Unknown Movie")
        self.assertEqual(unknown_movie_enriched["item"]["media_id"], "99999")
        self.assertEqual(
            unknown_movie_enriched["item"]["description"],
            "This movie doesn't exist in our database",
        )

    def test_hide_completed_recommendations_enabled(self):
        """Test that completed items are hidden when preference is enabled."""
        self.user.hide_completed_recommendations = True
        self.user.save()

        raw_items = [
            {
                "media_id": "238",  # This is our completed movie
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "title": "Test Movie",
                "image": "http://example.com/movie.jpg",
            },
            {
                "media_id": "99999",  # Not tracked
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "title": "Unknown Movie",
                "image": "http://example.com/unknown.jpg",
            },
        ]

        # When section is "recommendations", completed items should be hidden
        enriched_items = enrich_items_with_user_data(
            self.request, raw_items, "recommendations"
        )
        self.assertEqual(len(enriched_items), 1)
        self.assertEqual(enriched_items[0]["item"]["media_id"], "99999")

    def test_hide_completed_recommendations_disabled(self):
        """Test that completed items are shown when preference is disabled."""
        self.user.hide_completed_recommendations = False
        self.user.save()

        raw_items = [
            {
                "media_id": "238",  # This is our completed movie
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "title": "Test Movie",
                "image": "http://example.com/movie.jpg",
            },
            {
                "media_id": "99999",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "title": "Unknown Movie",
                "image": "http://example.com/unknown.jpg",
            },
        ]

        # With preference disabled, all items should be returned
        enriched_items = enrich_items_with_user_data(
            self.request, raw_items, "recommendations"
        )
        self.assertEqual(len(enriched_items), 2)
