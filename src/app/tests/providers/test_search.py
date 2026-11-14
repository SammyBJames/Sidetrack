from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from app.models import MediaTypes, Sources
from app.providers import (
    hardcover,
    igdb,
    mal,
    mangaupdates,
    openlibrary,
    tmdb,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"

BREAKING_BAD_TMDB_ID = 1396
THE_MATRIX_TMDB_ID = 603


class Search(TestCase):
    """Test the external API calls for media search."""

    def test_anime(self):
        """Test the search method for anime.

        Assert that all required keys are present in each entry.
        """
        response = mal.search(MediaTypes.ANIME.value, "Cowboy Bebop", 1)

        required_keys = {"media_id", "media_type", "title", "image"}

        for anime in response["results"]:
            self.assertTrue(all(key in anime for key in required_keys))

    def test_anime_not_found(self):
        """Test the search method for anime with no results."""
        response = mal.search(MediaTypes.ANIME.value, "q", 1)

        self.assertEqual(response["results"], [])

    @patch("app.providers.mal.cache")
    @patch("app.providers.services.api_request")
    def test_mal_total_is_made_up_unless_there_is_no_next_page(
        self,
        mock_request,
        mock_cache,
    ):
        """A MAL page with a next page has a made-up total that still pages.

        One without is the last, even when short, and its total is exact.
        """
        mock_cache.get.return_value = None

        def respond(count, *, next_page):
            """Return a MAL response of `count` entries."""
            return {
                "data": [{"node": {"id": n, "title": f"T{n}"}} for n in range(count)],
                "paging": {"next": "https://api.myanimelist.net/next"}
                if next_page
                else {"previous": "https://api.myanimelist.net/previous"},
            }

        with patch("app.providers.mal.settings.PER_PAGE", 3):
            mock_request.return_value = respond(3, next_page=True)
            full = mal.search(MediaTypes.ANIME.value, "dune", 1)
            # Adult titles filtered after paging leave a short page with more after
            mock_request.return_value = respond(2, next_page=True)
            short_with_next = mal.search(MediaTypes.ANIME.value, "dune", 1)
            mock_request.return_value = respond(2, next_page=False)
            last = mal.search(MediaTypes.ANIME.value, "dune", 2)

        self.assertTrue(full["total_estimated"])
        self.assertGreater(full["total_pages"], 1)
        self.assertTrue(short_with_next["total_estimated"])
        self.assertGreater(short_with_next["total_pages"], 1)
        self.assertFalse(last["total_estimated"])
        self.assertEqual(last["total_results"], 5)
        self.assertEqual(last["total_pages"], 2)

    def test_mangaupdates(self):
        """Test the search method for manga.

        Assert that all required keys are present in each entry.
        """
        response = mangaupdates.search("One Piece", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for manga in response["results"]:
            self.assertTrue(all(key in manga for key in required_keys))

    def test_manga_not_found(self):
        """Test the search method for manga with no results."""
        response = mangaupdates.search("", 1)

        self.assertEqual(response["results"], [])

    @patch("app.providers.mangaupdates.services.api_request")
    def test_mangaupdates_nsfw_setting(self, mock_api_request):
        """Test MangaUpdates adult genres follow MU_NSFW, not MAL_NSFW."""
        mock_api_request.return_value = {"results": [], "total_hits": 0}

        with self.settings(MU_NSFW=False, MAL_NSFW=True):
            mangaupdates.search("mu nsfw disabled", 1)
        params = mock_api_request.call_args.kwargs["params"]
        self.assertIn("Hentai", params["exclude_genre"])

        with self.settings(MU_NSFW=True, MAL_NSFW=False):
            mangaupdates.search("mu nsfw enabled", 1)
        params = mock_api_request.call_args.kwargs["params"]
        self.assertNotIn("exclude_genre", params)

    def test_tv(self):
        """Test the search method for TV shows.

        Assert that all required keys are present in each entry.
        """
        response = tmdb.search(MediaTypes.TV.value, "Breaking Bad", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for tv in response["results"]:
            self.assertTrue(all(key in tv for key in required_keys))

        breaking_bad = next(
            tv for tv in response["results"] if tv["media_id"] == BREAKING_BAD_TMDB_ID
        )
        self.assertEqual(breaking_bad["release_year"], 2008)

    def test_movie(self):
        """Test the search method for movies.

        Assert that all required keys are present in each entry.
        """
        response = tmdb.search(MediaTypes.MOVIE.value, "The Matrix", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for movie in response["results"]:
            self.assertTrue(all(key in movie for key in required_keys))

        the_matrix = next(
            movie
            for movie in response["results"]
            if movie["media_id"] == THE_MATRIX_TMDB_ID
        )
        self.assertEqual(the_matrix["release_year"], 1999)

    def test_tmdb_release_year(self):
        """Test the release year is read from movie and TV date fields."""
        self.assertEqual(tmdb.get_release_year({"release_date": "1999-03-31"}), 1999)
        self.assertEqual(
            tmdb.get_release_year({"first_air_date": "2008-01-20"}),
            2008,
        )

    def test_tmdb_release_year_unknown(self):
        """Test the release year is None when the date is empty or missing."""
        self.assertIsNone(tmdb.get_release_year({"release_date": ""}))
        self.assertIsNone(tmdb.get_release_year({"first_air_date": ""}))
        self.assertIsNone(tmdb.get_release_year({}))

    def test_games(self):
        """Test the search method for games.

        Assert that all required keys are present in each entry.
        """
        response = igdb.search("Persona 5", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for game in response["results"]:
            self.assertTrue(all(key in game for key in required_keys))

    def test_books(self):
        """Test the search method for books.

        Assert that all required keys are present in each entry.
        """
        response = openlibrary.search("The Name of the Wind", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for book in response["results"]:
            self.assertTrue(all(key in book for key in required_keys))

    def test_comics(self):
        """Test the search method for comics.

        Assert that all required keys are present in each entry.
        """
        response = igdb.search("Batman", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for comic in response["results"]:
            self.assertTrue(all(key in comic for key in required_keys))

    def test_hardcover(self):
        """Test the search method for books from Hardcover.

        Assert that all required keys are present in each entry.
        """
        response = hardcover.search("1984 George Orwell", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        self.assertTrue(len(response["results"]) > 0)

        for book in response["results"]:
            self.assertTrue(all(key in book for key in required_keys))

    def test_hardcover_not_found(self):
        """Test the search method for books from Hardcover with no results."""
        response = hardcover.search("xjkqzptmvnsieurytowahdbfglc", 1)
        self.assertEqual(response["results"], [])

    @patch("app.providers.hardcover.services.api_request")
    def test_hardcover_title_query_is_capped(self, mock_api_request):
        """Test the long title is capped before search."""
        query = (
            "The Short Story of Architecture: A Pocket Guide to Key Styles, "
            "Buildings, Elements & Materials (Architectural History Introduction, "
            "A Guide to Architecture)"
        )
        capped_query = "The Short Story of Architecture: A Pocket Guide to"
        cache.delete(
            f"search_{Sources.HARDCOVER.value}_{MediaTypes.BOOK.value}_"
            f"{capped_query}_1",
        )
        mock_api_request.return_value = {
            "data": {
                "search": {
                    "results": {
                        "hits": [
                            {
                                "document": {
                                    "id": "123",
                                    "title": "The Short Story of Architecture",
                                    "image": {"url": "https://example.com/cover.jpg"},
                                },
                            },
                        ],
                        "found": 1,
                    },
                },
            },
        }

        response = hardcover.search(query, 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        self.assertEqual(len(query), 156)
        self.assertEqual(hardcover.cap_search_query(query), capped_query)
        _, kwargs = mock_api_request.call_args
        self.assertEqual(kwargs["params"]["variables"]["query"], capped_query)
        self.assertTrue(len(response["results"]) > 0)

        for book in response["results"]:
            self.assertTrue(all(key in book for key in required_keys))

    def test_hardcover_title_query_cap_stops_at_word_boundary(self):
        """Test the long title cap does not split words."""
        query = "one two three four five six seven eight nine ten eleven twelve"

        self.assertEqual(
            hardcover.cap_search_query(query),
            "one two three four five six seven eight nine ten",
        )
