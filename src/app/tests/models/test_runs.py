from datetime import UTC, datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)


def day(month, number):
    """Return a day of 2024."""
    return datetime(2024, month, number, tzinfo=UTC)


class RunTests(TestCase):
    """A show's and a season's times through are worked out from their watches."""

    def setUp(self):
        """Track a show with a first season and a special, both still empty."""
        self.user = get_user_model().objects.create_user(username="test")
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
        )
        self.tv = TV(item=tv_item, user=self.user, status=Status.COMPLETED.value)
        TV.save_base(self.tv)
        self.season = self.add_season(1)
        self.special = self.add_season(0)

    def add_season(self, number, status=Status.COMPLETED.value):
        """Add a season of the show, saved without looking up its metadata."""
        item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Show",
            season_number=number,
        )
        season = Season(item=item, user=self.user, related_tv=self.tv, status=status)
        Season.save_base(season)
        return season

    def watch(self, season, number, end, start=None, status=Status.COMPLETED.value):
        """Add an entry of an episode of a season, without moving the season on."""
        item, _ = Item.objects.get_or_create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=season.item.season_number,
            episode_number=number,
            defaults={"title": "Show"},
        )
        Episode.objects.bulk_create(
            [
                Episode(
                    related_season=season,
                    item=item,
                    status=status,
                    start_date=start,
                    end_date=end,
                ),
            ],
        )

    def season_runs(self, status=None):
        """Return the first season's runs, with its entries prefetched."""
        if status:
            Season.objects.filter(pk=self.season.pk).update(status=status)
        season = Season.objects.prefetch_related("episodes__item").get(
            pk=self.season.pk,
        )
        return season.runs

    def show_runs(self, status=None):
        """Return the show's runs, with its entries prefetched."""
        if status:
            TV.objects.filter(pk=self.tv.pk).update(status=status)
        tv = TV.objects.prefetch_related(
            "seasons__item",
            "seasons__episodes__item",
        ).get(pk=self.tv.pk)
        return tv.runs

    def test_season_watched_twice_has_two_completed_runs(self):
        """Each time through is dated from its own watches, the newest is the entry."""
        # Watched out of order, the second time through the later row for each
        self.watch(self.season, 1, day(3, 10), start=day(3, 9))
        self.watch(self.season, 2, day(1, 2))
        self.watch(self.season, 1, day(1, 1))
        self.watch(self.season, 2, day(3, 12))

        newest, oldest = self.season_runs()

        self.assertEqual(
            (oldest.status, oldest.start_date, oldest.end_date),
            (Status.COMPLETED.value, day(1, 1), day(1, 2)),
        )
        self.assertEqual(
            (newest.status, newest.start_date, newest.end_date),
            (Status.COMPLETED.value, day(3, 9), day(3, 12)),
        )
        self.assertEqual(newest.id, self.season.id)
        self.assertIsNone(oldest.id)

    def test_rewatching_season_adds_a_run_dated_from_the_rewatched_episodes(self):
        """The run of the rewatch has the entry's status and the new watches' dates."""
        self.watch(self.season, 1, day(1, 1))
        self.watch(self.season, 2, day(1, 2))
        self.watch(self.season, 1, day(3, 5))
        self.watch(self.season, 2, None, status=Status.PLANNING_REPEAT.value)

        rewatch, first = self.season_runs(Status.REPEATING.value)

        self.assertEqual(first.status, Status.COMPLETED.value)
        self.assertEqual((first.start_date, first.end_date), (day(1, 1), day(1, 2)))
        self.assertEqual(rewatch.status, Status.REPEATING.value)
        self.assertEqual((rewatch.start_date, rewatch.end_date), (day(3, 5), day(3, 5)))
        self.assertEqual(rewatch.id, self.season.id)
        self.assertIsNone(first.id)

    def test_planned_season_has_one_undated_run(self):
        """A planned season, or a planned rewatch, has a run without dates."""
        (planned,) = self.season_runs(Status.PLANNING.value)

        self.assertEqual(planned.status, Status.PLANNING.value)
        self.assertEqual((planned.start_date, planned.end_date), (None, None))
        self.assertEqual(planned.id, self.season.id)

        self.watch(self.season, 1, day(1, 1))
        self.watch(self.season, 1, day(3, 5), status=Status.PLANNING_REPEAT.value)
        planned_again, first = self.season_runs(Status.PLANNING_REPEAT.value)

        self.assertEqual(planned_again.status, Status.PLANNING_REPEAT.value)
        self.assertEqual(
            (planned_again.start_date, planned_again.end_date), (None, None)
        )
        self.assertEqual(first.status, Status.COMPLETED.value)

    def test_season_in_progress_has_a_run_from_its_watches(self):
        """A first time through not finished is one run with the watches so far."""
        self.watch(self.season, 1, day(1, 1))

        (run,) = self.season_runs(Status.IN_PROGRESS.value)

        self.assertEqual(run.status, Status.IN_PROGRESS.value)
        self.assertEqual((run.start_date, run.end_date), (day(1, 1), day(1, 1)))

    def test_dropped_rewatch_keeps_the_completed_run(self):
        """A rewatch dropped part way is its own run after the completed one."""
        self.watch(self.season, 1, day(1, 1))
        self.watch(self.season, 2, day(1, 2))
        self.watch(self.season, 1, day(3, 5))

        dropped, first = self.season_runs(Status.DROPPED.value)

        self.assertEqual(dropped.status, Status.DROPPED.value)
        self.assertEqual((dropped.start_date, dropped.end_date), (day(3, 5), day(3, 5)))
        self.assertEqual(first.status, Status.COMPLETED.value)
        self.assertEqual((first.start_date, first.end_date), (day(1, 1), day(1, 2)))

    def test_paused_first_watch_is_one_run(self):
        """Watches of a first time through paused are one run, not a play."""
        self.watch(self.season, 1, day(1, 1))
        self.watch(self.season, 2, day(1, 2))

        (paused,) = self.season_runs(Status.PAUSED.value)

        self.assertEqual(paused.status, Status.PAUSED.value)
        self.assertEqual((paused.start_date, paused.end_date), (day(1, 1), day(1, 2)))

    def test_in_progress_show_with_a_rewatched_episode_is_one_run(self):
        """A stray rewatch during a first time through isn't a completed play."""
        second = self.add_season(2, Status.IN_PROGRESS.value)
        self.watch(self.season, 1, day(1, 1))
        self.watch(self.season, 2, day(1, 2))
        self.watch(self.season, 1, day(1, 3))
        self.watch(second, 1, day(1, 4))

        (run,) = self.show_runs(Status.IN_PROGRESS.value)

        self.assertEqual(run.status, Status.IN_PROGRESS.value)
        self.assertEqual((run.start_date, run.end_date), (day(1, 1), day(1, 4)))

    def test_in_progress_show_with_only_one_episode_watched_twice_is_one_run(self):
        """Every watched episode watched as often still makes no completed play."""
        self.watch(self.season, 1, day(1, 1))
        self.watch(self.season, 1, day(1, 3))

        (run,) = self.show_runs(Status.IN_PROGRESS.value)

        self.assertEqual(run.status, Status.IN_PROGRESS.value)
        self.assertEqual((run.start_date, run.end_date), (day(1, 1), day(1, 3)))

    def test_planning_show_with_a_watched_season_keeps_its_dates(self):
        """Only a planned rewatch has its run cleared of dates."""
        self.watch(self.season, 1, day(1, 1))

        (run,) = self.show_runs(Status.PLANNING.value)

        self.assertEqual(run.status, Status.PLANNING.value)
        self.assertEqual((run.start_date, run.end_date), (day(1, 1), day(1, 1)))

    def test_stray_rewatches_of_single_episodes_add_no_run(self):
        """While the season is completed only its plays are runs."""
        self.watch(self.season, 1, day(1, 1))
        self.watch(self.season, 2, day(1, 2))
        self.watch(self.season, 1, day(2, 1))
        self.watch(self.season, 1, day(3, 1))

        (run,) = self.season_runs()

        self.assertEqual((run.start_date, run.end_date), (day(1, 1), day(1, 2)))

    def test_show_runs_span_its_seasons_and_leave_out_specials(self):
        """A show's run takes the k-th watch of every episode of every season."""
        second = self.add_season(2)
        self.watch(self.season, 1, day(1, 1))
        self.watch(second, 1, day(1, 5))
        self.watch(self.season, 1, day(2, 1))
        self.watch(second, 1, day(2, 5))
        # Specials watched once more, or on other days, change nothing
        self.watch(self.special, 1, day(6, 1))
        self.watch(self.special, 1, day(7, 1))

        newest, oldest = self.show_runs()

        self.assertEqual((oldest.start_date, oldest.end_date), (day(1, 1), day(1, 5)))
        self.assertEqual((newest.start_date, newest.end_date), (day(2, 1), day(2, 5)))
        self.assertEqual(newest.id, self.tv.id)
        self.assertIsNone(oldest.id)

    def test_rewatching_show_adds_a_run_of_the_seasons_watched_again(self):
        """The rewatch's run holds the watches after the plays of every season."""
        second = self.add_season(2)
        self.watch(self.season, 1, day(1, 1))
        self.watch(second, 1, day(1, 5))
        self.watch(self.season, 1, day(4, 1))

        rewatch, first = self.show_runs(Status.REPEATING.value)

        self.assertEqual(rewatch.status, Status.REPEATING.value)
        self.assertEqual((rewatch.start_date, rewatch.end_date), (day(4, 1), day(4, 1)))
        self.assertEqual(first.status, Status.COMPLETED.value)
        self.assertEqual((first.start_date, first.end_date), (day(1, 1), day(1, 5)))

    def test_completed_without_watches_still_lists_its_entry(self):
        """A completed entry with no watches is one run, so it isn't lost."""
        (run,) = self.season_runs()

        self.assertEqual(run.status, Status.COMPLETED.value)
        self.assertEqual(run.id, self.season.id)
        self.assertEqual((run.start_date, run.end_date), (None, None))

    def test_runs_match_plays(self):
        """A completed entry has one run for each of its plays."""
        for month in (1, 2, 3):
            self.watch(self.season, 1, day(month, 1))
            self.watch(self.season, 2, day(month, 2))
        self.watch(self.season, 2, day(4, 2))

        self.assertEqual(len(self.season_runs()), 3)
        self.assertEqual(len(self.show_runs()), 3)
