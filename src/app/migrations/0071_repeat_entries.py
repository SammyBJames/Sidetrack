from django.db import migrations
from django.db.models import Max
from django.utils import timezone

COMPLETED = "Completed"
PLANNING_REPEAT = "Planning repeat"
REPEAT_STATUSES = (PLANNING_REPEAT, "Repeating")


def forwards(apps, schema_editor):
    """Turn the seasons' rewatch state into entries to watch episodes again.

    An episode planned to be watched again on its own gets one, and so does
    each watched episode of a season being rewatched that wasn't watched again
    since the rewatch was planned. Watches aren't changed, so plays aren't.
    """
    Season = apps.get_model("app", "Season")
    Episode = apps.get_model("app", "Episode")
    seasons = Season.objects.exclude(
        planned_repeat_episodes=[],
    ) | Season.objects.filter(status__in=REPEAT_STATUSES)
    entries = []
    for season in seasons.distinct():
        watches = Episode.objects.filter(related_season=season, status=COMPLETED)
        planned_items = set(
            Episode.objects.filter(
                related_season=season,
                status__in=REPEAT_STATUSES,
            ).values_list("item_id", flat=True),
        )
        items = {}
        if season.status in REPEAT_STATUSES:
            rewatched = set()
            if season.repeat_started_at is not None:
                rewatched = set(
                    watches.filter(
                        created_at__gte=season.repeat_started_at,
                    ).values_list("item_id", flat=True),
                )
            for item_id, number in watches.values_list(
                "item_id",
                "item__episode_number",
            ):
                if item_id not in rewatched:
                    items[item_id] = number
        for item_id, number in watches.filter(
            item__episode_number__in=season.planned_repeat_episodes,
        ).values_list("item_id", "item__episode_number"):
            items[item_id] = number
        entries.extend(
            Episode(
                related_season=season,
                item_id=item_id,
                status=PLANNING_REPEAT,
            )
            for item_id, _number in sorted(items.items(), key=lambda pair: pair[1])
            if item_id not in planned_items
        )
    Episode.objects.bulk_create(entries, batch_size=500)


def backwards(apps, schema_editor):
    """Rebuild the seasons' rewatch state from the entries to watch episodes again.

    A season being rewatched starts its rewatch at the earliest of the latest
    watches of the episodes it watched again, or when it was planned if it
    watched none again yet. Other seasons plan the episodes with an entry to
    watch them again on their own. The entries are then deleted, so plays
    aren't changed.
    """
    Season = apps.get_model("app", "Season")
    Episode = apps.get_model("app", "Episode")
    entries = Episode.objects.filter(status__in=REPEAT_STATUSES)
    for season in Season.objects.filter(
        pk__in=entries.values("related_season_id"),
    ) | Season.objects.filter(status__in=REPEAT_STATUSES):
        planned = entries.filter(related_season=season)
        if season.status not in REPEAT_STATUSES:
            season.planned_repeat_episodes = sorted(
                set(planned.values_list("item__episode_number", flat=True)),
            )
            season.save(update_fields=["planned_repeat_episodes"])
            continue
        latest_watches = (
            Episode.objects.filter(related_season=season, status=COMPLETED)
            .exclude(item_id__in=planned.values("item_id"))
            .values("item_id")
            .annotate(latest=Max("created_at"))
            .values_list("latest", flat=True)
        )
        season.repeat_started_at = (
            min(latest_watches, default=None)
            or min(planned.values_list("created_at", flat=True), default=None)
            or timezone.now()
        )
        season.save(update_fields=["repeat_started_at"])
    entries.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0070_episode_status_start_date"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
