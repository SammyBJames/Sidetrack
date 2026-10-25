from types import SimpleNamespace

from django.test import TestCase

from app import quick_actions
from app.models import MediaTypes, Status


def entry(status):
    """Return a stand-in for a tracked entry with the given status."""
    return SimpleNamespace(status=status)


class GetQuickActionTests(TestCase):
    """Test which one-click action a media card offers."""

    def test_untracked_media_is_planned(self):
        """Untracked media of any type offers to plan it."""
        media_types = (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.BOOK.value,
        )
        for media_type in media_types:
            action = quick_actions.get_quick_action(media_type, None)
            self.assertEqual(action.key, quick_actions.PLAN)
            self.assertEqual(action.target_status, Status.PLANNING.value)

        self.assertEqual(
            quick_actions.get_quick_action(MediaTypes.GAME.value, None).label,
            "Plan to play",
        )
        for media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
            self.assertEqual(
                quick_actions.get_quick_action(media_type, None).label,
                "Watchlist",
            )

    def test_planned_movie_is_completed(self):
        """A planned movie offers to mark it watched."""
        action = quick_actions.get_quick_action(
            MediaTypes.MOVIE.value,
            entry(Status.PLANNING.value),
        )
        self.assertEqual(action.key, quick_actions.COMPLETE)
        self.assertEqual(action.label, "Mark watched")

    def test_planned_series_is_started(self):
        """Planned media with several parts offers to start it."""
        action = quick_actions.get_quick_action(
            MediaTypes.MANGA.value,
            entry(Status.PLANNING.value),
        )
        self.assertEqual(action.key, quick_actions.START)
        self.assertEqual(action.label, "Start reading")

    def test_in_progress_counted_media_adds_one(self):
        """In progress media counted in parts offers the next part."""
        action = quick_actions.get_quick_action(
            MediaTypes.ANIME.value,
            entry(Status.IN_PROGRESS.value),
        )
        self.assertEqual(action.key, quick_actions.NEXT)
        self.assertEqual(action.label, "+1 episode")

    def test_caught_up_media_has_no_next(self):
        """Media with everything released so far done offers no +1."""
        caught_up = SimpleNamespace(
            status=Status.IN_PROGRESS.value,
            progress=3,
            max_progress=3,
        )
        self.assertIsNone(
            quick_actions.get_quick_action(MediaTypes.ANIME.value, caught_up),
        )

        behind = SimpleNamespace(
            status=Status.IN_PROGRESS.value,
            progress=2,
            max_progress=3,
        )
        self.assertEqual(
            quick_actions.get_quick_action(MediaTypes.ANIME.value, behind).key,
            quick_actions.NEXT,
        )

    def test_in_progress_book_is_completed(self):
        """A book in progress offers to mark it read rather than add a page."""
        action = quick_actions.get_quick_action(
            MediaTypes.BOOK.value,
            entry(Status.IN_PROGRESS.value),
        )
        self.assertEqual(action.key, quick_actions.COMPLETE)
        self.assertEqual(action.label, "Mark read")

    def test_in_progress_tv_has_no_action(self):
        """A show in progress is tracked through its seasons."""
        self.assertIsNone(
            quick_actions.get_quick_action(
                MediaTypes.TV.value,
                entry(Status.IN_PROGRESS.value),
            ),
        )

    def test_paused_media_is_resumed(self):
        """Paused media offers to resume it."""
        action = quick_actions.get_quick_action(
            MediaTypes.GAME.value,
            entry(Status.PAUSED.value),
        )
        self.assertEqual(action.key, quick_actions.RESUME)
        self.assertEqual(action.target_status, Status.IN_PROGRESS.value)

    def test_finished_media_has_no_action(self):
        """Completed and dropped media offer nothing."""
        for status in (Status.COMPLETED.value, Status.DROPPED.value):
            self.assertIsNone(
                quick_actions.get_quick_action(MediaTypes.MOVIE.value, entry(status)),
            )

    def test_episodes_have_no_action(self):
        """Episode cards keep their own tracking button."""
        self.assertIsNone(
            quick_actions.get_quick_action(MediaTypes.EPISODE.value, None),
        )
