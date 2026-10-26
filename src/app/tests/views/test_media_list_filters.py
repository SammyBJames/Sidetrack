from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Item, MediaTypes, Movie, Sources, Status
from app.templatetags import app_tags


class MediaListFilterViewTests(TestCase):
    """Test the sort direction and filters of a media type's list."""

    def setUp(self):
        """Create a user with a rated and an unrated movie, and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.url = reverse("medialist", args=["test", MediaTypes.MOVIE.value])
        for media_id, title, score in (("1", "Alpha", Decimal(8)), ("2", "Beta", None)):
            Movie.objects.create(
                item=Item.objects.create(
                    media_id=media_id,
                    source=Sources.MANUAL.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=title,
                    image="https://example.com/image.jpg",
                ),
                user=self.user,
                status=Status.COMPLETED.value,
                score=score,
            )

    def titles(self, response):
        """Return the titles a response lists, in its order."""
        return [media.item.title for media in response.context["media_list"]]

    def test_new_sort_starts_in_its_usual_direction(self):
        """A sort without a direction goes in its usual one, which is saved."""
        response = self.client.get(f"{self.url}?sort=title")

        self.user.refresh_from_db()
        self.assertEqual(self.titles(response), ["Alpha", "Beta"])
        self.assertEqual(self.user.movie_sort_order, "asc")

        self.client.get(f"{self.url}?sort=added")
        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_sort_order, "desc")

    def test_direction_is_saved_and_kept(self):
        """A chosen direction is saved, and the list keeps it on its next visit."""
        self.client.get(f"{self.url}?sort=title&order=desc")

        response = self.client.get(self.url)

        self.assertEqual(self.titles(response), ["Beta", "Alpha"])
        self.assertEqual(response.context["current_order"], "desc")

    def test_invalid_direction_keeps_the_saved_one(self):
        """An unknown direction leaves the saved one as it is."""
        self.client.get(f"{self.url}?sort=title&order=sideways")

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_sort_order, "desc")

    def test_score_filter_is_not_saved(self):
        """The unrated filter narrows the list for the request alone."""
        response = self.client.get(f"{self.url}?score=unrated")
        self.assertEqual(self.titles(response), ["Beta"])

        response = self.client.get(self.url)
        self.assertEqual(len(self.titles(response)), 2)

    def test_counts_alone_after_a_card_changes(self):
        """The status counts can be asked for alone, under the list's filters."""
        response = self.client.get(
            f"{self.url}?part=counts&score_min=0",
            headers={"hx-request": "true"},
        )

        self.assertTemplateUsed(response, "app/components/library_status_chips.html")
        self.assertTemplateNotUsed(response, "app/components/library_results.html")
        counts = {
            choice["value"]: choice["count"]
            for choice in response.context["status_choices"]
        }
        self.assertEqual(counts, {"All": 1, Status.COMPLETED.value: 1})

    def test_rows_show_the_sorted_date(self):
        """The rows show when media was added while the list sorts by it."""
        response = self.client.get(f"{self.url}?sort=added&layout=table")

        self.assertEqual(response.context["sorted_field"].label, "Date added")
        movie = response.context["media_list"][0]
        self.assertEqual(
            movie.sorted_value, app_tags.date_format(movie.added_at, self.user)
        )
        self.assertContains(response, 'class="media-row', count=2)

    def test_other_users_list_isnt_saved(self):
        """Sorting someone else's public list leaves their preferences alone."""
        other = get_user_model().objects.create_user(
            username="other",
            password="12345",  # noqa: S106
            profile_private=False,
        )

        self.client.get(
            reverse("medialist", args=["other", MediaTypes.MOVIE.value])
            + "?sort=title&order=asc",
        )

        other.refresh_from_db()
        self.assertEqual(other.movie_sort, "score")
        self.assertEqual(other.movie_sort_order, "desc")
