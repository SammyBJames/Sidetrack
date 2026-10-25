from datetime import UTC, datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Item, MediaTypes, Movie, Sources, Status
from events.models import Event, SentinelDatetime


class DownloadEventTests(TestCase):
    """Test downloading one release as an iCalendar file."""

    def setUp(self):
        """Create a user, log in and add a movie item."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.PLANNING.value,
        )

    def test_release_without_time_is_all_day(self):
        """A release with only a date becomes an all day event on that date."""
        release = Event.objects.create(
            item=self.item,
            datetime=datetime(
                2026,
                10,
                6,
                SentinelDatetime.HOUR,
                SentinelDatetime.MINUTE,
                SentinelDatetime.SECOND,
                SentinelDatetime.MICROSECOND,
                tzinfo=UTC,
            ),
        )

        response = self.client.get(reverse("download_event", args=[release.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/calendar")
        self.assertIn('filename="test-movie.ics"', response["Content-Disposition"])
        content = response.content.decode()
        self.assertIn("DTSTART;VALUE=DATE:20261006", content)
        self.assertIn("DTEND;VALUE=DATE:20261007", content)
        self.assertIn("SUMMARY:Test Movie", content)
        self.assertIn(f"UID:{release.id}", content)

    def test_release_with_time_keeps_it(self):
        """A release with a known time keeps that time."""
        release = Event.objects.create(
            item=self.item,
            datetime=datetime(2026, 10, 6, 21, 0, tzinfo=UTC),
        )

        response = self.client.get(reverse("download_event", args=[release.id]))

        self.assertIn("DTSTART:20261006T210000Z", response.content.decode())

    def test_missing_release_is_not_found(self):
        """An unknown release returns 404."""
        response = self.client.get(reverse("download_event", args=[999]))

        self.assertEqual(response.status_code, 404)

    def test_release_not_tracked_by_the_user_is_not_found(self):
        """Releases of media only other users track can't be downloaded."""
        other_item = Item.objects.create(
            media_id="2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Someone Else's Movie",
            image="http://example.com/image.jpg",
        )
        other = get_user_model().objects.create_user(
            username="other",
            password=self.credentials["password"],
        )
        Movie.objects.create(
            item=other_item,
            user=other,
            status=Status.PLANNING.value,
        )
        release = Event.objects.create(
            item=other_item,
            datetime=datetime(2026, 10, 6, 21, 0, tzinfo=UTC),
        )

        response = self.client.get(reverse("download_event", args=[release.id]))

        self.assertEqual(response.status_code, 404)
