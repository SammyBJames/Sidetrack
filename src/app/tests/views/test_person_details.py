import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app import views
from app.models import Item, MediaTypes, Movie, Sources, Status


def _credit(media_id, department, media_type="movie", **extra):
    """Return a credit as `tmdb.person` gives it."""
    return {
        "media_id": media_id,
        "source": Sources.TMDB.value,
        "media_type": media_type,
        "title": f"Title {media_id}",
        "image": "http://example.com/poster.jpg",
        "release_year": 2000 + media_id,
        "date": f"{2000 + media_id}-01-01",
        "department": department,
        "votes": media_id,
        "role": f"Role {media_id}",
        **extra,
    }


def _person(person_credits, known_for="Acting"):
    """Return a person as `tmdb.person` gives them."""
    return {
        "person_id": 42,
        "source": Sources.TMDB.value,
        "source_url": "https://www.themoviedb.org/person/42",
        "name": "Jane Doe",
        "image": "http://example.com/jane.jpg",
        "biography": "A life.",
        "details": {
            "known_for": known_for,
            "born": "1970-02-03",
            "died": None,
            "birthplace": "Paris, France",
        },
        "external_links": {"IMDb": "https://www.imdb.com/name/nm001/"},
        "credits": person_credits,
    }


@patch("app.providers.tmdb.person")
class PersonDetailsTests(TestCase):
    """Test the page of a person."""

    def setUp(self):
        """Create a user and log in."""
        self.user = get_user_model().objects.create_user(username="test")
        self.client.force_login(self.user)
        self.url = reverse(
            "person_details", kwargs={"person_id": 42, "name": "jane-doe"}
        )

    def test_shows_the_person_and_their_credits(self, mock_person):
        """The page has their details and each credit with its role."""
        mock_person.return_value = _person(
            [_credit(2, "Acting", "tv"), _credit(1, "Acting")]
        )

        response = self.client.get(self.url)
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        mock_person.assert_called_once_with(42)
        self.assertContains(response, "Jane Doe")
        self.assertContains(response, "A life.")
        self.assertContains(response, "Paris, France")
        self.assertContains(response, "https://www.imdb.com/name/nm001/")
        self.assertEqual(
            re.findall(r"Role \d", content.split("Known For")[1].split("<section")[1]),
            ["Role 2", "Role 1"],
        )
        self.assertContains(response, "/details/tmdb/movie/1/title-1")
        self.assertContains(response, "/details/tmdb/tv/2/title-2")

    def test_departments_start_with_the_known_one_then_the_largest(self, mock_person):
        """Sections go by the department the person is known for, then by size."""
        mock_person.return_value = _person(
            [
                _credit(9, "Writing"),
                _credit(2, "Directing"),
                _credit(3, "Directing"),
                _credit(4, "Acting"),
            ],
        )

        response = self.client.get(self.url)

        self.assertEqual(
            [name for name, _ in response.context["departments"]],
            ["Acting", "Directing", "Writing"],
        )

    def test_known_for_is_the_most_voted_of_the_known_department(self, mock_person):
        """Credits without votes and those of other departments are left out."""
        mock_person.return_value = _person(
            [
                _credit(1, "Acting"),
                _credit(3, "Acting"),
                _credit(2, "Acting", "tv"),
                _credit(4, "Acting", votes=0),
                _credit(9, "Directing"),
            ],
        )

        response = self.client.get(self.url)

        self.assertEqual(
            [credit["item"]["media_id"] for credit in response.context["known_for"]],
            [3, 2, 1],
        )

    def test_known_for_has_a_limit(self, mock_person):
        """Only the most voted few are known for."""
        mock_person.return_value = _person(
            [_credit(media_id, "Acting") for media_id in range(1, 20)]
        )

        response = self.client.get(self.url)

        self.assertEqual(len(response.context["known_for"]), views.PERSON_KNOWN_FOR)

    def test_no_chips_for_a_person_with_one_type(self, mock_person):
        """A type the person has no credits of isn't offered."""
        mock_person.return_value = _person([_credit(1, "Acting")])

        response = self.client.get(self.url)

        self.assertEqual(response.context["credit_types"], ["movie"])
        self.assertNotContains(response, 'aria-label="Media type"')

    def test_more_credits_than_sqlite_nests_conditions(self, mock_person):
        """A person with over a thousand credits still gets their page."""
        mock_person.return_value = _person(
            [_credit(media_id, "Acting") for media_id in range(1, 1102)]
        )

        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_media_type_narrows_the_credits(self, mock_person):
        """A type's chip leaves only its credits, and an unknown one is ignored."""
        mock_person.return_value = _person(
            [_credit(1, "Acting"), _credit(2, "Acting", "tv")]
        )

        def shown(query):
            response = self.client.get(self.url + query)
            return [
                credit["item"]["media_id"]
                for _, section in response.context["departments"]
                for credit in section
            ]

        self.assertEqual(shown(""), [2, 1])
        self.assertEqual(shown("?media_type=tv"), [2])
        self.assertEqual(shown("?media_type=movie"), [1])
        self.assertEqual(shown("?media_type=book"), [2, 1])
        self.assertContains(self.client.get(self.url), "?media_type=tv")

    def test_only_enabled_types_are_shown(self, mock_person):
        """A disabled type's credits are left out, and so are the chips."""
        mock_person.return_value = _person(
            [_credit(1, "Acting"), _credit(2, "Acting", "tv")]
        )
        self.user.tv_enabled = False
        self.user.save()

        response = self.client.get(self.url + "?media_type=tv")

        self.assertEqual(response.context["credit_types"], ["movie"])
        self.assertNotContains(response, "Title 2")
        self.assertContains(response, "Title 1")
        self.assertNotContains(response, 'aria-label="Media type"')

    def test_no_credits(self, mock_person):
        """A person with nothing to show gets a note."""
        mock_person.return_value = _person([])

        response = self.client.get(self.url)

        self.assertContains(response, "No movies or TV shows to show for Jane Doe.")
        self.assertNotContains(response, "Known For")

    def test_a_tracked_credit_shows_its_status_and_reloads_the_page(self, mock_person):
        """A credit is matched to the user's entry, and its action reloads the page."""
        mock_person.return_value = _person([_credit(1, "Acting")])
        item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Title 1",
            image="http://example.com/poster.jpg",
        )
        movie = Movie.objects.create(
            item=item, user=self.user, status=Status.PLANNING.value
        )

        response = self.client.get(self.url)

        credit = response.context["departments"][0][1][0]
        self.assertEqual(credit["media"], movie)
        self.assertContains(response, '"card": "details"')
        self.assertNotContains(response, '"card": "row"')


class PersonLinkTests(TestCase):
    """Test the links to a person's page."""

    def setUp(self):
        """Create a user and log in."""
        self.client.force_login(get_user_model().objects.create_user(username="test"))

    @patch("app.providers.services.get_media_metadata")
    def test_cast_and_crew_link_to_their_pages(self, mock_metadata):
        """A details page's people link to their pages here, not to TMDB."""
        mock_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/movie/238",
            "image": "http://example.com/image.jpg",
            "cast": [
                {"id": 7, "name": "Jane Doe", "role": "Hero", "image": "http://x/j.jpg"}
            ],
            "total_cast_count": 1,
            "crew": [
                {
                    "id": 8,
                    "name": "Åsa Öst",
                    "role": "Director",
                    "image": "http://x/a.jpg",
                }
            ],
            "total_crew_count": 1,
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

        self.assertContains(response, 'href="/person/tmdb/7/jane-doe"')
        self.assertContains(response, 'href="/person/tmdb/8/asa-ost"')
        self.assertNotContains(response, "themoviedb.org/person")
