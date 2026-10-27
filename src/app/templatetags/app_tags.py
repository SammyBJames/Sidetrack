import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from django import template
from django.conf import settings
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.dateparse import parse_date
from django.utils.html import format_html
from unidecode import unidecode

from app import config, helpers, quick_actions
from app import home as home_helpers
from app.models import REPEAT_STATUSES, MediaTypes, Sources, Status, is_caught_up
from events.models import SentinelDatetime

register = template.Library()

# Statuses of media being gone through, which show their progress on cards
STARTED_STATUSES = (
    Status.IN_PROGRESS.value,
    Status.PAUSED.value,
    Status.DROPPED.value,
    Status.REPEATING.value,
)


@register.simple_tag
def get_static_file_mtime(file_path):
    """Return the last modification time of a static file for cache busting."""
    full_path = Path(settings.STATIC_ROOT) / file_path
    try:
        mtime = int(full_path.stat().st_mtime)
    except OSError:
        # If file doesn't exist or can't be accessed
        return ""
    else:
        return f"?{mtime}"


@register.simple_tag(takes_context=True)
def absolute_app_url(context, path):
    """Return an absolute app URL for links copied into external services."""
    return helpers.build_absolute_app_url(context.get("request"), path)


@register.filter
def no_underscore(arg1):
    """Return the title case of the string."""
    return arg1.replace("_", " ")


@register.filter
def related_title(name):
    """Return the heading of a related media row, e.g. Related Anime.

    Rows are named by a key like related_anime, made readable here, or by
    their source, like a movie's Spider-Man (MCU) Collection, kept as it is.
    """
    if name.islower():
        return name.replace("_", " ").title()
    return name


@register.filter
def slug(arg1):
    """Return the slug of the string.

    Sometimes slugify removes all characters from a string, so we need to
    urlencode the special characters first.
    e.g Anime: 31687
    """
    cleaned = template.defaultfilters.slugify(arg1)
    if cleaned == "":
        cleaned = template.defaultfilters.slugify(
            template.defaultfilters.urlencode(unidecode(arg1)),
        )
        if cleaned == "":
            cleaned = template.defaultfilters.urlencode(unidecode(arg1))

            if cleaned == "":
                cleaned = template.defaultfilters.urlencode(arg1)

    return cleaned


@register.filter
def date_format(datetime, user):
    """Format a datetime using user's preferred date format (date only, no time).

    Args:
        datetime: The datetime object to format
        user: User object to get preferred date format
    """
    if not datetime:
        return None
    local_dt = timezone.localtime(datetime)
    return formats.date_format(local_dt, user.date_format)


@register.filter
def iso_date_format(value, user):
    """Format an ISO date string (YYYY-MM-DD) using user's preferred date format.

    If value is not a valid ISO date string, returns the original value.
    """
    if isinstance(value, str):
        date_obj = parse_date(value)
        if date_obj:
            return formats.date_format(date_obj, user.date_format)

    return value


@register.filter
def time_format(datetime, user):
    """Format a datetime using user's preferred time format (time only, no date)."""
    if not datetime:
        return None
    local_dt = timezone.localtime(datetime)
    return formats.time_format(local_dt, user.time_format)


@register.filter
def datetime_format(datetime, user):
    """Format a datetime using user's preferred formats.

    Includes time only if TRACK_TIME setting is enabled.

    Args:
        datetime: The datetime object to format
        user: User object to get preferred date/time format
    """
    if not datetime:
        return None
    local_dt = timezone.localtime(datetime)
    formatted_date = formats.date_format(local_dt, user.date_format)

    if settings.TRACK_TIME:
        formatted_time = formats.time_format(local_dt, user.time_format)
        return f"{formatted_date} {formatted_time}"
    return formatted_date


@register.filter
def is_list(arg1):
    """Return True if the object is a list."""
    return isinstance(arg1, list)


@register.filter
def source_readable(source):
    """Return the readable source name."""
    return Sources(source).label


@register.filter
def media_type_readable(media_type):
    """Return the readable media type."""
    return MediaTypes(media_type).label


@register.filter
def media_type_readable_plural(media_type):
    """Return the readable media type in plural form."""
    singular = MediaTypes(media_type).label

    # Special cases that don't change in plural form
    if singular.lower() in [MediaTypes.ANIME.value, MediaTypes.MANGA.value]:
        return singular

    return f"{singular}s"


@register.filter
def media_status_readable(media_status, media_type=None):
    """Return the readable media status, in a media type's words if given."""
    return config.get_status_label(media_status, media_type)


@register.filter
def default_source(media_type):
    """Return the default source for the media type."""
    return config.get_default_source_name(media_type).label


@register.filter
def plays_heading(media_type):
    """Return the heading of a media type's times through, e.g. Your Reads."""
    verb = config.get_verb(media_type, past_tense=False).capitalize()
    plural = f"{verb}es" if verb.endswith(("ch", "sh")) else f"{verb}s"
    return f"Your {plural}"


@register.filter
def media_past_verb(media_type):
    """Return the past tense verb for the given media type."""
    return config.get_verb(media_type, past_tense=True)


@register.filter
def sample_search(media_type):
    """Return a sample search URL for the given media type using GET parameters."""
    return config.get_sample_search_url(media_type)


@register.filter
def short_unit(media_type):
    """Return the short unit for the media type."""
    return config.get_unit(media_type, short=True)


@register.filter
def sources(media_type):
    """Template filter to get source options for a media type."""
    return config.get_sources(media_type)


@register.simple_tag
def get_finish_action(media_type, media):
    """Return the action that marks a whole show or season watched, or None."""
    return quick_actions.get_finish_action(media_type, media)


@register.simple_tag
def get_quick_action(media_type, media):
    """Return the one-click action of a media card, or None."""
    return quick_actions.get_quick_action(media_type, media)


@register.simple_tag
def get_search_media_types(user):
    """Return available media types for search based on user preferences."""
    enabled_types = user.get_enabled_media_types()

    # Filter and format the types for search
    return [
        {
            "display": media_type_readable_plural(media_type),
            "value": media_type,
        }
        for media_type in enabled_types
        if media_type != MediaTypes.SEASON.value
    ]


@register.simple_tag
def get_sidebar_media_types(user):
    """Return available media types for sidebar navigation based on user preferences."""
    enabled_types = user.get_enabled_media_types()

    # Format the types for sidebar
    return [
        {
            "media_type": media_type,
            "display_name": media_type_readable_plural(media_type),
        }
        for media_type in enabled_types
    ]


@register.filter
def media_color(media_type):
    """Return the color associated with the media type."""
    return config.get_text_color(media_type)


@register.filter
def journal_accent(accent):
    """Return the badge background class and icon template for a journal accent."""
    return config.get_journal_accent(accent)


@register.filter
def status_config(status):
    """Return the config dict for a status, or None if it is unrecognized."""
    return config.get_status_config(status)


@register.filter
def caught_up(media):
    """Return whether media is caught up to its latest release, waiting for the next."""
    return is_caught_up(media)


@register.filter
def status_color(status):
    """Return the color associated with the status."""
    return config.get_status_text_color(status)


@register.filter
def status_icon(status):
    """Return the icon template associated with the status."""
    return config.get_status_icon(status)


@register.filter
def status_background_color(status):
    """Return the background color associated with the status."""
    return config.get_status_background_color(status)


@register.filter
def status_button_color(status):
    """Return the button background and hover classes for the status."""
    return config.get_status_button_color(status)


@register.filter
def natural_day(datetime, user):
    """Format date with natural language (Today, Tomorrow, etc.)."""
    if not datetime:
        return None

    today = timezone.localdate()

    local_dt = timezone.localtime(datetime)
    datetime_date = local_dt.date()
    formatted_date = formats.date_format(local_dt, user.date_format)
    formatted_time = formats.time_format(local_dt, user.time_format)
    days = (datetime_date - today).days

    if days == 0:
        return f"Today {formatted_time}"
    if days == 1:
        return f"Tomorrow {formatted_time}"

    return f"{formatted_date} {formatted_time}"


@register.filter
def release_day(event, user):
    """Return when a release happens in words, e.g. Tomorrow or October 6.

    Cards spell the month out on purpose, so it ignores the user's date format.
    """
    if not event:
        return ""

    local_dt = timezone.localtime(event.datetime)
    days = (local_dt.date() - timezone.localdate()).days
    day = _day_words(local_dt.date())

    # Releases without a known time are stored at a placeholder time
    if days in (0, 1) and not event.is_sentinel_time:
        return f"{day} {formats.time_format(local_dt, user.time_format)}"
    return day


def _day_words(day):
    """Return a date in words, e.g. Today, Tomorrow, October 6 or October 6, 2027."""
    today = timezone.localdate()
    days = (day - today).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Tomorrow"
    if day.year == today.year:
        return formats.date_format(day, "F j")
    return formats.date_format(day, "F j, Y")


def _local_day(value):
    """Return the day of a date or datetime, in the current time zone."""
    if isinstance(value, datetime):
        return (
            timezone.localtime(value).date()
            if timezone.is_aware(value)
            else value.date()
        )
    return value


@register.filter
def entry_dates(entry):
    """Return an entry's dates in a few words, as its tracking dialog does.

    That is Mar 1 to Mar 9 as a range (one day when both are the same), Started
    Mar 1, Finished Mar 9 or nothing, each day in the current time zone and with
    its year when that isn't this year's.
    """

    def short(value):
        """Return a date or datetime as a short day, with the year if not this one."""
        day = _local_day(value)
        pattern = "M j" if day.year == timezone.localdate().year else "M j, Y"
        return formats.date_format(day, pattern)

    start, end = entry.start_date, entry.end_date
    if start and end:
        if _local_day(start) == _local_day(end):
            return short(start)
        return f"{short(start)} \u2013 {short(end)}"
    if start:
        return f"Started {short(start)}"
    if end:
        return f"Finished {short(end)}"
    return ""


@register.filter
def air_date_label(air_date, episode_number=None):
    """Return when an episode airs, e.g. E4 · October 6, or TBA when it isn't known.

    `air_date` is an ISO date like 2027-10-06, as metadata gives it. Without an
    `episode_number` it's only the day.
    """
    try:
        day = parse_date(str(air_date or ""))
    except ValueError:
        day = None
    day = _day_words(day) if day else "TBA"
    if episode_number is None:
        return day
    return f"E{episode_number} \u00b7 {day}"


@register.filter
def unreleased(air_date):
    """Return whether something with this air date, or none, isn't out yet."""
    return not helpers.is_released_date(air_date)


@register.filter
def repeat_state(episode, season):
    """Return where a rewatch of one of a season's episodes is.

    While the season is rewatched, that's "left" while the episode has an
    entry to watch it again and "done" once it's watched. Otherwise, it's
    "planned" for a watched episode planned to be watched again on its own. It's
    "" for the rest, like episodes not watched before.
    """
    if season is None or not episode["watched"]:
        return ""
    if season.status in REPEAT_STATUSES:
        return "left" if episode["repeat_planned"] else "done"
    return "planned" if episode["repeat_planned"] else ""


@register.simple_tag
def play_count(entry, entries):
    """Return how many times media was finished, by its entry and all of them.

    Shows and seasons count their plays through their episodes, and other media
    by its finished entries.
    """
    if hasattr(entry, "plays"):
        return entry.plays
    return sum(1 for other in entries if other.status == Status.COMPLETED.value)


@register.filter
def season_release_label(event, user):
    """Return a season release's episode and day, e.g. S2E1 · Tomorrow 21:00."""
    return f"S{event.item.season_number}{release_label(event, user)}"


@register.filter
def release_label(event, user):
    """Return a release's number and day, e.g. E4 · Tomorrow 21:00."""
    day = release_day(event, user)
    if event and event.content_number:
        return f"{event.readable_content_number} \u00b7 {day}"
    return day


@register.filter
def release_year(item):
    """Return the year a card's media came out, or None when it isn't known.

    Accepts a metadata dict or an Item. Uses the item's `release_date` when it
    has one, and for movies without it their release event, reading
    `prefetched_events` when set to avoid a query per card.
    """
    if isinstance(item, dict):
        return item.get("release_year")
    # Set when re-rendering a card for an item whose release isn't fetched yet
    if getattr(item, "release_year", None):
        return item.release_year
    if item.release_date:
        return item.release_date.year
    if item.media_type != MediaTypes.MOVIE.value:
        return None

    events = getattr(item, "prefetched_events", None)
    if events is None:
        events = item.event_set.filter(content_number__isnull=True)
    for event in events:
        # Releases with an unknown date are stored at the first or last year
        if event.content_number is None and (
            SentinelDatetime.MIN_YEAR < event.datetime.year < SentinelDatetime.MAX_YEAR
        ):
            return event.datetime.year
    return None


@register.simple_tag
def card_title(item, title):
    """Return a card's title parts as a dict.

    Keys are `title`, `subtitle`, `short_subtitle` and `full`. A season titled
    with its show's name is split into the show and a "Season N" subtitle.
    """
    is_season = (
        not isinstance(item, dict)
        and item.media_type == MediaTypes.SEASON.value
        and item.season_number is not None
    )
    # Cards titled with something else, like a season's own name, keep it
    if not is_season or str(title) != str(item):
        return {"title": title, "subtitle": "", "short_subtitle": "", "full": title}

    if item.season_number == 0:
        subtitle = short_subtitle = "Specials"
    else:
        subtitle = f"Season {item.season_number}"
        short_subtitle = f"S{item.season_number}"
    return {
        "title": item.title,
        "subtitle": subtitle,
        # For cards too narrow to fit the subtitle next to the progress
        "short_subtitle": short_subtitle,
        "full": f"{item.title} \u2013 {subtitle}",
    }


def _field(obj, name):
    """Return a metadata dict's key or an object's attribute, None when missing."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _plural(count, unit):
    """Return a unit pluralized for the count, e.g. "Episodes" for 2."""
    return unit if count == 1 else f"{unit}s"


@register.simple_tag
def card_details(item, media, *, home=False, progress=True):
    """Return the details under a card's title as a dict of `part` and `repeats`.

    The part is None or a dict with a `kind`: "progress" has `text` and its long
    `unit` (empty when it has none), "plain" has `text`, and "tv" has the
    `seasons` and `episodes` counts. Items under a started status show their progress,
    unless `progress` is False for rows with a column of their own or it's
    played time on the `home` page. Otherwise a season shows its episode count
    (announced ones too, where started progress counts only released ones),
    a show its counts and anything else its release year, when known. `media`
    may be empty for media that isn't tracked. Reads only what's on the
    objects, so it costs no queries. `repeats` is how many times the media was
    repeated when more than once, else 0.
    """
    media_type = _field(item, "media_type")
    part = None

    started = (
        progress
        and getattr(media, "progress", None)
        and media.status in STARTED_STATUSES
        and media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value)
        and not (home and media_type == MediaTypes.GAME.value)
    )
    if started:
        text, unit = media.formatted_progress, ""
        # Played time has no unit
        if media_type != MediaTypes.GAME.value:
            total = _length(item, media)
            if total:
                text = f"{text} / {total}"
            unit = config.get_unit(media_type, short=False)
            unit = _plural(total or media.progress, unit)
        part = {"kind": "progress", "text": text, "unit": unit}
    # All its episodes, announced too, not only the released ones progress counts
    elif media_type == MediaTypes.SEASON.value and (
        episodes := getattr(media, "episode_count", None)
        or _field(item, "max_progress")
        or _field(item, "length")
        or getattr(media, "max_progress", None)
    ):
        text = f"{episodes} {_plural(episodes, 'Episode')}"
        part = {"kind": "plain", "text": text}
    elif media_type == MediaTypes.TV.value and (counts := _show_counts(item, media)):
        part = {"kind": "tv", "seasons": counts[0], "episodes": counts[1]}
    elif year := release_year(item):
        part = {"kind": "plain", "text": year}

    repeats = getattr(media, "repeats", 0) or 0
    return {"part": part, "repeats": repeats if repeats > 1 else 0}


def _length(item, media):
    """Return how many episodes, chapters or pages media has, None when unknown."""
    return (
        getattr(media, "max_progress", None)
        or _field(item, "max_progress")
        or _field(item, "length")
    )


def _show_counts(item, media):
    """Return a show's seasons and episodes counts, or None when unknown.

    A tracked show's come from its released seasons and episodes, the rest from
    the counts stored on its item.
    """
    if getattr(media, "released_seasons", None):
        return media.released_seasons, media.max_progress
    seasons = _field(item, "season_count")
    episodes = _field(item, "length")
    return (seasons, episodes) if seasons and episodes else None


@register.filter
def unreleased_on_home(media, home_status):
    """Return whether a home card is waiting on its next release."""
    return home_helpers.is_unreleased_home_media(media, home_status)


@register.filter
def media_url(media):
    """Return the media URL for both metadata and model object cases."""
    is_dict = isinstance(media, dict)

    # Get attributes using either dict access or object attribute
    media_type = media["media_type"] if is_dict else media.media_type
    source = media["source"] if is_dict else media.source
    media_id = media["media_id"] if is_dict else media.media_id
    title = media["title"] if is_dict else media.title

    if media_type in [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]:
        season_number = media["season_number"] if is_dict else media.season_number
        return reverse(
            "season_details",
            kwargs={
                "source": source,
                "media_id": media_id,
                "title": slug(title),
                "season_number": season_number,
            },
        )

    return reverse(
        "media_details",
        kwargs={
            "source": source,
            "media_type": media_type,
            "media_id": media_id,
            "title": slug(title),
        },
    )


@register.simple_tag
def media_view_url(view_name, media):
    """Return the modal URL for both metadata and model object cases."""
    is_dict = isinstance(media, dict)

    # Build kwargs using either dict access or object attribute
    kwargs = {
        "source": media["source"] if is_dict else media.source,
        "media_type": media["media_type"] if is_dict else media.media_type,
        "media_id": media["media_id"] if is_dict else media.media_id,
    }

    # Handle season/episode numbers if they exist
    if is_dict:
        if "season_number" in media:
            kwargs["season_number"] = media["season_number"]
        if "episode_number" in media:
            kwargs["episode_number"] = media["episode_number"]
    else:
        if media.season_number is not None:
            kwargs["season_number"] = media.season_number
        if media.episode_number is not None:
            kwargs["episode_number"] = media.episode_number

    return reverse(view_name, kwargs=kwargs)


@register.simple_tag
def component_id(component_type, media, instance_id=None):
    """Return the component ID for both metadata and model object cases."""
    is_dict = isinstance(media, dict)

    # Get base attributes using either dict access or object attribute
    media_type = media["media_type"] if is_dict else media.media_type
    media_id = media["media_id"] if is_dict else media.media_id

    component_id = f"{component_type}-{media_type}-{media_id}"

    # Handle season/episode numbers if they exist
    if is_dict:
        if "season_number" in media:
            component_id += f"-{media['season_number']}"
        if "episode_number" in media:
            component_id += f"-{media['episode_number']}"
    else:
        if media.season_number is not None:
            component_id += f"-{media.season_number}"
        if media.episode_number is not None:
            component_id += f"-{media.episode_number}"

    # Add instance id if provided
    if instance_id:
        component_id += f"-{instance_id}"

    return component_id


@register.simple_tag
def unicode_icon(name):
    """Return the Unicode icon for the media type."""
    return config.get_unicode_icon(name)


@register.simple_tag
def icon(name, is_active, extra_classes="w-5 h-5"):
    """Return the SVG icon for the given name."""
    base_svg = """<svg xmlns="http://www.w3.org/2000/svg"
                      width="24"
                      height="24"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      class="{active_class}{extra_classes}">
                      {content}
                 </svg>"""

    content = config.get_svg_icon(name)
    active_class = "text-indigo-400 " if is_active else ""

    svg = base_svg.format(
        content=content,
        active_class=active_class,
        extra_classes=extra_classes,
    )

    return format_html(svg)


@register.filter
def str_equals(value, arg):
    """Return True if the string value is equal to the argument."""
    return str(value) == str(arg)


@register.filter
def get_range(value):
    """Return a range from 1 to the given value."""
    return range(1, int(value) + 1)


@register.simple_tag
def get_pagination_range(current_page, total_pages, window):
    """
    Return a list of page numbers to display in pagination.

    Args:
        current_page: The current page number
        total_pages: Total number of pages
        window: Number of pages to show before and after current page

    Returns:
        A list of page numbers and None values (for ellipses)
    """
    if total_pages <= 5 + window * 2:
        # If few pages, show all
        return list(range(1, total_pages + 1))

    # Calculate left and right boundaries
    left_boundary = max(2, current_page - window)
    right_boundary = min(total_pages - 1, current_page + window)

    # Add ellipsis indicators and page numbers
    result = [1]

    second_page = 2
    # Add left ellipsis if needed
    if left_boundary > second_page:
        result.append(None)  # None represents ellipsis

    # Add pages around current page
    result.extend(range(left_boundary, right_boundary + 1))

    # Add right ellipsis if needed
    if right_boundary < total_pages - 1:
        result.append(None)  # None represents ellipsis

    # Add last page if not already included
    if total_pages not in result:
        result.append(total_pages)

    return result


@register.filter
def show_media_score(rating, user):
    """
    Return if we should show the rating of a media.

    Args:
        rating: the rating value of the media
        user: the user to check preferences for

    Returns:
        True if we should show the media score
    """
    return rating is not None and (not user.hide_zero_rating or rating > 0)


# The details holding when media came out, in order of preference
RELEASE_DETAILS = ("release_date", "first_air_date", "start_date", "publish_date")

# The details counting how long media is, with their singular and plural units
LENGTH_DETAILS = (
    ("seasons", "season", "seasons"),
    ("episodes", "episode", "episodes"),
    ("number_of_chapters", "chapter", "chapters"),
    ("number_of_pages", "page", "pages"),
)


@register.filter
def specials_last(seasons):
    """Return a show's seasons with its specials, season 0, moved to the end.

    Each season is its metadata, or a dict of it as `item` with the user's entry.
    """
    return sorted(
        seasons or [],
        key=lambda season: season.get("item", season).get("season_number") == 0,
    )


@register.simple_tag
def media_facts(media):
    """Return the short facts under a details page's title, as a list of strings.

    These are the year it came out, how many seasons, episodes, chapters or
    pages it has, and a movie's runtime, each only when known.
    """
    details = media.get("details") or {}
    facts = []

    for key in RELEASE_DETAILS:
        year = re.search(r"\b\d{4}\b", str(details.get(key) or ""))
        if year:
            facts.append(year.group())
            break

    for key, singular, plural in LENGTH_DETAILS:
        count = details.get(key)
        if isinstance(count, int) and count > 0:
            facts.append(f"{count} {singular if count == 1 else plural}")
            break

    if media.get("media_type") == MediaTypes.MOVIE.value and details.get("runtime"):
        facts.append(details["runtime"])
    return facts


@register.simple_tag
def media_details_split(media):
    """Return a details page's facts as `values` and `lists` of (key, value) pairs.

    Single values are listed apart from lists, which start with the genres, so
    each can be laid out to suit it. Unknown and empty values are left out.
    """
    values = []
    lists = [("genres", media["genres"])] if media.get("genres") else []
    for key, value in (media.get("details") or {}).items():
        if isinstance(value, list):
            if value:
                lists.append((key, value))
        elif value is not None and value != "":
            values.append((key, value))
    return {"values": values, "lists": lists}


@register.filter
def seconds_to_duration(seconds):
    """Convert seconds to human-readable duration.

    Under 30 min: rounds to nearest 5 min. 30 min and above: rounds to nearest 30 min.
    """
    if not seconds:
        return None
    total_minutes = seconds // 60
    if total_minutes < 30:  # noqa: PLR2004
        return f"{max(5, round(total_minutes / 5) * 5)}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return "30m" if minutes < 45 else "1h"  # noqa: PLR2004
    if minutes >= 45:  # noqa: PLR2004
        return f"{hours + 1}h"
    return f"{hours}h" if minutes < 15 else f"{hours}h 30m"  # noqa: PLR2004


@register.simple_tag(takes_context=True)
def add_entry_vals(context, entry, media_type):
    """Return the form request parameters, as JSON, for the Add entry button.

    `entry` is the media's latest entry. No entry or a Completed one gets the
    new-entry form, but a show or season has one entry for all its times
    through, so a Completed one opens it set to Planning rewatch. Any other
    status opens the entry as it is. `return_url` is the current page, URL
    quoted.
    """
    vals = {"return_url": quote(context["request"].get_full_path())}
    if entry is None:
        vals["is_create"] = True
    elif entry.status == Status.COMPLETED.value:
        if media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value):
            vals["instance_id"] = entry.id
            vals["status"] = Status.PLANNING_REPEAT.value
        else:
            vals["is_create"] = True
    else:
        vals["instance_id"] = entry.id
    return json.dumps(vals)
