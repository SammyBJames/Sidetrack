from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app import quick_actions
from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)

# Three episodes, all out
EPISODES = [
    {"episode_number": number, "air_date": "2020-01-0" + str(number)}
    for number in (1, 2, 3)
]

# A show of specials and one season, as every metadata call returns it
METADATA = {
    "title": "Show",
    "image": "http://example.com/show.jpg",
    "max_progress": 3,
    "related": {
        "seasons": [
            {
                "season_number": number,
                "image": "http://example.com/s.jpg",
                "first_air_date": "2020-01-01",
            }
            for number in (0, 1)
        ],
    },
    "episodes": EPISODES,
    "season/0": {"episodes": EPISODES, "image": "http://example.com/s.jpg"},
    "season/1": {"episodes": EPISODES, "image": "http://example.com/s.jpg"},
}


@patch("app.models.Item.fetch_releases")
@patch("app.models.providers.services.get_media_metadata", return_value=METADATA)
class EpisodeEntryCascadeTests(TestCase):
    """Test how episode entries and their season's status move each other."""

    def setUp(self):
        """Track a show in progress with its first season."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
            image="http://example.com/show.jpg",
        )
        self.tv = TV(item=tv_item, user=self.user, status=Status.IN_PROGRESS.value)
        TV.save_base(self.tv)
        self.season = self.track_season(1, Status.IN_PROGRESS.value)

    def track_season(self, number, status):
        """Track a season of the show in a status, without its cascade."""
        season = Season(
            item=Item.objects.create(
                media_id="1",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Show",
                image="http://example.com/s.jpg",
                season_number=number,
            ),
            user=self.user,
            related_tv=self.tv,
            status=status,
        )
        Season.save_base(season)
        return season

    def add(self, number, status, season=None, **fields):
        """Add an entry of an episode of a season, without its cascade."""
        season = season or self.season
        item, _ = Item.objects.get_or_create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=season.item.season_number,
            episode_number=number,
            defaults={"title": "Show", "image": "http://example.com/e.jpg"},
        )
        return Episode.objects.bulk_create(
            [Episode(related_season=season, item=item, status=status, **fields)],
        )[0]

    def entries(self, season=None):
        """Return a season's entries as (episode number, status), in order."""
        season = season or self.season
        return sorted(
            Episode.objects.filter(related_season=season).values_list(
                "item__episode_number",
                "status",
            ),
        )

    def set_status(self, status, season=None):
        """Set a season's status as the tracking form does."""
        season = Season.objects.get(pk=(season or self.season).pk)
        season.status = status
        season.save()
        return season

    def statuses(self):
        """Return the show's and its first season's statuses."""
        self.tv.refresh_from_db()
        self.season.refresh_from_db()
        return self.tv.status, self.season.status

    def test_a_planned_entry_made_a_watch_moves_the_season_on(self, *_):
        """Editing an entry into a watch starts the season, and the finale ends it."""
        entry = self.add(1, Status.PLANNING.value)
        self.add(2, Status.COMPLETED.value)
        finale = self.add(3, Status.PLANNING.value)
        Season.objects.filter(pk=self.season.pk).update(status=Status.PLANNING.value)

        entry.status = Status.COMPLETED.value
        entry.save()
        self.assertEqual(self.statuses()[1], Status.IN_PROGRESS.value)

        finale = Episode.objects.get(pk=finale.pk)
        finale.status = Status.COMPLETED.value
        finale.save()
        self.assertEqual(
            self.statuses(),
            (Status.COMPLETED.value, Status.COMPLETED.value),
        )

    def test_entries_that_arent_watches_leave_the_season(self, *_):
        """A new planned entry, and deleting one, leave its season as it is."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.PLANNING.value)
        self.add(3, Status.PLANNING.value)
        Episode.objects.create(
            related_season=self.season,
            item=Item.objects.get(episode_number=3),
            status=Status.IN_PROGRESS.value,
        )
        self.assertEqual(self.statuses()[1], Status.PLANNING.value)

        Season.objects.filter(pk=self.season.pk).update(status=Status.COMPLETED.value)
        for number in (1, 2):
            self.add(number, Status.COMPLETED.value)
        Episode.objects.filter(item__episode_number=3).first().delete()
        self.assertEqual(self.statuses()[1], Status.COMPLETED.value)

    def test_a_watch_made_unfinished_reopens_the_season(self, *_):
        """A watch of the finale changed back to planned reopens its season."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.COMPLETED.value)
        TV.objects.filter(pk=self.tv.pk).update(status=Status.COMPLETED.value)
        for number in (1, 2):
            self.add(number, Status.COMPLETED.value)
        finale = Episode.objects.get(pk=self.add(3, Status.COMPLETED.value).pk)

        finale.status = Status.PLANNING.value
        finale.save()

        self.assertEqual(
            self.statuses(),
            (Status.IN_PROGRESS.value, Status.IN_PROGRESS.value),
        )

    def test_a_new_watch_fulfils_the_episodes_plans(self, *_):
        """A watch made some other way, e.g. by a webhook, replaces its plans."""
        self.add(1, Status.PLANNING.value)
        self.add(2, Status.COMPLETED.value)
        self.add(2, Status.PLANNING_REPEAT.value)

        for number in (1, 2):
            Episode.objects.create(
                related_season=self.season,
                item=Item.objects.get(episode_number=number),
            )

        self.assertEqual(
            self.entries(),
            [
                (1, Status.COMPLETED.value),
                (2, Status.COMPLETED.value),
                (2, Status.COMPLETED.value),
            ],
        )

    def test_watching_turns_the_episodes_entry_into_a_watch(self, *_):
        """Marking an episode watched completes its entry, keeping what it had."""
        start = datetime(2024, 1, 1, tzinfo=UTC)
        entry = self.add(1, Status.IN_PROGRESS.value, start_date=start, notes="Half")

        self.season.watch(1, datetime(2024, 1, 2, tzinfo=UTC))

        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.COMPLETED.value)
        self.assertEqual(entry.start_date, start)
        self.assertEqual(entry.notes, "Half")
        self.assertEqual(Episode.objects.count(), 1)

    def test_planning_a_season_plans_its_episodes(self, *_):
        """Each known episode without an entry is planned, once."""
        self.add(1, Status.COMPLETED.value)
        self.add(2, Status.DROPPED.value)
        Season.objects.filter(pk=self.season.pk).update(status=Status.DROPPED.value)
        data = {
            "instance_id": self.season.pk,
            "media_id": "1",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
            "status": Status.PLANNING.value,
        }
        self.client.login(**self.credentials)

        self.client.post(reverse("media_save"), data)
        self.client.post(reverse("media_save"), data)

        self.assertEqual(
            self.entries(),
            [
                (1, Status.COMPLETED.value),
                (2, Status.PLANNING.value),
                (3, Status.PLANNING.value),
            ],
        )

    def test_planning_a_season_from_its_card_plans_its_episodes(self, *_):
        """A season put on the watchlist with its card's action is planned whole."""
        season = Season(
            item=Item.objects.create(
                media_id="1",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Show",
                image="http://example.com/s.jpg",
                season_number=2,
            ),
            user=self.user,
            related_tv=self.tv,
        )
        action = quick_actions.get_quick_action(MediaTypes.SEASON.value, None)

        season = quick_actions.apply_quick_action(action, season)

        self.assertEqual(
            self.entries(season),
            [(number, Status.PLANNING.value) for number in (1, 2, 3)],
        )

    def test_planning_a_show_plans_its_episodes_but_specials(self, *_):
        """Putting a show on the watchlist plans its seasons' episodes."""
        specials = self.track_season(0, Status.PLANNING.value)
        self.add(1, Status.COMPLETED.value)
        Season.objects.filter(pk=self.season.pk).update(status=Status.PLANNING.value)

        self.tv.plan_seasons()

        self.assertEqual(
            self.entries(),
            [
                (1, Status.COMPLETED.value),
                (2, Status.PLANNING.value),
                (3, Status.PLANNING.value),
            ],
        )
        self.assertEqual(self.entries(specials), [])

    def test_planning_a_rewatch_plans_each_watched_episode_once(self, *_):
        """Every watched episode gets one entry to watch it again."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.COMPLETED.value)
        for number in (1, 1, 2):
            self.add(number, Status.COMPLETED.value)
        self.add(2, Status.PLANNING_REPEAT.value)
        self.add(3, Status.PLANNING.value)

        season = self.set_status(Status.PLANNING_REPEAT.value)

        self.assertEqual(
            [
                entry
                for entry in self.entries()
                if entry[1] == Status.PLANNING_REPEAT.value
            ],
            [(1, Status.PLANNING_REPEAT.value), (2, Status.PLANNING_REPEAT.value)],
        )
        self.assertEqual(season.next_repeat_episode_number, 1)
        self.assertEqual(season.progress, 0)

    def test_pausing_dropping_and_resuming_move_unfinished_entries(self, *_):
        """Unfinished entries follow the season, and watches stay as they are."""
        self.add(1, Status.COMPLETED.value)
        self.add(
            2, Status.IN_PROGRESS.value, start_date=datetime(2024, 1, 1, tzinfo=UTC)
        )
        self.add(3, Status.PLANNING.value)

        self.set_status(Status.PAUSED.value)
        self.assertEqual(
            self.entries(),
            [
                (1, Status.COMPLETED.value),
                (2, Status.PAUSED.value),
                (3, Status.PAUSED.value),
            ],
        )

        self.set_status(Status.DROPPED.value)
        self.assertEqual(
            self.entries()[1:],
            [(2, Status.DROPPED.value), (3, Status.DROPPED.value)],
        )

        # The started one is back in progress, and the other planned
        self.set_status(Status.IN_PROGRESS.value)
        self.assertEqual(
            self.entries(),
            [
                (1, Status.COMPLETED.value),
                (2, Status.IN_PROGRESS.value),
                (3, Status.PLANNING.value),
            ],
        )

    def test_dropping_a_show_drops_its_seasons_entries(self, *_):
        """A dropped show drops its seasons in progress with their episodes."""
        self.add(1, Status.PLANNING.value)

        self.tv.status = Status.DROPPED.value
        self.tv.save()

        self.assertEqual(self.entries(), [(1, Status.DROPPED.value)])

    def test_completing_a_season_finishes_its_entries_without_duplicates(self, *_):
        """Unfinished entries become the watches completing it, and the rest are new."""
        self.add(1, Status.PLANNING.value)
        start = datetime(2024, 1, 1, tzinfo=UTC)
        started = self.add(2, Status.PAUSED.value, start_date=start)

        self.set_status(Status.COMPLETED.value)

        self.assertEqual(
            self.entries(),
            [(number, Status.COMPLETED.value) for number in (1, 2, 3)],
        )
        started.refresh_from_db()
        self.assertEqual(started.start_date, start)
        self.assertIsNotNone(started.end_date)

    def test_completing_a_show_finishes_its_entries_without_duplicates(self, *_):
        """Marking the show completed turns its planned episodes into watches."""
        for number in (1, 2, 3):
            self.add(number, Status.PLANNING.value)

        self.tv.status = Status.COMPLETED.value
        self.tv.save()

        self.assertEqual(
            self.entries(),
            [(number, Status.COMPLETED.value) for number in (1, 2, 3)],
        )

    def test_rewatching_an_episode_undone_plans_it_again(self, *_):
        """Taking back the latest episode of a rewatch makes it left to watch again."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.COMPLETED.value)
        for number in (1, 2, 3):
            self.add(number, Status.COMPLETED.value)
        season = self.set_status(Status.REPEATING.value)
        season.increase_progress()
        season = Season.objects.get(pk=season.pk)
        self.assertEqual((season.progress, season.next_repeat_episode_number), (1, 2))

        season.decrease_progress()

        season = Season.objects.get(pk=season.pk)
        self.assertEqual((season.progress, season.next_repeat_episode_number), (0, 1))
        self.assertEqual(season.episodes.watches().count(), 3)
        self.assertEqual(season.plays, 1)

    def test_leaving_a_rewatch_drops_what_was_left(self, *_):
        """A rewatch given up leaves no entries to watch again, nor new plays."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.COMPLETED.value)
        TV.objects.filter(pk=self.tv.pk).update(status=Status.COMPLETED.value)
        for number in (1, 2, 3):
            self.add(number, Status.COMPLETED.value)
        self.tv.refresh_from_db()
        self.tv.status = Status.PLANNING_REPEAT.value
        self.tv.save()
        self.assertEqual(len(Season.objects.get(pk=self.season.pk).repeat_entries), 3)

        self.tv.status = Status.PAUSED.value
        self.tv.save()

        season = Season.objects.get(pk=self.season.pk)
        self.assertEqual(season.status, Status.COMPLETED.value)
        self.assertEqual(season.repeat_entries, [])
        self.assertEqual(season.plays, 1)

    def test_pausing_a_season_rewatch_drops_what_was_left(self, *_):
        """A season's rewatch paused is given up, as it starts over when resumed."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.COMPLETED.value)
        for number in (1, 2):
            self.add(number, Status.COMPLETED.value)
        self.set_status(Status.REPEATING.value)

        season = self.set_status(Status.PAUSED.value)

        self.assertEqual(season.repeat_entries, [])
        self.assertEqual(season.episodes.watches().count(), 2)

    def test_completing_a_season_skips_episodes_watched_in_bulk(self, *_):
        """An episode imported as watched while planned isn't watched again."""
        self.add(1, Status.PLANNING.value)
        self.add(1, Status.COMPLETED.value)

        self.set_status(Status.COMPLETED.value)

        self.assertEqual(
            self.season.episodes.watches().filter(item__episode_number=1).count(),
            1,
        )
        self.assertEqual(self.season.episodes.watches().count(), 3)

    def test_completing_a_season_watches_planned_episodes_before_the_latest(
        self,
        *_,
    ):
        """A planned episode before the latest one watched is watched with the rest."""
        self.add(1, Status.PLANNING.value)
        self.add(2, Status.COMPLETED.value)

        self.set_status(Status.COMPLETED.value)

        self.assertEqual(
            self.entries(),
            [(number, Status.COMPLETED.value) for number in (1, 2, 3)],
        )

    def test_watching_a_paused_seasons_episode_resumes_its_others(self, *_):
        """Watching one episode of a paused or dropped season resumes the rest."""
        for status in (Status.PAUSED.value, Status.DROPPED.value):
            with self.subTest(status=status):
                Episode.objects.all().delete()
                Season.objects.filter(pk=self.season.pk).update(status=status)
                for number in (1, 2, 3):
                    self.add(number, status)

                Season.objects.get(pk=self.season.pk).watch(
                    1,
                    datetime(2024, 1, 1, tzinfo=UTC),
                )

                self.assertEqual(self.statuses()[1], Status.IN_PROGRESS.value)
                self.assertEqual(
                    self.entries(),
                    [
                        (1, Status.COMPLETED.value),
                        (2, Status.PLANNING.value),
                        (3, Status.PLANNING.value),
                    ],
                )

    def test_undoing_a_rewatch_takes_back_its_latest_episode(self, *_):
        """With two episodes watched again, taking one back leaves the first."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.COMPLETED.value)
        for number in (1, 2, 3):
            self.add(
                number,
                Status.COMPLETED.value,
                end_date=datetime(2023, 1, 1, tzinfo=UTC),
            )
        season = self.set_status(Status.REPEATING.value)
        for _ in range(2):
            Season.objects.get(pk=season.pk).increase_progress()

        Season.objects.get(pk=season.pk).decrease_progress()

        season = Season.objects.get(pk=season.pk)
        self.assertEqual(season.repeated_episode_numbers, {1})
        self.assertEqual(season.next_repeat_episode_number, 2)

    def test_starting_the_next_season_resumes_its_paused_entries(
        self, mock_metadata, _
    ):
        """Finishing a season starts the paused next one, with its episodes planned."""
        mock_metadata.return_value = {
            **METADATA,
            "related": {
                "seasons": [
                    {
                        "season_number": number,
                        "image": "http://example.com/s.jpg",
                        "first_air_date": "2020-01-01",
                    }
                    for number in (1, 2)
                ],
            },
        }
        second = self.track_season(2, Status.PAUSED.value)
        self.add(1, Status.PAUSED.value, season=second)
        for number in (1, 2):
            self.add(number, Status.COMPLETED.value)

        self.season.watch(3, datetime(2024, 1, 1, tzinfo=UTC))

        second.refresh_from_db()
        self.assertEqual(second.status, Status.IN_PROGRESS.value)
        self.assertEqual(self.entries(second), [(1, Status.PLANNING.value)])

    def test_planning_a_show_plans_its_paused_seasons_entries(self, *_):
        """A paused season with nothing watched is planned with its show."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.PAUSED.value)
        self.add(1, Status.PAUSED.value)

        self.tv.plan_seasons()

        self.assertEqual(
            self.entries(),
            [(number, Status.PLANNING.value) for number in (1, 2, 3)],
        )

    def test_undoing_a_rewatch_leaves_episodes_watched_once(self, *_):
        """An episode first watched during the rewatch isn't taken back by it."""
        Season.objects.filter(pk=self.season.pk).update(status=Status.REPEATING.value)
        self.add(1, Status.COMPLETED.value, end_date=datetime(2023, 1, 1, tzinfo=UTC))
        self.add(1, Status.COMPLETED.value, end_date=datetime(2024, 1, 1, tzinfo=UTC))
        self.add(2, Status.COMPLETED.value, end_date=datetime(2023, 1, 1, tzinfo=UTC))
        self.add(2, Status.PLANNING_REPEAT.value)
        # Out after the rewatch was planned, so watched for the first time
        self.add(3, Status.COMPLETED.value, end_date=datetime(2024, 2, 1, tzinfo=UTC))

        Season.objects.get(pk=self.season.pk).decrease_progress()

        self.assertEqual(
            self.entries(),
            [
                (1, Status.COMPLETED.value),
                (1, Status.PLANNING_REPEAT.value),
                (2, Status.COMPLETED.value),
                (2, Status.PLANNING_REPEAT.value),
                (3, Status.COMPLETED.value),
            ],
        )
