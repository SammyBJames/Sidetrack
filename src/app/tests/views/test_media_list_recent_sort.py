from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Anime, Item, MediaTypes, Sources, Status
from users.models import MediaSortChoices


class MediaListRecentSortTests(TestCase):
    """Test the media list's Recent sort, the order of the home page's rows."""

    def setUp(self):
        """Create a user, log in and add three anime in progress, oldest first."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.anime = {}
        for name in ("Oldest", "Middle", "Newest"):
            item = Item.objects.create(
                media_id=name,
                source=Sources.MANUAL.value,
                media_type=MediaTypes.ANIME.value,
                title=name,
                image="http://example.com/image.jpg",
            )
            self.anime[name] = Anime.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

    def titles(self, sort):
        """Return the anime list's titles in the order of a sort."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.ANIME.value]),
            {"sort": sort, "status": Status.IN_PROGRESS.value},
        )
        return [media.item.title for media in response.context["media_list"]]

    def test_recent_sorts_by_last_activity(self):
        """Recent puts the most recently updated entry first."""
        self.assertEqual(
            self.titles(MediaSortChoices.RECENT),
            ["Newest", "Middle", "Oldest"],
        )

        oldest = self.anime["Oldest"]
        oldest.progress = 1
        oldest.save()

        self.assertEqual(self.titles(MediaSortChoices.RECENT)[0], "Oldest")

    def test_recent_is_saved_as_the_list_sort(self):
        """Opening the list sorted by Recent saves it like any other list sort."""
        self.titles(MediaSortChoices.RECENT)

        self.user.refresh_from_db()
        self.assertEqual(self.user.anime_sort, MediaSortChoices.RECENT)
