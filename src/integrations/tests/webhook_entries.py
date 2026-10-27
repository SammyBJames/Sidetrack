from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status


def friends_item(media_type, season_number=None, episode_number=None):
    """Return an item of Friends, creating it when there's none yet."""
    item, _ = Item.objects.get_or_create(
        media_id="1668",
        source=Sources.TMDB.value,
        media_type=media_type,
        season_number=season_number,
        episode_number=episode_number,
        defaults={"title": "Friends", "image": "http://example.com/image.jpg"},
    )
    return item


def plan_friends_episode(user, status=Status.PLANNING.value):
    """Track Friends' first season in progress with an entry of its first episode.

    The entry is in `status`, with notes, and is returned.
    """
    tv = TV(
        item=friends_item(MediaTypes.TV.value),
        user=user,
        status=Status.IN_PROGRESS.value,
    )
    TV.save_base(tv)
    season = Season(
        item=friends_item(MediaTypes.SEASON.value, 1),
        user=user,
        related_tv=tv,
        status=Status.IN_PROGRESS.value,
    )
    Season.save_base(season)
    return Episode.objects.bulk_create(
        [
            Episode(
                item=friends_item(MediaTypes.EPISODE.value, 1, 1),
                related_season=season,
                status=status,
                notes="Pilot",
            ),
        ],
    )[0]
