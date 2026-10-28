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

    def test_in_progress_book_and_game_log_their_progress(self):
        """Books and games in progress or repeating log pages or time instead."""
        for media_type, label in (
            (MediaTypes.BOOK.value, "Log pages"),
            (MediaTypes.GAME.value, "Log time"),
        ):
            for status in (Status.IN_PROGRESS.value, Status.REPEATING.value):
                action = quick_actions.get_quick_action(media_type, entry(status))
                self.assertEqual(
                    (action.key, action.label, action.target_status),
                    (quick_actions.LOG, label, status),
                )

    def test_books_and_games_can_be_finished_in_progress(self):
        """A book or game in progress or repeating can be marked done at once."""
        cases = {
            (MediaTypes.BOOK.value, Status.IN_PROGRESS.value): (
                quick_actions.COMPLETE,
                "Mark read",
            ),
            (MediaTypes.BOOK.value, Status.REPEATING.value): (
                quick_actions.REPEAT,
                "Mark reread",
            ),
            (MediaTypes.GAME.value, Status.IN_PROGRESS.value): (
                quick_actions.COMPLETE,
                "Mark played",
            ),
            (MediaTypes.GAME.value, Status.REPEATING.value): (
                quick_actions.REPEAT,
                "Mark replayed",
            ),
        }
        for (media_type, status), expected in cases.items():
            action = quick_actions.get_finish_action(media_type, entry(status))
            self.assertEqual((action.key, action.label), expected)
            self.assertEqual(action.target_status, Status.COMPLETED.value)

        for status in (Status.PLANNING.value, Status.COMPLETED.value):
            self.assertIsNone(
                quick_actions.get_finish_action(MediaTypes.BOOK.value, entry(status)),
            )

    def test_planned_and_finished_books_keep_their_actions(self):
        """Only books and games in progress log; the other statuses are as before."""
        for status, key in (
            (Status.PLANNING.value, quick_actions.START),
            (Status.COMPLETED.value, quick_actions.PLAN_REPEAT),
            (Status.PAUSED.value, quick_actions.RESUME),
        ):
            action = quick_actions.get_quick_action(
                MediaTypes.BOOK.value, entry(status)
            )
            self.assertEqual(action.key, key)

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
            (MediaTypes.ANIME.value, Status.IN_PROGRESS.value),
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
            MediaTypes.BOOK.value: ("Plan to reread", "Start rereading"),
            MediaTypes.GAME.value: ("Plan to replay", "Start replaying"),
        }
        # A book or game is repeated in sittings, so its plan starts the repeat
        started = {MediaTypes.BOOK.value, MediaTypes.GAME.value}
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
            if media_type in started:
                expected = (quick_actions.START_REPEAT, Status.REPEATING.value)
            else:
                expected = (quick_actions.REPEAT, Status.COMPLETED.value)
            self.assertEqual(
                (action.key, action.label, action.target_status),
                (expected[0], mark, expected[1]),
            )

    def test_a_started_reread_logs_and_finishes_as_a_repeat(self):
        """A book or game being repeated logs progress, and is finished as a repeat."""
        for media_type, logged, finish in (
            (MediaTypes.BOOK.value, "Log pages", "Mark reread"),
            (MediaTypes.GAME.value, "Log time", "Mark replayed"),
        ):
            media = entry(Status.REPEATING.value)
            action = quick_actions.get_quick_action(media_type, media)
            self.assertEqual((action.key, action.label), (quick_actions.LOG, logged))
            self.assertEqual(action.target_status, Status.REPEATING.value)
            finish_action = quick_actions.get_finish_action(media_type, media)
            self.assertEqual(
                (finish_action.key, finish_action.label),
                (quick_actions.REPEAT, finish),
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

    def test_episode_without_state_has_no_action(self):
        """An episode entry of a list, which has no state yet, offers nothing."""
        self.assertIsNone(
            quick_actions.get_quick_action(MediaTypes.EPISODE.value, None),
        )


def episode_state(**fields):
    """Return the state of an episode not watched, with the fields given."""
    return {
        "watched": False,
        "repeat_planned": False,
        "history": [],
        "air_date": "2008-01-20",
        **fields,
    }


class EpisodeActionTests(TestCase):
    """Test the one-click action of an episode by its season entry and state."""

    def action(self, season, **fields):
        """Return the action of an episode with the state fields, or None."""
        return quick_actions.get_quick_action(
            MediaTypes.EPISODE.value,
            season,
            episode_state(**fields),
        )

    def test_unwatched_episode_is_watched(self):
        """An episode not watched yet offers to mark it watched, tracked or not."""
        for season in (None, entry(Status.IN_PROGRESS.value)):
            action = self.action(season)
            self.assertEqual(action.key, quick_actions.WATCH)
            self.assertEqual(action.label, "Mark watched")
            self.assertEqual(action.target_status, Status.COMPLETED.value)

    def test_watched_episode_is_planned_to_be_rewatched(self):
        """A watched episode of a season not rewatched offers to plan a rewatch."""
        for status in (Status.COMPLETED.value, Status.IN_PROGRESS.value):
            action = self.action(
                entry(status),
                watched=True,
                history=[object()],
            )
            self.assertEqual(action.key, quick_actions.PLAN_REPEAT)
            self.assertEqual(action.label, "Plan to rewatch")
            self.assertEqual(action.target_status, Status.PLANNING_REPEAT.value)

    def test_episode_planned_to_be_rewatched_is_rewatched(self):
        """One planned again is rewatched, on its own or in its season's rewatch."""
        for status in (Status.COMPLETED.value, Status.PLANNING_REPEAT.value):
            action = self.action(
                entry(status),
                watched=True,
                repeat_planned=True,
                history=[object()],
            )
            self.assertEqual(action.key, quick_actions.WATCH)
            self.assertEqual(action.label, "Mark rewatched")

    def test_episode_done_in_a_rewatch_has_no_action(self):
        """Once watched again in its season's rewatch, there is nothing to do."""
        for status in (Status.PLANNING_REPEAT.value, Status.REPEATING.value):
            self.assertIsNone(
                self.action(entry(status), watched=True, history=[object()]),
            )

    def test_unaired_episode_has_no_action(self):
        """An episode still to air waits, but a date in the past or none doesn't."""
        season = entry(Status.IN_PROGRESS.value)
        self.assertIsNone(self.action(season, air_date="2099-01-01"))
        self.assertEqual(self.action(season, air_date=None).key, quick_actions.WATCH)


class WaitingTests(TestCase):
    """Test the planned media that has nothing to act on until it comes out."""

    def planned(self, media_type, *offsets):
        """Return a planned entry with releases that many days from now."""
        events = [
            SimpleNamespace(datetime=timezone.now() + timedelta(days=days))
            for days in offsets
        ]
        item = SimpleNamespace(media_type=media_type, prefetched_events=events)
        return SimpleNamespace(status=Status.PLANNING.value, item=item)

    def test_planned_media_with_every_release_to_come_has_no_action(self):
        """A movie or season not out yet waits, whatever its type."""
        for media_type in (MediaTypes.MOVIE.value, MediaTypes.SEASON.value):
            self.assertIsNone(
                quick_actions.get_quick_action(
                    media_type, self.planned(media_type, 30)
                ),
            )

    def test_planned_media_airing_has_its_action(self):
        """With a release out already, planned media is acted on as usual."""
        movie = self.planned(MediaTypes.MOVIE.value, -1, 30)
        action = quick_actions.get_quick_action(MediaTypes.MOVIE.value, movie)
        self.assertEqual(action.key, quick_actions.COMPLETE)
        season = self.planned(MediaTypes.SEASON.value, -1, 30)
        season.progress = 0
        season.item.season_number = 1
        action = quick_actions.get_quick_action(MediaTypes.SEASON.value, season)
        self.assertEqual(action.key, quick_actions.NEXT)

    def test_caught_up_media_has_no_action(self):
        """Media in progress with all that is out done waits, even a game."""
        game = SimpleNamespace(
            status=Status.IN_PROGRESS.value,
            next_event=SimpleNamespace(is_max_datetime=False),
            progress=10,
            max_progress=None,
            item=SimpleNamespace(media_type=MediaTypes.GAME.value),
        )
        self.assertIsNone(quick_actions.get_quick_action(MediaTypes.GAME.value, game))
        game.next_event = None
        self.assertEqual(
            quick_actions.get_quick_action(MediaTypes.GAME.value, game).key,
            quick_actions.LOG,
        )

    def test_planned_media_without_known_releases_has_its_action(self):
        """No release known means nothing is to come, and no query to find out."""
        movie = self.planned(MediaTypes.MOVIE.value)
        self.assertEqual(
            quick_actions.get_quick_action(MediaTypes.MOVIE.value, movie).key,
            quick_actions.COMPLETE,
        )


class ButtonStateTests(TestCase):
    """Test what a button shows when it has no action."""

    def media(self, status, *, released=False, caught_up=False):
        """Return an entry with a next release, or none when `released`."""
        event = SimpleNamespace(is_max_datetime=False)
        return SimpleNamespace(
            status=status,
            next_event=None if released else event,
            progress=5 if caught_up else 1,
            max_progress=5,
        )

    def test_next_release_comes_with_the_clock_of_its_status(self):
        """A known release shows in the colour of the status it waits in."""
        colors = {
            Status.PLANNING.value: "text-sky-400",
            Status.IN_PROGRESS.value: "text-indigo-300",
            Status.REPEATING.value: "text-teal-300",
            Status.PLANNING_REPEAT.value: "text-teal-300",
        }
        for status, color in colors.items():
            with self.subTest(status=status):
                state = quick_actions.get_button_state(self.media(status))
                self.assertTrue(state.release)
                self.assertEqual(state.clock_color, color)

    def test_caught_up_media_waits_like_planned_media(self):
        """Media with all that is out done is in the colours of planning."""
        for status in (Status.IN_PROGRESS.value, Status.REPEATING.value):
            state = quick_actions.get_button_state(
                self.media(status, caught_up=True),
            )
            self.assertTrue(state.release)
            self.assertEqual(state.status, Status.PLANNING.value)
            self.assertEqual(state.clock_color, "text-sky-400")

    def test_without_a_release_it_is_the_status(self):
        """Without a known release it shows the status, and nothing without one."""
        state = quick_actions.get_button_state(
            self.media(Status.IN_PROGRESS.value, released=True),
        )
        self.assertFalse(state.release)
        self.assertEqual(state.status, Status.IN_PROGRESS.value)
        self.assertIsNone(quick_actions.get_button_state(None))

    def test_unknown_release_date_is_no_release(self):
        """A release without a date, and one of finished media, isn't shown."""
        media = self.media(Status.PLANNING.value)
        media.next_event.is_max_datetime = True
        self.assertFalse(quick_actions.get_button_state(media).release)
        completed = self.media(Status.COMPLETED.value)
        state = quick_actions.get_button_state(completed)
        self.assertFalse(state.release)
        self.assertEqual(state.status, Status.COMPLETED.value)

    def test_unaired_season_or_episode_waits_in_planning(self):
        """An air date to come is a release, in planning's colours if untracked."""
        state = quick_actions.get_button_state(None, "2099-01-01")
        self.assertTrue(state.release)
        self.assertIsNone(state.event)
        self.assertEqual(state.clock_color, "text-sky-400")
        self.assertFalse(quick_actions.get_button_state(None, "2008-01-20"))


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
            self.assertFalse(quick_actions._unaired(item, lookup=True))

        item.prefetched_events[0].datetime = timezone.now() + timedelta(days=1)
        self.assertTrue(quick_actions._unaired(item, lookup=True))
