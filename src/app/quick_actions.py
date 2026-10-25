"""The one-click action shown on media cards."""

from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from app import config
from app.models import MediaTypes, Status

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

    label, icon, target_status = ACTIONS[key]
    if key == PLAN and _watched(media_type):
        label = "Watchlist"
    label = label.format(
        verb=config.get_verb(media_type, past_tense=False),
        past=config.get_verb(media_type, past_tense=True),
        # Only counted media has a unit
        unit=config.get_unit(media_type, short=False).lower() if key == NEXT else "",
    )
    return QuickAction(key, label, icon, target_status)


def apply_quick_action(action, media):
    """Apply a quick action to a tracked entry, or to an unsaved one to create it.

    Saves `media`. A start or end date is filled in only when it is empty, and
    not at all for media whose dates come from its episodes.
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
    media.save()


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


def _watched(media_type):
    """Return whether a media type is watched, so planning it means a watchlist."""
    return config.get_verb(media_type, past_tense=False) == "watch"


def _now():
    """Return the current time the way the tracking form fills it in."""
    now = timezone.localtime().replace(second=0, microsecond=0)
    if settings.TRACK_TIME:
        return now
    return now.replace(hour=0, minute=0)
