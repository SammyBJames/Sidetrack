from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings

from app.providers import omdb

LINKS = {"IMDb": "https://www.imdb.com/title/tt0133093/"}
RESPONSE = {
    "Response": "True",
    "imdbVotes": "2,284,373",
    "Ratings": [
        {"Source": "Metacritic", "Value": "73/100"},
        {"Source": "Internet Movie Database", "Value": "8.7/10"},
        {"Source": "Rotten Tomatoes", "Value": "83%"},
    ],
}


def ok(data):
    """Return a stand-in for a successful OMDb response with this JSON."""
    response = MagicMock()
    response.json.return_value = data
    return response


@override_settings(OMDB_API="key")
class OmdbTests(TestCase):
    """Test the IMDb, Rotten Tomatoes and Metacritic ratings from OMDb."""

    def setUp(self):
        """Start without cached ratings."""
        cache.clear()

    @patch("app.providers.omdb.services.session.get")
    def test_ratings_are_read_in_order(self, mock_request):
        """Each source's score is split from its scale, IMDb first with its votes."""
        mock_request.return_value = ok(RESPONSE)

        self.assertEqual(
            omdb.get_ratings(LINKS),
            [
                {"source": "imdb", "score": "8.7", "scale": "/10", "votes": 2284373},
                {
                    "source": "rotten_tomatoes",
                    "score": "83%",
                    "scale": "",
                    "votes": None,
                },
                {"source": "metacritic", "score": "73", "scale": "/100", "votes": None},
            ],
        )
        self.assertEqual(mock_request.call_args.kwargs["params"]["i"], "tt0133093")
        # Ratings load with the page, so OMDb isn't waited on for long
        self.assertEqual(mock_request.call_args.kwargs["timeout"], omdb.TIMEOUT)

    @patch("app.providers.omdb.services.session.get")
    def test_ratings_are_cached(self, mock_request):
        """A title's ratings are fetched once."""
        mock_request.return_value = ok(RESPONSE)

        omdb.get_ratings(LINKS)
        omdb.get_ratings(LINKS)

        mock_request.assert_called_once()

    @override_settings(OMDB_API="")
    @patch("app.providers.omdb.services.session.get")
    def test_no_ratings_without_a_key(self, mock_request):
        """Nothing is fetched until a key is set."""
        self.assertEqual(omdb.get_ratings(LINKS), [])
        mock_request.assert_not_called()

    @patch("app.providers.omdb.services.session.get")
    def test_no_ratings_without_an_imdb_link(self, mock_request):
        """Titles are found by their IMDb ID, so without one there's nothing."""
        self.assertEqual(omdb.get_ratings({"TVDB": "https://thetvdb.com/1"}), [])
        self.assertEqual(omdb.get_ratings(None), [])
        mock_request.assert_not_called()

    @patch("app.providers.omdb.services.session.get")
    def test_errors_leave_the_ratings_out_for_a_while(self, mock_request):
        """A failed request shows no extra ratings, and isn't retried every page."""
        error_response = MagicMock(status_code=401)
        mock_request.return_value.raise_for_status.side_effect = (
            requests.exceptions.HTTPError(
                "401 Client Error: Unauthorized for url: "
                "https://www.omdbapi.com/?apikey=key&i=tt0133093",
                response=error_response,
            )
        )

        with self.assertLogs("app.providers.omdb", "WARNING") as logs:
            self.assertEqual(omdb.get_ratings(LINKS), [])
        self.assertEqual(omdb.get_ratings(LINKS), [])

        mock_request.assert_called_once()
        # The error's URL holds the key, which stays out of the logs
        self.assertNotIn("apikey", "".join(logs.output))
        self.assertIn("HTTPError 401", "".join(logs.output))

    def test_unknown_titles_have_no_ratings(self):
        """OMDb reports unknown titles with a failed response."""
        self.assertEqual(
            omdb.process_ratings({"Response": "False", "Error": "Not found!"}),
            [],
        )
