import datetime
import html
import re

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from app import config
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

    def test_history_modal_folds_rapid_edits(self):
        """Rapid rating edits show in the popup as one expandable row."""
        for score in (3, 4, 5):
            self.movie.score = score
            self.movie.save()
        self.movie.history.update(history_user=self.user)

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

        self.assertContains(response, "3 edits")
        self.assertContains(response, 'id="activity-member-movie-')

    def test_history_modal_fold_is_plain_text_and_members_are_chips(self):
        """A status fold's row reads as plain text, its members as coloured chips."""
        for status in (Status.PAUSED, Status.IN_PROGRESS, Status.DROPPED):
            self.movie.status = status.value
            self.movie.save()
        self.movie.history.update(history_user=self.user)

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

        self.assertContains(response, "3 edits")
        # The title row: the net change, with the new status uncoloured
        plain = re.findall(
            r'text-gray-200">\s*(?:<svg.*?</svg>\s*)?Dropped\s*</span>',
            response.content.decode(),
            flags=re.DOTALL,
        )
        self.assertEqual(len(plain), 1)
        # Each member: its new status as a chip in its colour
        for status in (Status.PAUSED, Status.IN_PROGRESS, Status.DROPPED):
            colour = f"font-medium {config.get_status_text_color(status.value)}"
            self.assertContains(response, colour, count=1)

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
        self.assertContains(response, "Finished 1 time")

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

    def test_popup_follows_the_users_date_and_time_formats(self):
        """Day headings, the summary and the time with its tooltip use the settings."""
        response = self.client.get(self._url(MediaTypes.SEASON.value, 2))

        self.assertContains(response, "October 2, 2026")
        self.assertContains(response, "Added October 1, 2026")
        self.assertContains(response, 'title="October 2, 2026 11:59 AM">11:59 AM<')

        self.user.date_format = "Y-m-d"
        self.user.time_format = "H:i"
        self.user.save()
        response = self.client.get(self._url(MediaTypes.SEASON.value, 2))

        self.assertContains(response, "2026-10-02")
        self.assertContains(response, "Added 2026-10-01")
        self.assertContains(response, 'title="2026-10-02 11:59">11:59<')

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

    def _render(self, change, media_type=MediaTypes.MOVIE.value, *, chips=True):
        """Return the activity_change template rendered for one change dict.

        ``chips`` is for a row with no badge (a fold's member).
        """
        return render_to_string(
            "app/components/activity_change.html",
            {"change": change, "media_type": media_type, "chips": chips},
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

    def test_badged_row_draws_its_status_as_plain_text(self):
        """A badged row shows old, an arrow and new, with no icon or colour."""
        html = self._render(
            {
                "field": "status",
                "old": Status.PLANNING.value,
                "new": Status.COMPLETED.value,
                "description": "x",
            },
            chips=False,
        )
        first = self._render(
            {
                "field": "status",
                "old": None,
                "new": Status.COMPLETED.value,
                "description": "x",
            },
            chips=False,
        )

        colour = config.get_status_text_color(Status.COMPLETED.value)
        self.assertIn("Planning", html)
        self.assertIn("Completed", html)
        self.assertIn("M5 12h14", html)
        self.assertEqual(html.count("<svg"), 1)
        self.assertNotIn(colour, html)
        self.assertIn("Completed", first)
        self.assertNotIn("<svg", first)
        self.assertNotIn(colour, first)

    def test_member_row_keeps_the_coloured_chip(self):
        """A fold's member, with no badge, draws the new status as an icon chip."""
        html = self._render(
            {
                "field": "status",
                "old": Status.PLANNING.value,
                "new": Status.COMPLETED.value,
                "description": "x",
            },
            chips=True,
        )

        self.assertEqual(html.count("<svg"), 2)
        self.assertIn(config.get_status_text_color(Status.COMPLETED.value), html)

    def test_first_status_is_its_chip_alone(self):
        """A first status shows as the status, with no arrow and no old status."""
        html = self._render(
            {
                "field": "status",
                "old": None,
                "new": Status.COMPLETED.value,
                "description": "Marked as finished watching",
            },
        )

        self.assertIn("Completed", html)
        self.assertIn("<svg", html)
        self.assertNotIn("M5 12h14", html)

    def test_first_planning_reads_as_its_description(self):
        """A first Planning is "Added to watchlist", not a chip."""
        html = self._render(
            {
                "field": "status",
                "old": None,
                "new": Status.PLANNING.value,
                "description": "Added to watchlist",
            },
        )

        self.assertIn("Added to watchlist", html)
        self.assertNotIn("<svg", html)

    def test_named_change_reads_as_its_description(self):
        """A named change (a repeat's sequence, an episode's) shows its words."""
        html = self._render(
            {
                "field": "status",
                "old": None,
                "new": Status.COMPLETED.value,
                "description": "Marked as rewatched",
                "named": True,
            },
        )

        self.assertIn("Marked as rewatched", html)
        self.assertNotIn("<svg", html)

        # On a row with a badge the words must show, not only a screen reader's
        plain = self._render(
            {
                "field": "status",
                "old": None,
                "new": Status.COMPLETED.value,
                "description": "Marked as rewatched",
                "named": True,
            },
            chips=False,
        )

        self.assertNotIn("sr-only", plain)
        self.assertIn(">Marked as rewatched<", plain)

    def test_cleared_rating_reads_removed(self):
        """A rating taken away reads "Removed rating", with no arrow."""
        html = self._render(
            {
                "field": "score",
                "old": 4.0,
                "new": None,
                "description": "Removed rating",
            },
        )

        self.assertIn("Removed rating", html)
        self.assertNotIn("M5 12h14", html)

    def test_rating_that_ends_where_it_began_has_no_arrow(self):
        """A fold's rating back to its start reads as text, not "Rating 4 → 4"."""
        html = self._render(
            {
                "field": "score",
                "old": 4.0,
                "new": 4.0,
                "description": "Changed rating 2 times, back to 4/10",
            },
        )

        self.assertIn("Changed rating 2 times, back to 4/10", html)
        self.assertNotIn("M5 12h14", html)
        self.assertNotIn("Rating 4", html)

    def test_date_change_has_its_date_as_a_tooltip(self):
        """A date change reads without the date, which is its tooltip."""
        html = self._render(
            {
                "field": "end_date",
                "old": None,
                "new": None,
                "description": "Changed end date",
                "title": "September 1, 2026 8:00 AM",
            },
        )

        self.assertIn("Changed end date", html)
        self.assertIn('title="September 1, 2026 8:00 AM"', html)

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
                "description": "x",
            },
        )

        self.assertIn("Rating 9", score)
        self.assertIn("M5 12h14", status)
        self.assertIn("M5 12h14", score)
