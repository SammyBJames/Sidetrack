"""Details of items kept on `Item`, read from provider metadata.

They let a library be sorted and filtered in the database. Each provider
formats its metadata its own way, so `details_from_metadata` reads them into
the columns of `Item` and leaves out what a provider doesn't know.
"""

import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.apps import apps
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from app import helpers
from app.models import AiringStatus, Item, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

# How old details get before they're fetched again
STALE_AFTER = timedelta(days=7)
# The longest the refresh waits out a provider's rate limit, in seconds
MAX_RATE_LIMIT_WAIT = 60

# How a provider words that a title is still coming out, ended or on its way
AIRING_STATUSES = {
    "returning series": AiringStatus.AIRING,
    "ended": AiringStatus.ENDED,
    "in production": AiringStatus.UPCOMING,
    "planned": AiringStatus.UPCOMING,
    "pilot": AiringStatus.UPCOMING,
    "airing": AiringStatus.AIRING,
    "publishing": AiringStatus.AIRING,
    "finished": AiringStatus.ENDED,
    "upcoming": AiringStatus.UPCOMING,
}

# The details key of the date and of the length of each media type
DATE_KEYS = {
    MediaTypes.MOVIE.value: "release_date",
    MediaTypes.TV.value: "first_air_date",
    MediaTypes.SEASON.value: "first_air_date",
    MediaTypes.ANIME.value: "start_date",
    MediaTypes.MANGA.value: "start_date",
    MediaTypes.GAME.value: "release_date",
    MediaTypes.BOOK.value: "publish_date",
    MediaTypes.COMIC.value: "start_date",
    MediaTypes.BOARDGAME.value: "year",
}
LENGTH_KEYS = {
    MediaTypes.TV.value: "episodes",
    MediaTypes.SEASON.value: "episodes",
    MediaTypes.ANIME.value: "episodes",
    MediaTypes.MANGA.value: "number_of_chapters",
    MediaTypes.BOOK.value: "number_of_pages",
    MediaTypes.COMIC.value: "issues_count",
}

# The largest value an integer and a small integer column hold on Postgres
MAX_INTEGER = 2**31 - 1
MAX_SMALL_INTEGER = 2**15 - 1

# The column and top score of each OMDb rating source
OMDB_COLUMNS = {
    "imdb": ("imdb_score", 10),
    "rotten_tomatoes": ("rotten_tomatoes", 100),
    "metacritic": ("metacritic", 100),
}


def parse_date(value):
    """Return a date from "YYYY", "YYYY-MM" or "YYYY-MM-DD", or None.

    A partial date is the first day of its period.
    """
    match = re.fullmatch(r"\s*(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?\s*", str(value))
    if match:
        year, month, day = (int(part or 1) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    # A negative year is BCE (e.g. BoardGameGeek's "-2200"), which a date can't hold
    if str(value).strip().startswith("-"):
        return None
    # Text like "May 2001" or "[2001]" has a year to go by
    year = re.search(r"\b(\d{4})\b", str(value))
    return date(int(year.group(1)), 1, 1) if year and int(year.group(1)) else None


def parse_minutes(value):
    """Return the minutes in a duration like "2h 5m", "5m" or "24 min", or None."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*(?:m|min))?\s*", value)
    if not match or not any(match.groups()):
        return None
    hours, minutes = (int(part or 0) for part in match.groups())
    return _count(hours * 60 + minutes)


def parse_players(value):
    """Return the fewest and most players in "2-4 players" or "2 players".

    Returns (None, None) when no count can be read. A count too large for a
    small integer column is None.
    """
    match = re.match(r"\s*(\d+)(?:\s*-\s*(\d+))?", value) if value else None
    if not match:
        return None, None
    fewest = _count(match.group(1), MAX_SMALL_INTEGER)
    return fewest, _count(match.group(2) or fewest, MAX_SMALL_INTEGER)


def _count(value, top=MAX_INTEGER):
    """Return a whole number from 1 up to `top` from a count, or None.

    The default `top` is the most an integer column holds on Postgres.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= top else None


def _score(value, top):
    """Return a score above 0 up to `top` with one decimal, or None."""
    try:
        score = Decimal(str(value).strip().rstrip("%")).quantize(Decimal("0.1"))
    except (InvalidOperation, ValueError):
        return None
    return score if 0 < score <= top else None


def _airing_status(status):
    """Return the `AiringStatus` of a provider's status text, or None."""
    text = str(status or "").lower()
    if text in AIRING_STATUSES:
        return AIRING_STATUSES[text]
    # MangaUpdates words it as e.g. "Ongoing" or "12 Volumes (Complete)"
    for word, airing_status in (
        ("ongoing", AiringStatus.AIRING),
        ("hiatus", AiringStatus.AIRING),
        ("complete", AiringStatus.ENDED),
        ("cancel", AiringStatus.ENDED),
        ("discontinued", AiringStatus.ENDED),
    ):
        if word in text:
            return airing_status
    return None


def _delimited(names):
    """Return names as "|Drama|Comedy|", or None when there are none."""
    names = [str(name).replace("|", " ").strip() for name in names or []]
    names = [name for name in names if name]
    return f"|{'|'.join(names)}|" if names else None


def details_from_metadata(media_type, source, metadata, ratings=None):
    """Return the `Item` columns a provider's metadata gives values for.

    Columns the provider has no value for are left out, so a caller updates only
    what it knows. `ratings` is the list `helpers.get_media_ratings` returns;
    its IMDb, Rotten Tomatoes and Metacritic scores are added when given.
    """
    details = metadata.get("details") or {}
    date_key = (
        "year" if source == Sources.MANGAUPDATES.value else DATE_KEYS.get(media_type)
    )
    found = {
        "release_date": parse_date(details.get(date_key)),
        "length": _count(details.get(LENGTH_KEYS.get(media_type))),
        "provider_score": _score(metadata.get("score"), 10),
        "provider_score_count": _count(metadata.get("score_count")),
        "genres": _delimited(metadata.get("genres")),
    }

    if media_type == MediaTypes.BOARDGAME.value:
        found["runtime"] = parse_minutes(details.get("playtime"))
        found["min_players"], found["max_players"] = parse_players(
            details.get("players"),
        )
    else:
        found["runtime"] = parse_minutes(details.get("runtime"))

    if media_type == MediaTypes.TV.value:
        found["season_count"] = _count(details.get("seasons"))
    status = details.get("status")
    if source == Sources.MANGAUPDATES.value:
        status = details.get("status_in_country_of_origin")
        found["length"] = _count(metadata.get("max_progress"))
    if media_type != MediaTypes.MOVIE.value:
        found["airing_status"] = _airing_status(status)

    if media_type == MediaTypes.GAME.value:
        normally = (metadata.get("time_to_beat") or {}).get("normally")
        found["time_to_beat"] = round(normally / 60) if _count(normally) else None
        found["platforms"] = _delimited(details.get("platforms"))

    for rating in ratings or []:
        column, top = OMDB_COLUMNS.get(rating.get("source"), (None, None))
        score = _score(rating.get("score"), top) if column else None
        if score is not None:
            found[column] = score if column == "imdb_score" else int(score)

    return {column: value for column, value in found.items() if value is not None}


def save_item_details(item, metadata, ratings=None, *, touch=False):
    """Store the details of `metadata` on `item` when they changed.

    Writes only the changed columns, and `details_updated_at` when something
    changed, the item has none yet or `touch` is set. Returns whether the item
    was saved.
    """
    found = details_from_metadata(item.media_type, item.source, metadata, ratings)
    changed = [
        column for column, value in found.items() if getattr(item, column) != value
    ]
    if not (changed or touch or item.details_updated_at is None):
        return False
    for column in changed:
        setattr(item, column, found[column])
    item.details_updated_at = timezone.now()
    item.save(update_fields=[*changed, "details_updated_at"])
    return True


def save_page_details(item, media_type, source, media_id, metadata, ratings):
    """Store the details a details page already has on its item, if it has one.

    `item` is the tracked item when known. Otherwise another user's item is
    looked up, and nothing happens when there is none. A season's number is read
    from its metadata. Makes no provider calls.
    """
    if item is None:
        item = Item.objects.filter(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=metadata.get("season_number"),
            episode_number=None,
        ).first()
    if item is not None:
        save_item_details(item, metadata, ratings)


def refresh_item_details(item, metadata=None):
    """Fetch the details of `item` and store them, returning whether they were saved.

    Fetches the metadata unless it's given, and adds OMDb ratings for movies and
    shows. A season is read from its show's metadata. Provider errors are raised
    for the caller to handle.
    """
    if metadata is None:
        if item.media_type == MediaTypes.SEASON.value:
            metadata = services.get_media_metadata(
                "tv_with_seasons",
                item.media_id,
                item.source,
                [item.season_number],
            )[f"season/{item.season_number}"]
        else:
            metadata = services.get_media_metadata(
                item.media_type,
                item.media_id,
                item.source,
            )
    return save_item_details(
        item,
        metadata,
        helpers.get_media_ratings(metadata),
        touch=True,
    )


def _try_refresh(item):
    """Refresh an item and return `"refreshed"`, `"failed"` or `"limited"`.

    A failure is logged by type only for provider and request errors, as their
    message and cause can hold the request URL, and in it an API key, and the
    item is stamped as looked at. A provider asking for a longer wait than
    `MAX_RATE_LIMIT_WAIT` makes it limited, and the item is left unstamped.
    """
    with services.cap_rate_limit_waits(MAX_RATE_LIMIT_WAIT) as cap:
        try:
            refresh_item_details(item)
        except Exception as error:
            if isinstance(
                error,
                (services.ProviderAPIError, requests.RequestException),
            ):
                logger.warning(
                    "Failed to refresh the details of %s: %s",
                    item,
                    type(error).__name__,
                )
            else:
                logger.exception("Failed to refresh the details of %s", item)
            if cap.exceeded:
                return "limited"
            Item.objects.filter(pk=item.pk).update(details_updated_at=timezone.now())
            return "failed"
    return "refreshed"


def refresh_library_details(limit=None):
    """Refresh the details of library items that have none or are a week old.

    A library item is one with an entry of any user, and an episode never is. Items
    never fetched come first, then the oldest, up to `limit` (all when None). An
    item that fails, on a provider error or odd metadata, is stamped as looked at,
    so it waits a week instead of heading the queue every run, and the run goes on.

    A provider asking to wait longer than `MAX_RATE_LIMIT_WAIT` fails its item
    without stamping it, so it's tried again next run, and the rest of that
    provider's items are skipped for this run without using up the `limit`, so one
    rate limited provider can't take the whole batch. Returns the number of items
    refreshed and of failures.
    """
    tracked = Q()
    for media_type in MediaTypes.values:
        if media_type != MediaTypes.EPISODE.value:
            model = apps.get_model("app", media_type)
            tracked |= Exists(model.objects.filter(item=OuterRef("pk")))

    items = (
        Item.objects.filter(tracked)
        .filter(
            Q(details_updated_at__isnull=True)
            | Q(details_updated_at__lt=timezone.now() - STALE_AFTER),
        )
        .order_by(F("details_updated_at").asc(nulls_first=True), "pk")
    )

    results = {"refreshed": 0, "failed": 0, "limited": 0}
    limited_sources = set()
    remaining = limit
    # Each pass stamps the items it tries or limits their source, so the next
    # pass has new ones
    while remaining is None or remaining > 0:
        batch = items.exclude(source__in=limited_sources)
        batch = list(batch if remaining is None else batch[:remaining])
        if not batch:
            break
        for item in batch:
            if item.source in limited_sources:
                continue
            if remaining is not None:
                remaining -= 1
            result = _try_refresh(item)
            results[result] += 1
            if result == "limited":
                limited_sources.add(item.source)
    return results["refreshed"], results["failed"] + results["limited"]
