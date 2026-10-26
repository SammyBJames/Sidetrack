"""The one-click action shown on media cards."""

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from app import config
from app.models import BasicMedia, MediaTypes, Status

PLAN = "plan"
START = "start"
RESUME = "resume"
COMPLETE = "complete"
NEXT = "next"

# Media that is usually finished in one sitting, so planning leads to completing
SINGLE_SITTING_TYPES = {MediaTypes.MOVIE.value}

# Media whose progress counts episodes, chapters or issues one at a time
COUNTED_TYPES = {
    MediaTypes.SEASON.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.COMIC.value,
}

# Media whose start and end dates come from its watched episodes
DERIVED_DATE_TYPES = {MediaTypes.TV.value, MediaTypes.SEASON.value}


@dataclass(frozen=True)
class QuickAction:
    """An action a card can take without opening the tracking form."""

    key: str
    label: str
    icon: str
    target_status: str


# Label, icon and resulting status of each action
ACTIONS = {
    PLAN: ("Plan to {verb}", "app/icons/states/planning.svg", Status.PLANNING.value),
    START: (
        "Start {verb}ing",
        "app/icons/states/in-progress.svg",
        Status.IN_PROGRESS.value,
    ),
    RESUME: (
        "Resume {verb}ing",
        "app/icons/states/in-progress.svg",
        Status.IN_PROGRESS.value,
    ),
    COMPLETE: ("Mark {past}", "app/icons/states/completed.svg", Status.COMPLETED.value),
    NEXT: ("+1 {unit}", "app/icons/plus.svg", Status.IN_PROGRESS.value),
}


def get_quick_action(media_type, media):
    """Return the most likely next action for a media type and tracked entry.

    `media` is the user's entry, or None when they don't track it yet. Returns
    None when there is nothing sensible to offer, e.g. for episodes, shows, or
    entries that are completed or dropped.
    """
    if media_type == MediaTypes.EPISODE.value:
        return None

    status = media.status if media else None
    if status is None:
        key = PLAN
    elif status == Status.PLANNING.value:
        key = COMPLETE if media_type in SINGLE_SITTING_TYPES else START
    elif status == Status.IN_PROGRESS.value:
        if media_type in COUNTED_TYPES:
            # Nothing to add once everything released so far is done
            max_progress = getattr(media, "max_progress", None)
            caught_up = max_progress and media.progress >= max_progress
            key = None if caught_up else NEXT
        elif media_type == MediaTypes.TV.value:
            # A show's progress comes from its seasons, so act on a season instead
            key = None
        else:
            key = COMPLETE
    elif status == Status.PAUSED.value:
        key = RESUME
    else:
        key = None

    if key is None:
        return None

    label, icon = _label_and_icon(key, media_type, media)
    return QuickAction(key, label, icon, ACTIONS[key][2])


def _label_and_icon(key, media_type, media):
    """Return the label and icon of an action on a media type and entry."""
    label, icon, _ = ACTIONS[key]
    if key == PLAN and _watched(media_type):
        label = "Watchlist"
    elif key == NEXT and _watched(media_type):
        # Watching names the episode, e.g. Mark S1E4 watched
        label = f"Mark {_next_episode_code(media_type, media)} {{past}}"
        icon = "app/icons/states/in-progress.svg"
    label = label.format(
        verb=config.get_verb(media_type, past_tense=False),
        past=config.get_verb(media_type, past_tense=True),
        # Only counted media has a unit
        unit=config.get_unit(media_type, short=False).lower() if key == NEXT else "",
    )
    return label, icon


def apply_quick_action(action, media):
    """Apply a quick action to a tracked entry, or to an unsaved one to create it.

    Saves `media`. A start or end date is filled in only when it is empty, and
    not at all for media whose dates come from its episodes. Planning a show
    plans its seasons too.
    """
    if action.key == NEXT:
        media.increase_progress()
        return

    media.status = action.target_status
    date_field = {START: "start_date", RESUME: "start_date", COMPLETE: "end_date"}.get(
        action.key,
    )
    if (
        date_field
        and media.item.media_type not in DERIVED_DATE_TYPES
        and getattr(media, date_field) is None
    ):
        setattr(media, date_field, _now())
    # A show on the watchlist brings its seasons with it, or isn't saved at all
    # when they can't be planned, so trying again plans them
    with transaction.atomic():
        media.save()
        if action.key == PLAN and media.item.media_type == MediaTypes.TV.value:
            media.plan_seasons()


def success_message(action, media):
    """Return the confirmation message for a quick action applied to `media`."""
    verb = config.get_verb(media.item.media_type, past_tense=False)
    past = config.get_verb(media.item.media_type, past_tense=True)
    messages = {
        PLAN: (
            f"{media.item} added to your watchlist."
            if _watched(media.item.media_type)
            else f"{media.item} added to Planning."
        ),
        START: f"Started {verb}ing {media.item}.",
        RESUME: f"Resumed {verb}ing {media.item}.",
        COMPLETE: f"{media.item} marked as {past}.",
        NEXT: f"{media.item} progress updated.",
    }
    return messages[action.key]


def _next_episode_code(media_type, media):
    """Return the episode after a season or anime's progress, e.g. S1E4 or E4.

    Episodes are taken to be numbered one after another from 1.
    """
    episode = f"E{media.progress + 1}"
    if media_type == MediaTypes.SEASON.value:
        return f"S{media.item.season_number}{episode}"
    return episode


def get_show_next_season(tv):
    """Return the season of a show in progress whose next episode is out, or None.

    That's the first season in progress with released episodes left, by number
    with specials last. Its `max_progress` is annotated for its quick action.
    """
    seasons = list(
        tv.seasons.filter(status=Status.IN_PROGRESS.value)
        .select_related("item")
        .prefetch_related("episodes__item"),
    )
    BasicMedia.objects.annotate_max_progress(seasons, MediaTypes.SEASON.value)
    seasons.sort(
        key=lambda season: (season.item.season_number == 0, season.item.season_number),
    )
    for season in seasons:
        if get_quick_action(MediaTypes.SEASON.value, season):
            return season
    return None


def _watched(media_type):
    """Return whether a media type is watched, so planning it means a watchlist."""
    return config.get_verb(media_type, past_tense=False) == "watch"


def _now():
    """Return the current time the way the tracking form fills it in."""
    now = timezone.localtime().replace(second=0, microsecond=0)
    if settings.TRACK_TIME:
        return now
    return now.replace(hour=0, minute=0)
