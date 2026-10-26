from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app import quick_actions
from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status
from events.models import Event


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
            MediaTypes.MANGA.value,
            SimpleNamespace(status=Status.IN_PROGRESS.value, progress=3),
        )
        self.assertEqual(action.key, quick_actions.NEXT)
        self.assertEqual(action.label, "+1 chapter")

    def test_in_progress_watched_media_names_the_next_episode(self):
        """Seasons and anime offer to mark their next episode watched."""
        anime = SimpleNamespace(status=Status.IN_PROGRESS.value, progress=3)
        season = SimpleNamespace(
            status=Status.IN_PROGRESS.value,
            progress=0,
            item=SimpleNamespace(season_number=2),
        )

        anime_action = quick_actions.get_quick_action(MediaTypes.ANIME.value, anime)
        season_action = quick_actions.get_quick_action(MediaTypes.SEASON.value, season)

        self.assertEqual(anime_action.label, "Mark E4 watched")
        self.assertEqual(season_action.label, "Mark S2E1 watched")
        self.assertEqual(season_action.key, quick_actions.NEXT)
        self.assertEqual(season_action.icon, "app/icons/states/in-progress.svg")

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

    def test_paused_media_is_resumed(self):
        """Paused media offers to resume it."""
        action = quick_actions.get_quick_action(
            MediaTypes.GAME.value,
            entry(Status.PAUSED.value),
        )
        self.assertEqual(action.key, quick_actions.RESUME)
        self.assertEqual(action.target_status, Status.IN_PROGRESS.value)

    def test_shows_and_seasons_can_be_finished_at_once(self):
        """Shows and seasons with episodes to watch can be marked watched whole."""
        cases = {
            (MediaTypes.TV.value, Status.IN_PROGRESS.value): (
                quick_actions.COMPLETE,
                "Mark show watched",
            ),
            (MediaTypes.SEASON.value, Status.PLANNING.value): (
                quick_actions.COMPLETE,
                "Mark season watched",
            ),
            (MediaTypes.SEASON.value, Status.REPEATING.value): (
                quick_actions.REPEAT,
                "Mark season rewatched",
            ),
        }
        for (media_type, status), expected in cases.items():
            action = quick_actions.get_finish_action(media_type, entry(status))
            self.assertEqual((action.key, action.label), expected)
            self.assertEqual(action.target_status, Status.COMPLETED.value)

        for media_type, status in (
            (MediaTypes.TV.value, Status.COMPLETED.value),
            (MediaTypes.SEASON.value, Status.DROPPED.value),
            (MediaTypes.MOVIE.value, Status.IN_PROGRESS.value),
        ):
            self.assertIsNone(
                quick_actions.get_finish_action(media_type, entry(status)),
            )
        self.assertIsNone(quick_actions.get_finish_action(MediaTypes.TV.value, None))

    def test_dropped_media_has_no_action(self):
        """Dropped media offers nothing."""
        self.assertIsNone(
            quick_actions.get_quick_action(
                MediaTypes.MOVIE.value,
                entry(Status.DROPPED.value),
            ),
        )

    def test_finished_media_is_planned_to_be_rewatched(self):
        """Finished media of any type offers a rewatch, in its own words."""
        labels = {
            MediaTypes.MOVIE.value: ("Plan to rewatch", "Mark rewatched"),
            MediaTypes.BOOK.value: ("Plan to reread", "Mark reread"),
            MediaTypes.GAME.value: ("Plan to replay", "Mark replayed"),
        }
        for media_type, (plan, mark) in labels.items():
            action = quick_actions.get_quick_action(
                media_type,
                entry(Status.COMPLETED.value),
            )
            self.assertEqual(
                (action.key, action.label, action.target_status),
                (quick_actions.PLAN_REPEAT, plan, Status.PLANNING_REPEAT.value),
            )
            action = quick_actions.get_quick_action(
                media_type,
                entry(Status.PLANNING_REPEAT.value),
            )
            self.assertEqual(
                (action.key, action.label, action.target_status),
                (quick_actions.REPEAT, mark, Status.COMPLETED.value),
            )

    def test_rewatch_in_progress_goes_on_as_one(self):
        """A rewatch in progress is finished, or counted on, without restarting."""
        movie = quick_actions.get_quick_action(
            MediaTypes.MOVIE.value,
            entry(Status.REPEATING.value),
        )
        self.assertEqual(movie.key, quick_actions.REPEAT)

        anime = SimpleNamespace(status=Status.REPEATING.value, progress=3)
        anime.max_progress = 12
        action = quick_actions.get_quick_action(MediaTypes.ANIME.value, anime)
        self.assertEqual(action.key, quick_actions.NEXT)
        self.assertEqual(action.target_status, Status.REPEATING.value)
        self.assertEqual(action.label, "Mark E4 watched")
        self.assertIn("repeating", action.icon)

    def test_episodes_have_no_action(self):
        """Episode cards keep their own tracking button."""
        self.assertIsNone(
            quick_actions.get_quick_action(MediaTypes.EPISODE.value, None),
        )


class ShowNextSeasonTests(TestCase):
    """Test which season a show in progress watches its next episode in."""

    def setUp(self):
        """Track a show in progress with specials and two seasons in progress.

        Each has two episodes out, and season 1 has both watched.
        """
        self.user = get_user_model().objects.create_user(username="test")
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
        )
        self.tv = TV(item=tv_item, user=self.user, status=Status.IN_PROGRESS.value)
        TV.save_base(self.tv)
        self.seasons = {}
        aired = timezone.now() - timedelta(days=30)
        for number in (0, 1, 2):
            item = Item.objects.create(
                media_id="1",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Show",
                season_number=number,
            )
            season = Season(
                item=item,
                user=self.user,
                related_tv=self.tv,
                status=Status.IN_PROGRESS.value,
            )
            Season.save_base(season)
            self.seasons[number] = season
            Event.objects.create(item=item, content_number=2, datetime=aired)
        self.watch(1, 1)
        self.watch(1, 2)

    def watch(self, season_number, episode_number):
        """Watch an episode, without the cascade of its season and show."""
        item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Show",
            season_number=season_number,
            episode_number=episode_number,
        )
        Episode.objects.bulk_create(
            [Episode(related_season=self.seasons[season_number], item=item)],
        )

    def next_season_number(self):
        """Return the number of the season the show watches next, or None."""
        season = quick_actions.get_show_next_season(self.tv)
        return season.item.season_number if season else None

    def test_caught_up_seasons_and_specials_come_last(self):
        """A caught up season is skipped, and specials wait for the rest."""
        self.assertEqual(self.next_season_number(), 2)

        self.watch(2, 1)
        self.watch(2, 2)
        self.assertEqual(self.next_season_number(), 0)

        self.watch(0, 1)
        self.watch(0, 2)
        self.assertIsNone(self.next_season_number())

    def test_show_card_watches_its_next_episode(self):
        """A show's own action watches the next episode of its season."""
        action = quick_actions.get_quick_action(MediaTypes.TV.value, self.tv)

        self.assertEqual(
            (action.key, action.label, action.target_status),
            (quick_actions.NEXT, "Mark S2E1 watched", Status.IN_PROGRESS.value),
        )

    def test_planned_seasons_follow_started_ones(self):
        """A planned show starts its first planned season with released episodes."""
        self.tv.status = Status.PLANNING.value
        TV.save_base(self.tv)
        for season in self.seasons.values():
            season.status = Status.PLANNING.value
            Season.save_base(season)

        action = quick_actions.get_quick_action(MediaTypes.TV.value, self.tv)

        self.assertEqual(action.label, "Mark S2E1 watched")

        # A started season comes before the planned ones
        self.seasons[0].status = Status.IN_PROGRESS.value
        Season.save_base(self.seasons[0])
        self.assertEqual(self.next_season_number(), 0)

    def test_unaired_planned_season_waits_for_its_release(self):
        """An unaired planned season, and a show waiting on it, offer nothing."""
        self.tv.status = Status.PLANNING.value
        TV.save_base(self.tv)
        for season in self.seasons.values():
            season.status = Status.COMPLETED.value
            Season.save_base(season)
        self.seasons[2].status = Status.PLANNING.value
        Season.save_base(self.seasons[2])
        Event.objects.filter(item=self.seasons[2].item).update(
            datetime=timezone.now() + timedelta(days=30),
        )

        self.assertIsNone(
            quick_actions.get_quick_action(MediaTypes.SEASON.value, self.seasons[2]),
        )
        self.assertIsNone(quick_actions.get_quick_action(MediaTypes.TV.value, self.tv))

    def test_unaired_uses_prefetched_events(self):
        """Cards with their releases loaded don't query them again."""
        item = self.seasons[2].item
        item.prefetched_events = list(Event.objects.filter(item=item))

        with self.assertNumQueries(0):
            self.assertFalse(quick_actions._unaired(item))

        item.prefetched_events[0].datetime = timezone.now() + timedelta(days=1)
        self.assertTrue(quick_actions._unaired(item))
