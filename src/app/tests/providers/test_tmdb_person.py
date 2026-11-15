from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from app.providers import tmdb


def _credit(media_id, media_type="movie", **extra):
    """Return a credit of a person's combined credits as TMDB gives it."""
    title_key, date_key = (
        ("title", "release_date")
        if media_type == "movie"
        else ("name", "first_air_date")
    )
    return {
        "id": media_id,
        "media_type": media_type,
        title_key: f"Title {media_id}",
        date_key: "2020-01-01",
        "poster_path": "/poster.jpg",
        "vote_count": 100,
        **extra,
    }


class PersonCreditsTests(TestCase):
    """Test the credits TMDB gives for a person."""

    def credits(self, cast=(), crew=()):
        """Return the credits by (department, media ID)."""
        found = tmdb.get_person_credits({"cast": list(cast), "crew": list(crew)})
        return {(credit["department"], credit["media_id"]): credit for credit in found}

    def test_a_title_is_listed_once_for_each_department(self):
        """Roles in a department are merged, and departments kept apart."""
        found = self.credits(
            cast=[_credit(1, character="Hero"), _credit(1, character="Twin")],
            crew=[
                _credit(1, department="Directing", job="Director"),
                _credit(1, department="Writing", job="Writer"),
                _credit(1, department="Writing", job="Story"),
            ],
        )

        self.assertEqual(
            {key: credit["role"] for key, credit in found.items()},
            {
                ("Acting", 1): "Hero, Twin",
                ("Directing", 1): "Director",
                ("Writing", 1): "Writer, Story",
            },
        )
        self.assertEqual(found["Acting", 1]["title"], "Title 1")
        self.assertEqual(found["Acting", 1]["release_year"], 2020)
        self.assertEqual(found["Acting", 1]["media_type"], "movie")

    def test_a_movie_and_a_show_with_the_same_id_stay_apart(self):
        """Movies and shows are numbered apart, and a repeated role is said once."""
        found = tmdb.get_person_credits(
            {
                "cast": [
                    _credit(1, character="Hero"),
                    _credit(1, character="Hero"),
                    _credit(1, "tv", character="Host"),
                ],
            },
        )

        self.assertEqual(
            sorted((credit["media_type"], credit["role"]) for credit in found),
            [("movie", "Hero"), ("tv", "Host")],
        )

    def test_a_show_role_says_how_many_episodes(self):
        """A show's role ends with its episode count, alone when it has no name."""
        found = self.credits(
            cast=[
                _credit(1, "tv", character="Lead", episode_count=62),
                _credit(2, "tv", character="", episode_count=1),
            ],
        )

        self.assertEqual(found["Acting", 1]["role"], "Lead · 62 episodes")
        self.assertEqual(found["Acting", 2]["role"], "1 episode")

    def test_newest_first_and_undated_last(self):
        """Credits go by release date, the ones without one at the end."""
        found = tmdb.get_person_credits(
            {
                "cast": [
                    _credit(1, release_date="2001-05-05"),
                    _credit(2, release_date=""),
                    _credit(3, "tv", first_air_date="2015-01-01"),
                ],
            },
        )

        self.assertEqual([credit["media_id"] for credit in found], [3, 1, 2])

    def test_only_real_roles_count_towards_known_for(self):
        """Cameos, guest spots and appearances as themself get no votes."""
        found = self.credits(
            cast=[
                _credit(1, character="Lead", order=0),
                _credit(2, character="Cameo", order=tmdb.KNOWN_FOR_BILLING),
                _credit(3, character="Self", order=0),
                _credit(
                    4, "tv", character="Lead", episode_count=tmdb.KNOWN_FOR_EPISODES
                ),
                _credit(
                    5,
                    "tv",
                    character="Guest",
                    episode_count=tmdb.KNOWN_FOR_EPISODES - 1,
                ),
                _credit(6, "tv", character="Self - Host", episode_count=50),
                _credit(7, character="Themselves", order=0),
                _credit(8, character="Harry Selfridge", order=0),
            ],
            crew=[
                _credit(2, department="Directing", job="Director"),
                _credit(
                    5, "tv", department="Directing", job="Director", episode_count=1
                ),
            ],
        )

        self.assertEqual(
            {key: credit["votes"] for key, credit in found.items()},
            {
                ("Acting", 1): 100,
                ("Acting", 2): 0,
                ("Acting", 3): 0,
                ("Acting", 4): 100,
                ("Acting", 5): 0,
                ("Acting", 6): 0,
                ("Acting", 7): 0,
                ("Acting", 8): 100,
                ("Directing", 2): 100,
                ("Directing", 5): 0,
            },
        )

    def test_adult_titles_need_the_setting(self):
        """Adult titles and other kinds of media are left out."""
        cast = [_credit(1, adult=True), _credit(2), _credit(3, "collection")]

        self.assertEqual(list(self.credits(cast=cast)), [("Acting", 2)])
        with override_settings(TMDB_NSFW=True):
            self.assertEqual(
                list(self.credits(cast=cast)), [("Acting", 1), ("Acting", 2)]
            )

    @patch("app.providers.tmdb.services.api_request")
    def test_person(self, mock_request):
        """A person's details are read once, then come from the cache."""
        self.addCleanup(cache.clear)
        mock_request.return_value = {
            "name": "Jane Doe",
            "profile_path": "/jane.jpg",
            "biography": "A life.",
            "known_for_department": "Acting",
            "birthday": "1970-02-03",
            "deathday": None,
            "place_of_birth": "Paris, France",
            "external_ids": {"imdb_id": "nm001", "wikidata_id": None},
            "combined_credits": {"cast": [_credit(1, character="Hero")], "crew": []},
        }

        person = tmdb.person(42)
        tmdb.person(42)

        mock_request.assert_called_once()
        self.assertEqual(person["name"], "Jane Doe")
        self.assertEqual(person["image"], tmdb.get_image_url("/jane.jpg"))
        self.assertEqual(person["source_url"], "https://www.themoviedb.org/person/42")
        self.assertEqual(
            person["details"],
            {
                "known_for": "Acting",
                "born": "1970-02-03",
                "died": None,
                "birthplace": "Paris, France",
            },
        )
        self.assertEqual(
            person["external_links"], {"IMDb": "https://www.imdb.com/name/nm001/"}
        )
        self.assertEqual([credit["role"] for credit in person["credits"]], ["Hero"])
