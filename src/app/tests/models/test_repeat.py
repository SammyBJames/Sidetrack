from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app import quick_actions
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

# A show whose two seasons of two episodes are out, so finishing them finishes it.
# Its seasons are fetched the same way.
SHOW_METADATA = {
    "max_progress": 4,
    "episodes": [{"episode_number": 1}, {"episode_number": 2}],
    "related": {
        "seasons": [
            {
                "season_number": number,
                "image": f"img{number}.jpg",
                "first_air_date": "2020-01-01",
            }
            for number in (1, 2)
        ],
    },
}


@patch("app.models.providers.services.get_media_metadata", return_value=SHOW_METADATA)
class ShowRewatchTests(TestCase):
    """Test rewatching a finished show through its seasons and episodes."""

    def setUp(self):
        """Track a finished show with two finished seasons of two episodes.

        Specials have a watched episode, and season 3 is planned, unwatched.
        """
        self.user = get_user_model().objects.create_user(username="test")
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
        )
        self.tv = TV(item=tv_item, user=self.user, status=Status.COMPLETED.value)
        TV.save_base(self.tv)
        self.seasons = {}
        for number, status in (
            (0, Status.COMPLETED.value),
            (1, Status.COMPLETED.value),
            (2, Status.COMPLETED.value),
            (3, Status.PLANNING.value),
        ):
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
                status=status,
            )
            Season.save_base(season)
            self.seasons[number] = season
        # Watched before the rewatch, without the cascade
        long_ago = timezone.now() - timedelta(days=365)
        first_watches = [
            Episode(
                related_season=self.seasons[season_number],
                item=self.episode_item(season_number, episode_number),
                end_date=long_ago,
            )
            for season_number, episode_numbers in ((0, (1,)), (1, (1, 2)), (2, (1, 2)))
            for episode_number in episode_numbers
        ]
        Episode.objects.bulk_create(first_watches)
        Episode.objects.update(created_at=long_ago)

    def episode_item(self, season_number, episode_number):
        """Return the item of an episode of the show."""
        item, _ = Item.objects.get_or_create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_number,
            episode_number=episode_number,
            defaults={"title": "Show"},
        )
        return item

    def watch(self, season_number, episode_number):
        """Watch an episode of the show now, with its cascade."""
        Episode.objects.create(
            related_season=Season.objects.get(pk=self.seasons[season_number].pk),
            item=self.episode_item(season_number, episode_number),
            end_date=timezone.now(),
        )

    def statuses(self):
        """Return the show's status and its seasons' by number."""
        self.tv.refresh_from_db()
        return self.tv.status, {
            season.item.season_number: season.status
            for season in self.tv.seasons.select_related("item")
        }

    def plan_rewatch(self):
        """Plan to rewatch the show with its card's action."""
        action = quick_actions.get_quick_action(MediaTypes.TV.value, self.tv)
        self.assertEqual(action.key, quick_actions.PLAN_REPEAT)
        quick_actions.apply_quick_action(action, self.tv)

    def test_planning_plans_watched_seasons_but_specials(self, _):
        """A planned rewatch takes in the watched seasons, not specials or new ones."""
        self.plan_rewatch()

        self.assertEqual(
            self.statuses(),
            (
                Status.PLANNING_REPEAT.value,
                {
                    0: Status.COMPLETED.value,
                    1: Status.PLANNING_REPEAT.value,
                    2: Status.PLANNING_REPEAT.value,
                    3: Status.PLANNING.value,
                },
            ),
        )
        season = Season.objects.get(pk=self.seasons[1].pk)
        self.assertIsNotNone(season.repeat_started_at)
        self.assertEqual(season.progress, 0)
        self.assertEqual(season.next_repeat_episode_number, 1)

    def test_watching_rewatches_rather_than_restarts(self, _):
        """An episode of a planned rewatch moves it on, keeping the earlier plays."""
        self.plan_rewatch()

        self.watch(1, 1)

        self.assertEqual(
            self.statuses(),
            (
                Status.REPEATING.value,
                {
                    0: Status.COMPLETED.value,
                    1: Status.REPEATING.value,
                    2: Status.PLANNING_REPEAT.value,
                    3: Status.PLANNING.value,
                },
            ),
        )
        season = Season.objects.get(pk=self.seasons[1].pk)
        self.assertEqual(season.progress, 1)
        self.assertEqual(season.next_repeat_episode_number, 2)
        self.assertEqual(season.episodes.count(), 3)
        action = quick_actions.get_quick_action(MediaTypes.SEASON.value, season)
        self.assertEqual(action.label, "Mark S1E2 watched")
        self.assertEqual(action.target_status, Status.REPEATING.value)

    def test_finishing_seasons_moves_on_then_finishes_the_show(self, _):
        """Each rewatched season starts the next, and the last finishes the show."""
        self.plan_rewatch()

        self.watch(1, 1)
        self.watch(1, 2)
        self.assertEqual(
            self.statuses(),
            (
                Status.REPEATING.value,
                {
                    0: Status.COMPLETED.value,
                    1: Status.COMPLETED.value,
                    2: Status.REPEATING.value,
                    3: Status.PLANNING.value,
                },
            ),
        )

        self.watch(2, 1)
        # Each counts the plays every watched episode has had
        self.assertEqual(Season.objects.get(pk=self.seasons[2].pk).plays, 1)
        self.assertEqual(TV.objects.get(pk=self.tv.pk).plays, 1)
        self.watch(2, 2)
        # Finished again, though season 3 is still to be watched for the first time
        self.assertEqual(
            self.statuses(),
            (
                Status.COMPLETED.value,
                {
                    0: Status.COMPLETED.value,
                    1: Status.COMPLETED.value,
                    2: Status.COMPLETED.value,
                    3: Status.PLANNING.value,
                },
            ),
        )
        # Season 3, not watched yet, doesn't count
        self.assertEqual(TV.objects.get(pk=self.tv.pk).plays, 2)

    def test_leaving_a_rewatch_cancels_it(self, _):
        """Another status than finished cancels the rewatch, adding no watches."""
        self.plan_rewatch()
        self.watch(1, 1)

        self.tv = TV.objects.get(pk=self.tv.pk)
        self.tv.status = Status.DROPPED.value
        self.tv.save()

        self.assertEqual(
            self.statuses(),
            (
                Status.DROPPED.value,
                {
                    0: Status.COMPLETED.value,
                    1: Status.COMPLETED.value,
                    2: Status.COMPLETED.value,
                    3: Status.PLANNING.value,
                },
            ),
        )
        self.assertEqual(self.seasons[1].episodes.count(), 3)
        self.assertEqual(self.seasons[2].episodes.count(), 2)

    def test_unwatched_season_planned_again_offers_nothing(self, _):
        """A season planned to be rewatched with nothing watched has no next episode."""
        season = Season.objects.get(pk=self.seasons[3].pk)
        season.status = Status.PLANNING_REPEAT.value
        season.save()

        season = Season.objects.get(pk=season.pk)
        self.assertIsNone(
            quick_actions.get_quick_action(MediaTypes.SEASON.value, season),
        )

    def test_marking_rewatched_watches_every_episode_again(self, _):
        """A rewatch finished from the form is a new play of each episode left."""
        self.plan_rewatch()
        self.watch(1, 1)

        self.tv = TV.objects.get(pk=self.tv.pk)
        self.tv.status = Status.COMPLETED.value
        self.tv.save()

        _, season_statuses = self.statuses()
        self.assertEqual(season_statuses[1], Status.COMPLETED.value)
        self.assertEqual(season_statuses[2], Status.COMPLETED.value)
        # Season 1's first episode was already watched again
        self.assertEqual(self.seasons[1].episodes.count(), 4)
        self.assertEqual(self.seasons[2].episodes.count(), 4)
        self.assertEqual(self.seasons[0].episodes.count(), 1)

    def test_planned_show_watches_its_next_episode(self, _):
        """A show planned to be rewatched starts with its first episode in one click."""
        self.plan_rewatch()
        self.tv = TV.objects.get(pk=self.tv.pk)

        action = quick_actions.get_quick_action(MediaTypes.TV.value, self.tv)
        self.assertEqual(
            (action.key, action.label, action.target_status),
            (quick_actions.NEXT, "Mark S1E1 watched", Status.REPEATING.value),
        )
        quick_actions.apply_quick_action(action, self.tv)

        self.assertEqual(self.statuses()[0], Status.REPEATING.value)
        self.tv = TV.objects.get(pk=self.tv.pk)
        action = quick_actions.get_quick_action(MediaTypes.TV.value, self.tv)
        self.assertEqual(action.label, "Mark S1E2 watched")

    def test_one_episode_again_leaves_a_finished_season(self, _):
        """Watching a finished season's episode again doesn't reopen it."""
        self.watch(1, 2)

        self.assertEqual(self.statuses()[1][1], Status.COMPLETED.value)
        self.assertEqual(self.tv.status, Status.COMPLETED.value)

    def test_season_rewatch_on_its_own(self, _):
        """A season can be rewatched alone, and finishing it finishes the show again."""
        season = Season.objects.get(pk=self.seasons[2].pk)
        action = quick_actions.get_quick_action(MediaTypes.SEASON.value, season)
        quick_actions.apply_quick_action(action, season)
        self.assertEqual(self.statuses()[0], Status.COMPLETED.value)

        season = Season.objects.get(pk=season.pk)
        season.increase_progress()
        self.assertEqual(self.statuses()[0], Status.REPEATING.value)

        season = Season.objects.get(pk=season.pk)
        season.decrease_progress()
        season = Season.objects.get(pk=season.pk)
        self.assertEqual(season.progress, 0)
        self.assertEqual(season.episodes.count(), 2)

        # Finished from the tracking form
        season.status = Status.COMPLETED.value
        season.save()
        self.assertEqual(
            self.statuses(),
            (
                Status.COMPLETED.value,
                {
                    0: Status.COMPLETED.value,
                    1: Status.COMPLETED.value,
                    2: Status.COMPLETED.value,
                    3: Status.PLANNING.value,
                },
            ),
        )
        self.assertEqual(season.episodes.count(), 4)


class CountedRewatchTests(TestCase):
    """Test rewatching media whose progress counts episodes."""

    @patch(
        "app.models.providers.services.get_media_metadata",
        return_value={"max_progress": 12},
    )
    def test_last_episode_finishes_the_rewatch(self, _):
        """Watching the last episode again finishes the rewatch as a new play."""
        user = get_user_model().objects.create_user(username="test")
        item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Anime",
        )
        anime = Anime.objects.create(
            item=item,
            user=user,
            status=Status.REPEATING.value,
            progress=11,
        )

        anime.increase_progress()

        anime.refresh_from_db()
        self.assertEqual(anime.status, Status.COMPLETED.value)
        self.assertEqual(anime.progress, 12)
        self.assertIsNotNone(anime.end_date)
