from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

BEFORE = [("app", "0070_episode_status_start_date")]
AFTER = [("app", "0071_repeat_entries")]


class RepeatEntriesMigrationTests(TransactionTestCase):
    """Data migration 0071 turns seasons' rewatch state into entries, and back."""

    def setUp(self):
        """Migrate to before the data migration."""
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(BEFORE)
        self.executor.loader.build_graph()

    def tearDown(self):
        """Migrate back to the latest migrations."""
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate(self, targets):
        """Migrate to `targets` and return the models as they are there."""
        self.executor.migrate(targets)
        self.executor.loader.build_graph()
        return self.executor.loader.project_state(targets).apps

    def test_rewatches_become_entries_and_back_with_plays_unchanged(self):
        """Planned and started rewatches get entries for what's left of them."""
        apps = self.executor.loader.project_state(BEFORE).apps
        Item = apps.get_model("app", "Item")
        TV = apps.get_model("app", "TV")
        Season = apps.get_model("app", "Season")
        Episode = apps.get_model("app", "Episode")
        user_id = get_user_model().objects.create_user(username="test").pk
        tv = TV.objects.create(
            item=Item.objects.create(
                media_id="1",
                source="tmdb",
                media_type="tv",
                title="Show",
                image="http://example.com/i.jpg",
            ),
            user_id=user_id,
            status="Repeating",
        )
        started = timezone.now() - timedelta(days=1)
        seasons = {}
        for number, status, planned in (
            (1, "Completed", [2]),
            (2, "Planning repeat", []),
            (3, "Repeating", []),
        ):
            season = Season.objects.create(
                item=Item.objects.create(
                    media_id="1",
                    source="tmdb",
                    media_type="season",
                    title="Show",
                    image="http://example.com/i.jpg",
                    season_number=number,
                ),
                user_id=user_id,
                related_tv=tv,
                status=status,
                planned_repeat_episodes=planned,
                repeat_started_at=started if status != "Completed" else None,
            )
            seasons[number] = season
            for episode_number in (1, 2):
                Episode.objects.create(
                    item=Item.objects.create(
                        media_id="1",
                        source="tmdb",
                        media_type="episode",
                        title="Show",
                        image="http://example.com/i.jpg",
                        season_number=number,
                        episode_number=episode_number,
                    ),
                    related_season=season,
                )
        Episode.objects.update(created_at=started - timedelta(days=1))
        # Season 3's first episode was watched again in its rewatch
        rewatch = Episode.objects.create(
            item=Item.objects.get(season_number=3, episode_number=1),
            related_season=seasons[3],
        )

        apps = self.migrate(AFTER)
        Episode = apps.get_model("app", "Episode")

        planned = sorted(
            Episode.objects.filter(status="Planning repeat").values_list(
                "related_season__item__season_number",
                "item__episode_number",
            ),
        )
        self.assertEqual(planned, [(1, 2), (2, 1), (2, 2), (3, 2)])
        self.assertEqual(Episode.objects.filter(status="Completed").count(), 7)

        apps = self.migrate(BEFORE)
        Season = apps.get_model("app", "Season")
        Episode = apps.get_model("app", "Episode")

        self.assertEqual(Episode.objects.count(), 7)
        self.assertFalse(Episode.objects.exclude(status="Completed").exists())
        seasons = {
            season.item.season_number: season
            for season in Season.objects.select_related("item")
        }
        self.assertEqual(seasons[1].planned_repeat_episodes, [2])
        self.assertIsNone(seasons[1].repeat_started_at)
        # Nothing was watched again in season 2, so its rewatch starts after
        # its watches, and season 3's with its episode watched again
        self.assertGreater(seasons[2].repeat_started_at, started)
        rewatch = Episode.objects.get(pk=rewatch.pk)
        self.assertEqual(seasons[3].repeat_started_at, rewatch.created_at)
