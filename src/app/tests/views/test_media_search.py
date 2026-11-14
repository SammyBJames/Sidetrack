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


def _page(page, total_pages, count=2, media_type=MediaTypes.MOVIE.value):
    """Return a search response for one page of `count` results."""
    return {
        "page": page,
        "total_results": total_pages * count,
        "total_pages": total_pages,
        "results": [
            {
                "media_id": str(page * 100 + n),
                "title": f"Result {page}-{n}",
                "media_type": media_type,
                "source": Sources.TMDB.value,
                "image": "http://example.com/image.jpg",
            }
            for n in range(count)
        ],
    }


class MediaSearchPageTests(TestCase):
    """Test the results page's header, scrolling pages and empty state."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.search")
    def test_header_shows_query_and_count(self, mock_search):
        """The title names the query and the count of results sits beside it."""
        mock_search.return_value = _page(1, 1, count=2)

        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune"},
        ).content.decode()

        self.assertIn("Results for “dune”", html)
        self.assertIn('text-gray-400">2</span>', html)
        self.assertNotIn("hx-get=", html.split("<main")[-1].split("</main>")[0])

    @patch("app.providers.services.search")
    def test_source_switch_only_for_types_with_several_sources(self, mock_search):
        """Manga links to its other source; movies have no switch."""
        mock_search.return_value = _page(1, 1, media_type=MediaTypes.MANGA.value)
        manga = self.client.get(
            reverse("search"),
            {"media_type": "manga", "q": "berserk", "layout": "list"},
        ).content.decode()
        self.assertIn('aria-label="Source"', manga)
        self.assertIn("source=mangaupdates", manga)
        self.assertIn("layout=list", manga)
        self.assertIn("MangaUpdates", manga)

        mock_search.return_value = _page(1, 1)
        movie = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune"},
        ).content.decode()
        self.assertNotIn('aria-label="Source"', movie)

    @patch("app.providers.services.search")
    def test_more_pages_end_in_a_sentinel_in_both_layouts(self, mock_search):
        """The last item is followed by a sentinel for the next page."""
        mock_search.side_effect = lambda *_: _page(1, 3)
        for layout in ("grid", "list"):
            html = self.client.get(
                reverse("search"),
                {
                    "media_type": "manga",
                    "q": "dune",
                    "source": "mangaupdates",
                    "layout": layout,
                },
            ).content.decode()
            self.assertEqual(html.count('hx-trigger="intersect once"'), 1)
            self.assertLess(html.index("Result 1-1"), html.index("intersect once"))
            self.assertIn(
                "/search?q=dune&amp;media_type=manga&amp;source=mangaupdates"
                f"&amp;layout={layout}&amp;page=2",
                html,
            )

    @patch("app.providers.services.search")
    def test_htmx_page_returns_only_items_and_next_sentinel(self, mock_search):
        """A further page is its items and the next sentinel; the last has none."""
        mock_search.return_value = _page(2, 3)
        response = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune", "page": 2},
            headers={"HX-Request": "true"},
        )
        html = response.content.decode()
        self.assertTemplateNotUsed(response, "app/search.html")
        self.assertNotIn("<html", html)
        self.assertIn("Result 2-0", html)
        self.assertIn("page=3", html)
        mock_search.assert_called_once_with("movie", "dune", 2, Sources.TMDB.value)

        mock_search.return_value = _page(3, 3)
        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune", "page": 3},
            headers={"HX-Request": "true"},
        ).content.decode()
        self.assertIn("Result 3-0", html)
        self.assertNotIn("hx-get", html)

    @patch("app.providers.services.search")
    def test_no_results_shows_the_empty_state(self, mock_search):
        """An empty search shows the dashed box naming the query."""
        mock_search.return_value = _page(1, 0, count=0)

        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "zzzz"},
        ).content.decode()

        self.assertIn("border-dashed", html)
        self.assertIn("No matches for “zzzz”", html)
        self.assertNotIn("media-grid", html)

    @patch("app.providers.services.search")
    def test_query_is_urlencoded_in_links_and_escaped_in_text(self, mock_search):
        """`&` and quotes in the query don't break links or inject markup."""
        mock_search.return_value = _page(1, 2, media_type=MediaTypes.MANGA.value)
        query = 'a&b "c" <i>'

        html = self.client.get(
            reverse("search"),
            {"media_type": "manga", "q": query, "layout": "list"},
        ).content.decode()

        encoded = "a%26b+%22c%22+%3Ci%3E"
        self.assertIn("Results for “a&amp;b &quot;c&quot; &lt;i&gt;”", html)
        self.assertNotIn("<i>”", html)
        self.assertEqual(html.count(f"q={encoded}"), 5)
        self.assertNotIn("q=a&b", html)
        self.assertNotIn("q=a&amp;b", html)

    @patch("app.providers.services.search")
    def test_empty_page_past_the_end_has_no_sentinel(self, mock_search):
        """A page with no results ends the scroll even if total_pages says more."""
        mock_search.return_value = {**_page(5, 9, count=0), "total_pages": 9}

        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune", "page": 5},
            headers={"HX-Request": "true"},
        ).content.decode()

        self.assertNotIn("hx-get", html)

    @patch("app.providers.services.search")
    def test_soft_navigation_request_gets_the_full_page(self, mock_search):
        """An HX-Request that is a soft navigation swaps the body, so it is full."""
        mock_search.return_value = _page(1, 2)

        response = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune"},
            headers={"HX-Request": "true", "X-Soft-Navigation": "true"},
        )

        self.assertTemplateUsed(response, "app/search.html")

    @patch("app.providers.services.search")
    def test_no_results_hides_the_count(self, mock_search):
        """With nothing found there is no count beside the title."""
        mock_search.return_value = _page(1, 0, count=0)

        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "zzzz"},
        ).content.decode()

        self.assertNotIn('text-gray-400">0</span>', html)
