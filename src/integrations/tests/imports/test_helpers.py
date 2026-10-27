from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)
from integrations.imports import (
    helpers,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class HelpersTest(TestCase):
    """Test helper functions for imports."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def test_update_season_references(self):
        """Test updating season references with actual TV instances."""
        item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test Show",
        )
        tv = TV.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        new_season = Season(
            item=item,
            user=self.user,
            related_tv=TV(item=item, user=self.user),
        )

        helpers.update_season_references([new_season], self.user)

        self.assertEqual(new_season.related_tv.id, tv.id)

    def test_update_episode_references(self):
        """Test updating episode references with actual Season instances."""
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test Show",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        season_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test Show",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )

        episode_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Test Show",
            season_number=1,
            episode_number=1,
        )

        new_episode = Episode(
            item=episode_item,
            related_season=Season(item=season_item, related_tv=tv, user=self.user),
        )

        helpers.update_episode_references([new_episode], self.user)

        self.assertEqual(new_episode.related_season.id, season.id)

    @patch("django.contrib.messages.error")
    def test_create_import_schedule(self, mock_messages):
        """Test creating import schedule."""
        request = Mock()
        request.user = self.user

        helpers.create_import_schedule(
            "testuser",
            request,
            "new",
            "daily",
            "14:30",
            "TestSource",
        )

        schedule = PeriodicTask.objects.first()
        self.assertIsNotNone(schedule)
        self.assertEqual(
            schedule.name,
            "Import from TestSource for testuser at 14:30:00 daily",
        )

        helpers.create_import_schedule(
            "testuser",
            request,
            "new",
            "daily",
            "14:30",
            "TestSource",
        )
        mock_messages.assert_called_with(
            request,
            "The same import task is already scheduled.",
        )

    @patch("django.contrib.messages.error")
    def test_create_import_schedule_invalid_time(self, mock_messages):
        """Test creating import schedule with invalid time."""
        request = Mock()
        request.user = self.user

        helpers.create_import_schedule(
            "testuser",
            request,
            "new",
            "daily",
            "25:00",  # Invalid time
            "TestSource",
        )

        mock_messages.assert_called_with(request, "Invalid import time.")
        self.assertEqual(PeriodicTask.objects.count(), 0)

    def test_create_import_schedule_every_2_days(self):
        """Test creating import schedule for every 2 days."""
        request = Mock()
        request.user = self.user

        helpers.create_import_schedule(
            "testuser",
            request,
            "new",
            "every_2_days",
            "14:30",
            "TestSource",
        )

        schedule = CrontabSchedule.objects.first()
        self.assertEqual(schedule.day_of_week, "*/2")


class SyncExistingMediaTest(TestCase):
    """Test syncing existing media with the values of an import."""

    def setUp(self):
        """Create an unsaved in progress anime."""
        credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**credentials)
        item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Cowboy Bebop",
            image="https://example.com/image.jpg",
        )
        self.anime = Anime(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=5,
        )

    def test_fills_missing_values(self):
        """Missing values are filled, progress raised and the status advanced."""
        start_date = datetime(2023, 1, 1, tzinfo=UTC)
        end_date = datetime(2023, 2, 1, tzinfo=UTC)

        changed = helpers.sync_existing_media(
            self.anime,
            {
                "status": Status.COMPLETED.value,
                "score": 8,
                "notes": "Great",
                "progress": 26,
                "start_date": start_date,
                "end_date": end_date,
            },
        )

        self.assertTrue(changed)
        self.assertEqual(self.anime.status, Status.COMPLETED.value)
        self.assertEqual(self.anime.score, 8)
        self.assertEqual(self.anime.notes, "Great")
        self.assertEqual(self.anime.progress, 26)
        self.assertIsNotNone(self.anime.progressed_at)
        self.assertEqual(self.anime.start_date, start_date)
        self.assertEqual(self.anime.end_date, end_date)

    def test_keeps_tracked_values(self):
        """Values tracked in Yamtrack are never overwritten or lowered."""
        self.anime.status = Status.PAUSED.value
        self.anime.score = 6

        changed = helpers.sync_existing_media(
            self.anime,
            {
                "status": Status.COMPLETED.value,
                "score": 8,
                "progress": 3,
                "end_date": datetime(2023, 2, 1, tzinfo=UTC),
            },
        )

        self.assertFalse(changed)
        self.assertEqual(self.anime.status, Status.PAUSED.value)
        self.assertEqual(self.anime.score, 6)
        self.assertEqual(self.anime.progress, 5)
        # Unfinished media doesn't get an end date
        self.assertIsNone(self.anime.end_date)

    def test_rewatch_moves_forward_like_a_first_watch(self):
        """A rewatch is finished by an import, but never turned into a first watch."""
        self.anime.status = Status.REPEATING.value

        self.assertFalse(helpers.advance_status(self.anime, Status.IN_PROGRESS.value))
        self.assertTrue(helpers.advance_status(self.anime, Status.COMPLETED.value))
        self.assertEqual(self.anime.status, Status.COMPLETED.value)

    def test_planned_rewatch_goes_on_as_a_rewatch(self):
        """A planned rewatch an import finds started is rewatched, not a first watch."""
        self.anime.status = Status.PLANNING_REPEAT.value

        self.assertTrue(helpers.advance_status(self.anime, Status.IN_PROGRESS.value))
        self.assertEqual(self.anime.status, Status.REPEATING.value)

    def test_seasons_imported_in_a_rewatch_start_it_over(self):
        """A season imported mid-rewatch rewatches all its imported episodes again."""
        tv_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Friends",
        )
        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            season_number=1,
        )
        tv = TV(item=tv_item, user=self.user, status=Status.REPEATING.value)
        season = Season(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.REPEATING.value,
        )
        episodes = [
            Episode(
                related_season=season,
                item=Item.objects.create(
                    media_id="1668",
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.EPISODE.value,
                    title="Friends",
                    season_number=1,
                    episode_number=number,
                ),
                end_date=datetime(2023, 1, number, tzinfo=UTC),
            )
            for number in (1, 2)
        ]

        helpers.bulk_create_media(
            {
                MediaTypes.TV.value: [tv],
                MediaTypes.SEASON.value: [season],
                MediaTypes.EPISODE.value: episodes,
            },
            self.user,
        )

        season = Season.objects.get(user=self.user)
        # Each episode imported is planned to be watched again
        self.assertEqual(
            [episode.item.episode_number for episode in season.repeat_entries],
            [1, 2],
        )
        self.assertEqual(season.progress, 0)
        self.assertEqual(season.next_repeat_episode_number, 1)

    def tracked_season(self, status):
        """Track a show's first season in a status, without its cascades."""
        tv = TV(
            item=Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title="Friends",
            ),
            user=self.user,
            status=status,
        )
        TV.save_base(tv)
        season = Season(
            item=Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Friends",
                season_number=1,
            ),
            user=self.user,
            related_tv=tv,
            status=status,
        )
        Season.save_base(season)
        return season

    def episode_entry(self, season, number, status=Status.COMPLETED.value):
        """Return an unsaved entry of an episode of a season."""
        item, _ = Item.objects.get_or_create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=number,
            defaults={"title": "Friends"},
        )
        return Episode(related_season=season, item=item, status=status)

    def test_imported_watches_fulfil_their_episodes_plans(self):
        """An episode imported as watched loses its planned entry, not others'."""
        season = self.tracked_season(Status.PLANNING.value)
        Episode.objects.bulk_create(
            [
                self.episode_entry(season, number, Status.PLANNING.value)
                for number in (1, 2)
            ],
        )

        helpers.bulk_create_media(
            {MediaTypes.EPISODE.value: [self.episode_entry(season, 1)]},
            self.user,
        )

        self.assertEqual(
            sorted(Episode.objects.values_list("item__episode_number", "status")),
            [(1, Status.COMPLETED.value), (2, Status.PLANNING.value)],
        )

    def test_existing_watches_leave_out_planned_entries(self):
        """Only watches count as the episodes already watched."""
        season = self.tracked_season(Status.IN_PROGRESS.value)
        Episode.objects.bulk_create(
            [
                self.episode_entry(season, 1),
                self.episode_entry(season, 2, Status.PLANNING.value),
            ],
        )

        watches = helpers.get_existing_watches(self.user)

        self.assertEqual(
            sorted(key[4] for key in watches),
            [1],
        )

    def test_import_leaves_a_rewatch_under_way(self):
        """A season already being rewatched isn't planned to be rewatched again."""
        season = self.tracked_season(Status.REPEATING.value)
        Episode.objects.bulk_create(
            [
                self.episode_entry(season, 1),
                self.episode_entry(season, 1),
                self.episode_entry(season, 2),
                self.episode_entry(season, 2, Status.PLANNING_REPEAT.value),
            ],
        )

        helpers.bulk_create_media({}, self.user)

        season = Season.objects.get(pk=season.pk)
        self.assertEqual(
            [entry.item.episode_number for entry in season.repeat_entries],
            [2],
        )

    def test_import_leaves_a_rewatch_with_nothing_left_alone(self):
        """Importing other media doesn't start over a season's rewatch."""
        season = self.tracked_season(Status.REPEATING.value)
        Episode.objects.bulk_create(
            [
                self.episode_entry(season, 1),
                self.episode_entry(season, 1),
                self.episode_entry(season, 2),
                self.episode_entry(season, 2),
            ],
        )

        helpers.bulk_create_media({}, self.user)

        season = Season.objects.get(pk=season.pk)
        self.assertEqual(season.repeat_entries, [])
        self.assertEqual(season.repeated_episode_numbers, {1, 2})

    def test_season_an_import_moves_into_a_rewatch_plans_it(self):
        """An existing season the import sets rewatching rewatches its episodes."""
        season = self.tracked_season(Status.PLANNING.value)
        Episode.objects.bulk_create([self.episode_entry(season, 1)])
        season = Season.objects.get(pk=season.pk)
        season.status = Status.REPEATING.value

        helpers.bulk_update_media(
            {MediaTypes.SEASON.value: {season.pk: season}},
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )

        season = Season.objects.get(pk=season.pk)
        self.assertEqual(
            [entry.item.episode_number for entry in season.repeat_entries],
            [1],
        )

    def test_import_of_the_last_episode_left_to_rewatch_finishes_the_rewatch(self):
        """A watch imported for the last episode left to rewatch finishes it."""
        season = self.tracked_season(Status.REPEATING.value)
        Episode.objects.bulk_create(
            [
                self.episode_entry(season, 1),
                self.episode_entry(season, 1),
                self.episode_entry(season, 2),
                self.episode_entry(season, 2, Status.PLANNING_REPEAT.value),
            ],
        )

        helpers.bulk_create_media(
            {MediaTypes.EPISODE.value: [self.episode_entry(season, 2)]},
            self.user,
        )

        season = Season.objects.get(pk=season.pk)
        self.assertEqual(season.status, Status.COMPLETED.value)
        self.assertEqual(season.plays, 2)

    def test_season_an_import_completes_drops_its_rewatch(self):
        """An existing season the import sets completed has no rewatch left."""
        season = self.tracked_season(Status.REPEATING.value)
        Episode.objects.bulk_create(
            [
                self.episode_entry(season, 1),
                self.episode_entry(season, 1, Status.PLANNING_REPEAT.value),
            ],
        )
        season = Season.objects.get(pk=season.pk)
        season.status = Status.COMPLETED.value

        helpers.bulk_update_media(
            {MediaTypes.SEASON.value: {season.pk: season}},
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )

        self.assertEqual(
            list(Episode.objects.values_list("status", flat=True)),
            [Status.COMPLETED.value],
        )

    def test_season_an_import_starts_rewatching_keeps_its_rewatch(self):
        """A planned rewatch the import sets rewatching keeps what's left of it."""
        season = self.tracked_season(Status.PLANNING_REPEAT.value)
        Episode.objects.bulk_create(
            [
                self.episode_entry(season, 1),
                self.episode_entry(season, 1, Status.PLANNING_REPEAT.value),
            ],
        )
        season = Season.objects.get(pk=season.pk)
        season.status = Status.REPEATING.value

        helpers.bulk_update_media(
            {MediaTypes.SEASON.value: {season.pk: season}},
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )

        self.assertEqual(len(Season.objects.get(pk=season.pk).repeat_entries), 1)

    def test_show_sync_does_not_query_dates(self):
        """Syncing a show doesn't query its episodes for dates it isn't given."""
        item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Friends",
            image="https://example.com/image.jpg",
        )
        tv = TV(item=item, user=self.user, status=Status.IN_PROGRESS.value)
        TV.objects.bulk_create([tv])

        with self.assertNumQueries(0):
            changed = helpers.sync_existing_media(
                tv,
                {"status": Status.COMPLETED.value, "score": 8, "notes": ""},
            )

        self.assertTrue(changed)

    def test_get_existing_entries(self):
        """Every entry of a media is returned newest first, including repeats."""
        # Created in file order by an import, so the current entry is the oldest
        Anime.objects.bulk_create(
            [
                Anime(item=self.anime.item, user=self.user, status=status)
                for status in (Status.IN_PROGRESS.value, Status.COMPLETED.value)
            ],
        )

        existing_entries = helpers.get_existing_entries(
            self.user,
            [MediaTypes.ANIME.value, MediaTypes.MANGA.value],
        )

        entries = existing_entries[MediaTypes.ANIME.value, Sources.MAL.value, "1"]
        self.assertEqual(
            [entry.status for entry in entries],
            [Status.COMPLETED.value, Status.IN_PROGRESS.value],
        )
        self.assertNotIn(
            (MediaTypes.MANGA.value, Sources.MAL.value, "1"),
            existing_entries,
        )

        # The unfinished entry is the current one, not the newest repeat
        self.assertEqual(
            helpers.get_current_entry(entries).status,
            Status.IN_PROGRESS.value,
        )
        self.assertEqual(helpers.get_current_entry(entries[:1]), entries[0])
