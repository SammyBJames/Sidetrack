from datetime import UTC, datetime
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from app import statistics
from app.forms import EpisodeForm
from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
    is_caught_up,
)
from app.providers import manual, tmdb

# The first season's four episodes, as the season's metadata lists them
SEASON_METADATA = {
    "episodes": [{"episode_number": number} for number in range(1, 5)],
}


def day(number):
    """Return a day of January 2024."""
    return datetime(2024, 1, number, tzinfo=UTC)


class EpisodeStatusTests(TestCase):
    """Only completed entries are watches, the rest are planned or unfinished."""

    def setUp(self):
        """Track a show whose first season has watches and unfinished entries.

        Episodes 1 and 2 are watched twice, on January 1 and 2, episode 3 is
        planned, dated later, and episode 4 in progress, dated earlier.
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
        season_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Show",
            season_number=1,
        )
        self.season = Season(
            item=season_item,
            user=self.user,
            related_tv=self.tv,
            status=Status.COMPLETED.value,
        )
        Season.save_base(self.season)
        self.add(1, Status.COMPLETED.value, day(1))
        self.add(1, Status.COMPLETED.value, day(1))
        self.add(2, Status.COMPLETED.value, day(2))
        self.add(2, Status.COMPLETED.value, day(2))
        self.add(3, Status.PLANNING.value, day(5))
        self.add(4, Status.IN_PROGRESS.value, datetime(2023, 12, 31, tzinfo=UTC))

    def add(self, number, status, end_date=None, season=None):
        """Add an entry of an episode of a season, the first by default.

        It's added without moving the season on.
        """
        season = season or self.season
        item, _ = Item.objects.get_or_create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=season.item.season_number,
            episode_number=number,
            defaults={"title": "Show"},
        )
        # bulk_create skips the save that looks up the season's metadata
        return Episode.objects.bulk_create(
            [
                Episode(
                    related_season=season,
                    item=item,
                    status=status,
                    end_date=end_date,
                ),
            ],
        )[0]

    def fresh_season(self):
        """Return the season with its entries as the library prefetches them."""
        return Season.objects.filter(pk=self.season.pk).prefetch_related(
            "episodes__item",
        )[0]

    def test_existing_and_new_entries_are_watches(self):
        """An entry is completed unless it says otherwise, so it's a watch."""
        episode = Episode(related_season=self.season, item=self.season.item)

        self.assertEqual(episode.status, Status.COMPLETED.value)
        self.assertTrue(episode.is_watch)
        self.assertEqual(Episode.objects.watches().count(), 4)

    def test_progress_plays_and_dates_count_watches_only(self):
        """The planned and unfinished entries don't move the season or show on."""
        season = self.fresh_season()

        self.assertEqual(season.progress, 2)
        self.assertEqual(season.plays, 2)
        self.assertEqual(season.start_date, day(1))
        self.assertEqual(season.end_date, day(2))
        self.assertEqual(season.progressed_at, day(2))
        self.assertEqual(season._get_latest_watched_episode_number(), 2)

        season.status = Status.IN_PROGRESS.value
        self.assertEqual(season.progress, 2)

        tv = TV.objects.filter(pk=self.tv.pk).prefetch_related(
            "seasons__item",
            "seasons__episodes__item",
        )[0]
        self.assertEqual(tv.progress, 2)
        self.assertEqual(tv.plays, 2)
        self.assertEqual(tv.last_watched, "S01E02")
        self.assertEqual(tv.start_date, day(1))
        self.assertEqual(tv.end_date, day(2))

    def test_a_season_only_planned_is_left_out_of_plays_and_stats(self):
        """A show's plays and the seasons watched skip one with no watches."""
        season = Season(
            item=Item.objects.create(
                media_id="1",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Show",
                season_number=2,
            ),
            user=self.user,
            related_tv=self.tv,
            status=Status.PLANNING.value,
        )
        Season.save_base(season)
        self.add(1, Status.PLANNING.value, season=season)

        tv = TV.objects.filter(pk=self.tv.pk).prefetch_related(
            "seasons__item",
            "seasons__episodes__item",
        )[0]
        self.assertEqual(tv.plays, 2)
        _, media_count = statistics.get_user_media(self.user, None, None)
        self.assertEqual(media_count[MediaTypes.SEASON.value], 1)

    def test_unfinished_entries_leave_a_season_not_caught_up(self):
        """A season up to its latest release only counts the episodes watched."""
        season = self.fresh_season()
        season.next_event = Mock(is_max_datetime=False)

        season.max_progress = 2
        self.assertTrue(is_caught_up(season))
        season.max_progress = 3
        self.assertFalse(is_caught_up(season))

    def test_a_season_without_watches_has_none(self):
        """A season with only planned entries has no progress, plays or dates."""
        Episode.objects.filter(status=Status.COMPLETED.value).delete()
        season = self.fresh_season()

        self.assertEqual(season.progress, 0)
        self.assertEqual(season.plays, 0)
        self.assertIsNone(season.end_date)

    def test_a_rewatch_goes_through_its_entries_to_watch_again(self):
        """Episodes with an entry to watch them again are left, then become watches."""
        Episode.objects.all().delete()
        self.season.status = Status.REPEATING.value
        Season.save_base(self.season)
        self.add(1, Status.COMPLETED.value, day(1))
        self.add(1, Status.COMPLETED.value, day(2))
        self.add(2, Status.COMPLETED.value, day(1))
        self.add(2, Status.PLANNING_REPEAT.value)
        self.add(3, Status.COMPLETED.value, day(1))
        self.add(3, Status.REPEATING.value)
        self.add(4, Status.PLANNING.value)
        season = self.fresh_season()

        self.assertEqual(season.repeated_episode_numbers, {1})
        self.assertEqual(season.progress, 1)
        self.assertEqual(season.next_repeat_episode_number, 2)
        remaining = season.get_remaining_repeat_eps(day(3))
        self.assertEqual(
            [(episode.item.episode_number, episode.status) for episode in remaining],
            [(2, Status.COMPLETED.value), (3, Status.COMPLETED.value)],
        )
        self.assertTrue(all(episode.pk for episode in remaining))

    def test_season_page_lists_watches_and_whether_watched(self):
        """An episode's history is its watches, and planned rewatches show it seen."""
        self.add(3, Status.PLANNING_REPEAT.value)
        metadata = {
            "media_id": "1",
            "season_number": 1,
            "episodes": [
                {
                    "episode_number": number,
                    "air_date": None,
                    "still_path": None,
                    "name": "",
                    "overview": "",
                    "runtime": None,
                    # As manual seasons list them
                    "media_id": "1",
                    "image": "",
                    "title": "",
                }
                for number in range(1, 5)
            ],
        }

        for provider in (tmdb, manual):
            with self.subTest(provider=provider.__name__):
                episodes = provider.process_episodes(
                    metadata,
                    self.fresh_season().episodes.all(),
                )

                self.assertEqual(
                    [len(episode["history"]) for episode in episodes],
                    [2, 2, 0, 0],
                )
                self.assertEqual(
                    [episode["watched"] for episode in episodes],
                    [True, True, True, False],
                )

    def test_statistics_count_watches_only(self):
        """Episodes watched count watches, and a planned entry adds no season."""
        user_media, media_count = statistics.get_user_media(self.user, None, None)
        consumed = {
            entry["media_type"]: entry["value"]
            for entry in statistics.get_consumption_stats(user_media, media_count)
        }
        self.assertEqual(consumed[MediaTypes.SEASON.value], 4)

        _, media_count = statistics.get_user_media(self.user, day(4), day(7))
        self.assertEqual(media_count[MediaTypes.SEASON.value], 0)
        self.assertEqual(media_count[MediaTypes.TV.value], 0)


# The fields every episode form posts besides the entry's own
EPISODE_FIELDS = {
    "media_type": MediaTypes.EPISODE.value,
    "source": Sources.MANUAL.value,
    "media_id": "1",
}


class EpisodeFormStatusTests(TestCase):
    """The episode form sets an entry's status, or keeps it when it's left out."""

    def setUp(self):
        """Create a planned entry of an episode."""
        user = get_user_model().objects.create_user(username="test")
        season_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Show",
            season_number=1,
        )
        tv = TV(
            item=Item.objects.create(
                media_id="1",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.TV.value,
                title="Show",
            ),
            user=user,
        )
        TV.save_base(tv)
        season = Season(item=season_item, user=user, related_tv=tv)
        Season.save_base(season)
        episode_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            title="Show",
            season_number=1,
            episode_number=1,
        )
        self.entry = Episode.objects.bulk_create(
            [
                Episode(
                    related_season=season,
                    item=episode_item,
                    status=Status.PLANNING.value,
                ),
            ],
        )[0]

    def test_form_without_a_status_keeps_the_entrys(self):
        """Changing only the date and notes leaves the status as it was."""
        form = EpisodeForm(
            {**EPISODE_FIELDS, "end_date": "2024-01-02", "notes": "Hi"},
            instance=self.entry,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().status, Status.PLANNING.value)

    def test_new_entry_without_a_status_is_completed(self):
        """A form only dating a new watch completes it, as adding a watch did."""
        form = EpisodeForm({**EPISODE_FIELDS, "end_date": "2024-01-02"})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["status"], Status.COMPLETED.value)

    def test_form_sets_the_status_and_start_date(self):
        """A posted status and start date are saved, and an unknown status isn't."""
        form = EpisodeForm(
            {
                **EPISODE_FIELDS,
                "status": Status.IN_PROGRESS.value,
                "start_date": "2024-01-01",
                "end_date": "",
            },
            instance=self.entry,
        )

        self.assertTrue(form.is_valid(), form.errors)
        entry = form.save()
        self.assertEqual(entry.status, Status.IN_PROGRESS.value)
        self.assertEqual(entry.start_date.date().isoformat(), "2024-01-01")
        self.assertFalse(
            EpisodeForm({**EPISODE_FIELDS, "status": "Watched"}).is_valid(),
        )

    def test_statuses_are_in_episodes_words(self):
        """The repeat statuses read as rewatching."""
        choices = dict(EpisodeForm().fields["status"].choices)

        self.assertEqual(choices[Status.REPEATING.value], "Rewatching")
