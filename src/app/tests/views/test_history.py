import datetime
import html
import re

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)


class HistoryModalViewTests(TestCase):
    """Test the history modal view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        self.movie = Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        self.movie.status = Status.COMPLETED.value
        self.movie.progress = 1
        self.movie.score = 8
        self.movie.save()
        self.movie.history.update(history_user=self.user)

    def test_history_modal_view(self):
        """Test the history modal view."""
        response = self.client.get(
            reverse(
                "history_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_history.html")

        self.assertTrue(response.context["journal_days"])
        self.assertContains(response, "Completed")
        self.assertContains(response, "Rated 8/10")

    def test_history_modal_shows_summary(self):
        """The popup opens with when it was added and its finishes."""
        response = self.client.get(
            reverse(
                "history_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            ),
        )

        self.assertContains(response, "Added ")
        self.assertContains(response, "Finished 1 time")
        self.assertContains(response, "Last activity just now")

    def test_history_modal_empty(self):
        """A media with no activity says so, with no summary."""
        response = self.client.get(
            reverse(
                "history_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "999",
                },
            ),
        )

        self.assertContains(response, "No activity yet.")
        self.assertNotContains(response, "Last activity")


class HistoryTimelineTests(TestCase):
    """Test the popup's scoped, paged timeline."""

    def setUp(self):
        """Create a show with a season of watched episodes, and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        tv_item = Item.objects.create(
            media_id="9",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
        )
        tv = TV(item=tv_item, user=self.user, status=Status.IN_PROGRESS.value)
        tv._history_user = self.user
        tv.save()
        season_item = Item.objects.create(
            media_id="9",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Show",
            season_number=2,
        )
        season = Season(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        season._history_user = self.user
        season.save()
        # Older than every episode watch, so it opens the last page.
        for obj in (tv, season):
            record = obj.history.first()
            type(record).objects.filter(history_id=record.history_id).update(
                history_date=datetime.datetime(2026, 10, 1, 10, tzinfo=datetime.UTC),
            )
        self.episode_records = []
        for number in range(1, 41):
            item = Item.objects.create(
                media_id="9",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Show",
                season_number=2,
                episode_number=number,
            )
            episode = Episode(
                item=item,
                related_season=season,
                status=Status.COMPLETED.value,
                end_date=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
            )
            episode._history_user = self.user
            Episode.save_base(episode)
            record = episode.history.first()
            # One per minute, the last 5 on the day before.
            when = datetime.datetime(2026, 10, 2, 12, tzinfo=datetime.UTC)
            when -= datetime.timedelta(minutes=number, days=number // 36)
            type(record).objects.filter(history_id=record.history_id).update(
                history_date=when,
            )
            self.episode_records.append(record)

    def _url(self, media_type, *args):
        """Return the popup's url for the show (or its season 2)."""
        return reverse(
            "history_modal",
            args=[Sources.TMDB.value, media_type, "9", *args],
        )

    def test_season_popup_lists_its_episode_watches(self):
        """A season's popup holds its episodes' watches, tagged by episode."""
        response = self.client.get(self._url(MediaTypes.SEASON.value, 2))

        self.assertContains(response, "E1")
        self.assertContains(response, "E30")
        self.assertNotContains(response, "E31<")
        self.assertContains(response, "Watched")

    def test_show_popup_lists_seasons_and_episodes(self):
        """A show's popup holds its season's and episodes' activity."""
        response = self.client.get(self._url(MediaTypes.TV.value))

        self.assertContains(response, "S2E1")
        later = self.client.get(
            self._url(MediaTypes.TV.value) + f"?{response.context['next_query']}",
            headers={"HX-Request": "true"},
        )
        self.assertContains(later, "Marked as currently watching")
        self.assertContains(later, "S2<")

    def test_cursor_request_returns_next_page_only(self):
        """A cursor request returns the items after it, without the sheet."""
        first = self.client.get(self._url(MediaTypes.SEASON.value, 2))
        next_url = first.context["next_query"]
        self.assertTrue(first.context["has_next"])

        response = self.client.get(
            self._url(MediaTypes.SEASON.value, 2)
            + f"?{next_url}&last_day={first.context['last_day']}",
            headers={"HX-Request": "true"},
        )

        self.assertTemplateNotUsed(response, "app/components/fill_history.html")
        self.assertNotContains(response, "Activity History")
        self.assertNotContains(response, "Last activity")
        self.assertContains(response, "E31")
        self.assertNotContains(response, "E30<")
        self.assertFalse(response.context["has_next"])

    def test_cursor_request_does_not_repeat_the_day_heading(self):
        """The rendered sentinel loads a page that doesn't repeat the day heading."""
        first = self.client.get(
            self._url(MediaTypes.SEASON.value, 2),
            headers={"HX-Request": "true"},
        )
        self.assertContains(first, "Activity History")
        self.assertEqual(len(first.context["journal_days"]), 1)
        sentinel = re.search(r'hx-get="([^"]+)"', first.content.decode())

        response = self.client.get(
            html.unescape(sentinel.group(1)),
            headers={"HX-Request": "true"},
        )

        self.assertContains(response, "E31")
        self.assertEqual(len(response.context["journal_days"]), 2)
        self.assertContains(response, "<h3", count=1)

    def test_other_users_activity_is_not_shown(self):
        """Another user's popup for the same show is empty."""
        other = get_user_model().objects.create_user(username="other")
        self.client.force_login(other)

        response = self.client.get(self._url(MediaTypes.SEASON.value, 2))

        self.assertContains(response, "No activity yet.")


class DeleteHistoryRecordViewTests(TestCase):
    """Test the delete history record view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        self.movie = Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        self.movie.status = Status.COMPLETED.value
        self.movie.progress = 1
        self.movie.score = 8
        self.movie.save()

        self.history = self.movie.history.first()
        self.history_id = self.history.history_id

        # Manually update the history_user field
        self.history.history_user = self.user
        self.history.save()

    def test_delete_history_record(self):
        """Test deleting a history record."""
        response = self.client.delete(
            reverse(
                "delete_history_record",
                kwargs={
                    "media_type": MediaTypes.MOVIE.value,
                    "history_id": self.history_id,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            self.movie.history.filter(history_id=self.history_id).count(),
            0,
        )

    def test_delete_nonexistent_history_record(self):
        """Test deleting a nonexistent history record."""
        response = self.client.delete(
            reverse(
                "delete_history_record",
                kwargs={
                    "media_type": MediaTypes.MOVIE.value,
                    "history_id": 999999,
                },
            ),
        )

        self.assertEqual(response.status_code, 404)


class ActivityChangeTemplateTests(TestCase):
    """Test how one change is drawn."""

    def _render(self, change, media_type=MediaTypes.MOVIE.value):
        """Return the activity_change template rendered for one change dict."""
        return render_to_string(
            "app/components/activity_change.html",
            {"change": change, "media_type": media_type},
        )

    def test_status_change_with_no_net_change_reads_as_text(self):
        """A status that ends where it began shows its description, not chips."""
        html = self._render(
            {
                "field": "status",
                "old": Status.PLANNING_REPEAT.value,
                "new": Status.PLANNING_REPEAT.value,
                "description": "Changed status 4 times, back to Planning Rewatch",
            },
        )

        self.assertIn("Changed status 4 times, back to Planning Rewatch", html)
        self.assertNotIn("<svg", html)

    def test_status_and_score_changes_share_the_arrow(self):
        """Both draw the same arrow icon between old and new."""
        status = self._render(
            {
                "field": "status",
                "old": Status.PLANNING.value,
                "new": Status.COMPLETED.value,
                "description": "x",
            },
        )
        score = self._render(
            {
                "field": "score",
                "old": 9.0,
                "new": 4.0,
                "label": "Rating 9 → 4",
                "description": "x",
            },
        )

        self.assertIn("Rating 9", score)
        self.assertIn("M5 12h14", status)
        self.assertIn("M5 12h14", score)
