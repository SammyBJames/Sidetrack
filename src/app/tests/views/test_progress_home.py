from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Anime, Item, MediaTypes, Sources, Status


@patch("app.models.Item.fetch_releases")
@patch(
    "app.providers.services.get_media_metadata",
    return_value={"title": "Test Anime", "image": "", "max_progress": 12},
)
class ProgressEditHomeCardTests(TestCase):
    """Test the home card returned after +1 on the home page."""

    def setUp(self):
        """Create a user, log in and add an anime in progress."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/anime.jpg",
        )
        self.anime = Anime.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=3,
        )

    def increase(self):
        """Post +1 from the In Progress section of the home page."""
        return self.client.post(
            reverse("progress_edit", args=[MediaTypes.ANIME.value, self.anime.id]),
            {"operation": "increase", "home_status": Status.IN_PROGRESS.value},
        )

    def test_card_is_updated_in_place(self, *_):
        """A card that stays in its section is re-rendered without a full refresh."""
        response = self.increase()

        self.anime.refresh_from_db()
        self.assertEqual(self.anime.progress, 4)
        self.assertContains(response, f'id="home-media-anime-{self.anime.id}"')
        self.assertNotIn("HX-Reswap", response)
        self.assertNotIn("HX-Trigger", response)

    def test_card_leaves_section_when_completed(self, *_):
        """Reaching the last episode completes the entry and moves its card."""
        self.anime.progress = 11
        self.anime.save()

        response = self.increase()

        self.anime.refresh_from_db()
        self.assertEqual(self.anime.status, Status.COMPLETED.value)
        self.assertEqual(response["HX-Reswap"], "delete")
        self.assertEqual(response["HX-Retarget"], f"#home-media-anime-{self.anime.id}")
        self.assertEqual(response["HX-Trigger"], "mediaChanged")
