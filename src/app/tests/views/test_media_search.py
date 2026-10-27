from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Book,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)


class MediaSearchViewTests(TestCase):
    """Test the media search view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.search")
    def test_media_search_view(self, mock_search):
        """Test the media search view."""
        mock_search.return_value = {
            "page": 1,
            "total_results": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": "238",
                    "title": "Test Movie",
                    "media_type": MediaTypes.MOVIE.value,
                    "source": Sources.TMDB.value,
                    "image": "http://example.com/image.jpg",
                },
            ],
        }

        response = self.client.get(
            reverse("search") + "?media_type=movie&q=test",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/search.html")

        self.user.refresh_from_db()
        self.assertEqual(self.user.last_search_type, MediaTypes.MOVIE.value)

        mock_search.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "test",
            1,
            Sources.TMDB.value,
        )

    @patch("app.providers.services.search")
    def test_list_layout_shows_rows_for_tracked_and_untracked_results(
        self,
        mock_search,
    ):
        """The list layout is the library's rows, with an add action when untracked."""
        tracked = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Tracked Movie",
            image="http://example.com/1.jpg",
        )
        Movie.objects.create(item=tracked, user=self.user, status=Status.PLANNING.value)
        mock_search.return_value = {
            "page": 1,
            "total_results": 2,
            "total_pages": 1,
            "results": [
                {
                    "media_id": media_id,
                    "title": title,
                    "media_type": MediaTypes.MOVIE.value,
                    "source": Sources.TMDB.value,
                    "image": "http://example.com/image.jpg",
                    "release_year": year,
                }
                for media_id, title, year in (
                    ("1", "Tracked Movie", 2001),
                    ("2", "New Movie", 2002),
                )
            ],
        }

        response = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "movie", "layout": "list"},
        )

        self.assertTemplateUsed(response, "app/components/media_row.html")
        self.assertTemplateNotUsed(response, "app/components/media_card_list.html")
        html = response.content.decode()
        tracked_row, new_row = html.split('class="media-row')[1:]
        self.assertIn(">Tracked Movie</a>", tracked_row)
        self.assertIn("Planning", tracked_row)
        self.assertIn('"action": "complete"', tracked_row)
        self.assertIn(">New Movie</a>", new_row)
        self.assertIn("<span>2001</span>", tracked_row)
        self.assertIn("<span>2002</span>", new_row)
        self.assertIn('"action": "plan"', new_row)
        # Dashes for its empty status and rating
        self.assertEqual(new_row.count('text-gray-600">—'), 2)
        # Nothing of the library's filter
        self.assertNotIn("filter-form", html)

    @patch("app.providers.services.search")
    def test_list_layout_shows_a_started_items_progress(self, mock_search):
        """A started book's row shows its progress, as search has no progress column."""
        item = Item.objects.create(
            media_id="1",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Started Book",
            image="http://example.com/1.jpg",
        )
        # Without save, which fetches the book's metadata
        Book.objects.bulk_create(
            [
                Book(
                    item=item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=123,
                ),
            ],
        )
        mock_search.return_value = {
            "page": 1,
            "total_results": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": "1",
                    "title": "Started Book",
                    "media_type": MediaTypes.BOOK.value,
                    "source": Sources.HARDCOVER.value,
                    "image": "http://example.com/1.jpg",
                    "release_year": 2001,
                },
            ],
        }

        response = self.client.get(
            reverse("search"),
            {"media_type": "book", "q": "book", "layout": "list"},
        )

        row = response.content.decode().split('class="media-row')[1]
        self.assertIn("123", row)
        self.assertIn("&nbsp;Pages", row)
