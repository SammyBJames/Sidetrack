import logging
from datetime import datetime

import requests
from django.core.cache import cache
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

from app.models import REPEAT_STATUSES, TV, Item, MediaTypes, Season, Status
from app.providers import services, tmdb
from events.models import Event

from .helpers import date_parser, resolve_episode_datetimes

logger = logging.getLogger(__name__)

# Shows that get their new seasons planned, unlike paused and dropped ones
PLANNABLE_TV_STATUSES = (
    Status.COMPLETED.value,
    Status.PLANNING.value,
    Status.IN_PROGRESS.value,
    *REPEAT_STATUSES,
)

# Statuses of the season before a new one that plan it, as the user is caught
# up to it or means to watch it. Seasons being repeated were finished before.
NEXT_SEASON_PLANNING_STATUSES = (
    Status.COMPLETED.value,
    Status.PLANNING.value,
    *REPEAT_STATUSES,
)


def process_tv(tv_item, events_bulk):
    """Process TV item and create events for all seasons and episodes."""
    logger.info("Processing TV show: %s", tv_item)

    try:
        seasons_to_process = get_seasons_to_process(tv_item)

        if not seasons_to_process:
            logger.info("%s - No seasons need processing", tv_item)
            return

        processed_season_items = process_tv_seasons(
            tv_item,
            seasons_to_process,
            events_bulk,
        )
        plan_new_seasons(
            tv_item,
            processed_season_items,
            events_bulk,
        )

    except services.ProviderAPIError:
        logger.warning(
            "Failed to fetch metadata for %s",
            tv_item,
        )
    except Exception:
        logger.exception("Error processing %s", tv_item)


def get_seasons_to_process(tv_item):
    """Identify which seasons of a TV show need to be processed."""
    tv_metadata = tmdb.tv(tv_item.media_id)

    if not tv_metadata.get("related", {}).get("seasons"):
        logger.warning("No seasons found for TV show: %s", tv_item)
        return []

    season_numbers = [
        season["season_number"] for season in tv_metadata["related"]["seasons"]
    ]

    if not season_numbers:
        logger.warning("No valid seasons found for TV show: %s", tv_item)
        return []

    next_episode_season = tv_metadata.get("next_episode_season")

    existing_season_events = Event.objects.filter(
        item__media_id=tv_item.media_id,
        item__source=tv_item.source,
        item__media_type=MediaTypes.SEASON.value,
    ).select_related("item")

    seasons_with_events = {event.item.season_number for event in existing_season_events}
    seasons_to_process = [
        season_num
        for season_num in season_numbers
        if season_num not in seasons_with_events
        or (next_episode_season and season_num >= next_episode_season)
    ]

    if not seasons_to_process:
        return []

    logger.info(
        "%s - Processing %d seasons (Next episode season: %s)",
        tv_item,
        len(seasons_to_process),
        next_episode_season,
    )

    return seasons_to_process


def process_tv_seasons(tv_item, seasons_to_process, events_bulk):
    """Process specific seasons of a TV show."""
    process_seasons_data = tmdb.tv_with_seasons(
        tv_item.media_id,
        seasons_to_process,
    )
    processed_season_items = []

    for season_number in seasons_to_process:
        season_key = f"season/{season_number}"
        if season_key not in process_seasons_data:
            logger.warning(
                "Season %s data not found for %s",
                season_number,
                tv_item,
            )
            continue

        season_metadata = process_seasons_data[season_key]

        season_item, _ = Item.objects.get_or_create(
            media_id=tv_item.media_id,
            source=tv_item.source,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_item.title,
                "image": season_metadata["image"],
            },
        )

        processed_season_items.append(season_item)
        process_season_episodes(season_item, season_metadata, events_bulk)

    return processed_season_items


def plan_new_seasons(tv_item, season_items, events_bulk):
    """Plan a show's newly announced seasons for users caught up to them.

    A new season is planned when the season before it is planned or finished.
    A finished show is planned with it rather than started, so it waits in
    Planning until the season's first episode is watched.
    """
    eligible_season_items = [
        season_item
        for season_item in season_items
        if season_item.season_number and season_item.season_number > 0
    ]
    season_item_map = {
        season_item.season_number: season_item for season_item in eligible_season_items
    }
    if not season_item_map:
        logger.info("%s - No processed seasons eligible for planning", tv_item)
        return

    # Only future season events are new seasons; processed past-only seasons
    # can exist when local season tracking was incomplete.
    future_season_numbers = {
        event.item.season_number
        for event in events_bulk
        if event.item in eligible_season_items and event.datetime >= timezone.now()
    }
    if not future_season_numbers:
        logger.info(
            "%s - Processed seasons have no future events; skipping season planning",
            tv_item,
        )
        return

    sorted_season_numbers = sorted(future_season_numbers)
    # Paused and dropped shows are left as they are
    tracked_tvs = list(
        TV.objects.filter(item=tv_item, status__in=PLANNABLE_TV_STATUSES)
        .select_related("user")
        .prefetch_related(
            Prefetch(
                "seasons",
                queryset=Season.objects.select_related("item"),
            ),
        ),
    )
    if not tracked_tvs:
        logger.info("%s - No TV entries to plan new seasons for", tv_item)
        return

    logger.info(
        "%s - Checking %d TV entries against discovered seasons %s",
        tv_item,
        len(tracked_tvs),
        sorted_season_numbers,
    )

    plans = []
    for tv in tracked_tvs:
        new_seasons = _new_seasons_to_plan(tv, sorted_season_numbers, season_item_map)
        if new_seasons:
            plans.append((tv, new_seasons))

    if not plans:
        logger.info("%s - No TV entries needed new seasons planned", tv_item)
        return

    with transaction.atomic():
        for tv, new_seasons in plans:
            bulk_create_with_history(new_seasons, Season, default_user=tv.user)
            # A finished show waits in Planning for its new season
            if tv.status == Status.COMPLETED.value:
                tv.status = Status.PLANNING.value
                bulk_update_with_history(
                    [tv],
                    TV,
                    ["status"],
                    default_user=tv.user,
                )
            logger.info(
                "%s - Planned %d new seasons for user %s",
                tv_item,
                len(new_seasons),
                tv.user,
            )


def _new_seasons_to_plan(tv, season_numbers, season_item_map):
    """Return the new planning seasons of a show for the season numbers found.

    There are none when the user tracks them all already, or when the season
    before the first new one isn't planned or finished.
    """
    existing_seasons = {
        season.item.season_number: season
        for season in tv.seasons.all()
        if season.item.season_number and season.item.season_number > 0
    }
    missing_season_numbers = [
        season_number
        for season_number in season_numbers
        if season_number not in existing_seasons
    ]
    if not missing_season_numbers:
        logger.info(
            "%s - User %s already tracks all discovered seasons",
            tv.item,
            tv.user,
        )
        return []

    earlier = [
        number for number in existing_seasons if number < missing_season_numbers[0]
    ]
    previous_status = existing_seasons[max(earlier)].status if earlier else tv.status
    if previous_status not in NEXT_SEASON_PLANNING_STATUSES:
        logger.info(
            "%s - User %s isn't caught up to the new seasons %s",
            tv.item,
            tv.user,
            missing_season_numbers,
        )
        return []

    logger.info(
        "%s - Planning new seasons %s for user %s",
        tv.item,
        missing_season_numbers,
        tv.user,
    )
    return [
        Season(
            item=season_item_map[season_number],
            related_tv=tv,
            user=tv.user,
            status=Status.PLANNING.value,
        )
        for season_number in missing_season_numbers
    ]


def process_season_episodes(item, metadata, events_bulk):
    """Process episodes for a season and add them to events_bulk."""
    tvmaze_map = {}
    if metadata.get("tvdb_id"):
        logger.info(
            "%s - TVDB ID found, fetching TVMaze episode data",
            item,
        )
        tvmaze_map = get_tvmaze_episode_map(metadata["tvdb_id"])
    else:
        logger.warning(
            "%s - No TVDB ID found, skipping TVMaze episode data",
            item,
        )

    if not metadata.get("episodes"):
        logger.warning("%s - No episodes found in metadata", item)
        return

    season_number = metadata["season_number"]
    episode_datetimes = {
        episode["episode_number"]: get_episode_datetime(
            episode,
            season_number,
            episode["episode_number"],
            tvmaze_map,
        )
        for episode in metadata["episodes"]
    }
    resolved_datetimes = resolve_episode_datetimes(
        episode_datetimes,
        timezone.now(),
    )

    for episode_number, episode_datetime in resolved_datetimes.items():
        events_bulk.append(
            Event(
                item=item,
                content_number=episode_number,
                datetime=episode_datetime,
            ),
        )


def get_episode_datetime(episode, season_number, episode_number, tvmaze_map):
    """Return the known air datetime for an episode, or None when unknown."""
    tvmaze_key = f"{season_number}_{episode_number}"
    tvmaze_airstamp = tvmaze_map.get(tvmaze_key)

    if tvmaze_airstamp:
        return datetime.fromisoformat(tvmaze_airstamp)

    if episode["air_date"]:
        try:
            return date_parser(episode["air_date"])
        except ValueError:
            logger.warning(
                "Invalid air date for S%sE%s from TMDB: %s",
                season_number,
                episode_number,
                episode["air_date"],
            )

    return None


def get_tvmaze_episode_map(tvdb_id):
    """Fetch and process episode data from TVMaze using TVDB ID with caching."""
    cache_key = f"tvmaze_map_{tvdb_id}"
    cached_map = cache.get(cache_key)

    if cached_map:
        logger.info("%s - Using cached TVMaze episode map", tvdb_id)
        return cached_map

    show_response = get_tvmaze_response(tvdb_id)
    tvmaze_map = {}

    if show_response:
        episodes = show_response["_embedded"]["episodes"]

        for episode in episodes:
            season_num = episode.get("season")
            episode_num = episode.get("number")
            if season_num is not None and episode_num is not None:
                key = f"{season_num}_{episode_num}"
                tvmaze_map[key] = episode.get("airstamp")

    cache.set(cache_key, tvmaze_map)
    logger.info(
        "%s - Cached TVMaze episode map with %d entries",
        tvdb_id,
        len(tvmaze_map),
    )

    return tvmaze_map


def get_tvmaze_response(tvdb_id):
    """Fetch episode data from TVMaze using TVDB ID."""
    lookup_url = f"https://api.tvmaze.com/lookup/shows?thetvdb={tvdb_id}"
    try:
        lookup_response = services.api_request("TVMaze", "GET", lookup_url)
    except requests.exceptions.HTTPError as err:
        if err.response.status_code == requests.codes.not_found:
            logger.warning(
                "TVMaze lookup failed for TVDB ID %s - %s",
                tvdb_id,
                err.response.text,
            )
        else:
            logger.warning(
                "%s - TVMaze lookup error: %s",
                tvdb_id,
                err.response.text,
            )
        lookup_response = {}

    if not lookup_response:
        logger.warning("%s - No TVMaze lookup response for TVDB ID", tvdb_id)
        return {}

    tvmaze_id = lookup_response.get("id")

    if not tvmaze_id:
        logger.warning("%s - TVMaze ID not found for TVDB ID", tvdb_id)
        return {}

    show_url = f"https://api.tvmaze.com/shows/{tvmaze_id}?embed=episodes"

    try:
        return services.api_request("TVMaze", "GET", show_url)
    except requests.exceptions.HTTPError:
        return {}
