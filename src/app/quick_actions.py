"""The one-click action shown on media cards."""

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from app import config, helpers
from app.models import (
    REPEAT_STATUSES,
    BasicMedia,
    MediaTypes,
    Status,
    is_caught_up,
)
from app.providers import services
from events.models import Event

PLAN = "plan"
START = "start"
RESUME = "resume"
COMPLETE = "complete"
NEXT = "next"
PLAN_REPEAT = "plan_repeat"
REPEAT = "repeat"
LOG = "log"
START_REPEAT = "start_repeat"
# An episode's own action: watching it now, whether for the first time or again
WATCH = "watch"

# Media that is usually finished in one sitting, so planning leads to completing
SINGLE_SITTING_TYPES = {MediaTypes.MOVIE.value}

# Media whose in progress action logs where they are up to, with what that is
LOGGED_TYPES = {MediaTypes.BOOK.value: "pages", MediaTypes.GAME.value: "time"}

# Media whose progress counts episodes, chapters or issues one at a time
COUNTED_TYPES = {
    MediaTypes.SEASON.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.COMIC.value,
}

# Statuses of shows and seasons whose action watches their next episode
EPISODE_STATUSES = (
    Status.PLANNING.value,
    Status.IN_PROGRESS.value,
    *REPEAT_STATUSES,
)

# Media whose start and end dates come from its watched episodes
DERIVED_DATE_TYPES = {MediaTypes.TV.value, MediaTypes.SEASON.value}

# Shows and seasons: one entry per user, which repeats through its episodes
# rather than through new entries
EPISODIC_TYPES = DERIVED_DATE_TYPES

# Statuses an entry waits on its next release in, shown in their colours
WAITING_STATUSES = (
    Status.PLANNING.value,
    Status.IN_PROGRESS.value,
    *REPEAT_STATUSES,
)


@dataclass(frozen=True)
class ButtonState:
    """What a button shows when it has no action: a release, or a status.

    `status` is the status shown, or for a release, the one it waits in, whose
    colours it takes. A release has the `event` it waits for, or none when only
    its air date is known.
    """

    status: str | None
    release: bool = False
    event: Event | None = None


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
    PLAN_REPEAT: (
        "Plan to re{verb}",
        "app/icons/states/planning-repeat.svg",
        Status.PLANNING_REPEAT.value,
    ),
    REPEAT: (
        "Mark re{past}",
        "app/icons/states/completed.svg",
        Status.COMPLETED.value,
    ),
    # Starts the repeat of a book or game, which then logs its progress
    START_REPEAT: (
        "Start re{verb}ing",
        "app/icons/states/repeating.svg",
        Status.REPEATING.value,
    ),
    WATCH: ("Mark {past}", "app/icons/states/completed.svg", Status.COMPLETED.value),
    # Not one click: it asks for the page or time, and keeps the status
    LOG: ("Log {logged}", "app/icons/edit.svg", Status.IN_PROGRESS.value),
}


def get_quick_action(media_type, media, episode=None):
    """Return the most likely next action for a media type and tracked entry.

    `media` is the user's entry, or None when they don't track it yet. Returns
    None when there is nothing sensible to offer, e.g. for caught up entries, or
    entries that are dropped. Finished media is planned to be repeated, which
    then marks a new play. Shows and seasons, planned or not, watch their next
    episode. Books and games in progress or repeating get the log action, which
    asks for a page or time and keeps the status.

    An episode is acted on through its season entry as `media` and its state as
    `episode`, as a provider's processed episode gives it. Without that state
    there is no action.
    """
    if media_type == MediaTypes.EPISODE.value:
        return None if episode is None else _get_episode_action(media, episode)

    status = media.status if media else None
    if media_type == MediaTypes.TV.value and status in EPISODE_STATUSES:
        # A show's progress comes from its seasons, so it acts on the next one
        season = get_show_next_season(media)
        if season:
            return get_quick_action(MediaTypes.SEASON.value, season)
        if status == Status.PLANNING.value and not media.seasons.all():
            # Planned before its seasons were, which are planned when it starts
            past = config.get_verb(media_type, past_tense=True)
            return QuickAction(
                NEXT,
                f"Mark S1E1 {past}",
                config.get_status_icon(Status.IN_PROGRESS.value),
                Status.IN_PROGRESS.value,
            )
        if status == Status.PLANNING.value:
            # Its planned seasons are still to air
            return None

    key = None if media and _waiting(media) else _action_key(media_type, media)
    if key is None:
        return None

    label, icon = _label_and_icon(key, media_type, media)
    target_status = ACTIONS[key][2]
    if key in (NEXT, LOG) and status in REPEAT_STATUSES:
        target_status = Status.REPEATING.value
    return QuickAction(key, label, icon, target_status)


def get_button_state(media, episode=None):
    """Return what a button shows when it has no action, or None for nothing.

    A known next release comes first: the next event of an entry planned, in
    progress or repeated, or an episode's air date still to come. It waits in
    the entry's status, which is planning once it has all that is out, and
    takes that status's colours. Otherwise it is the entry's status, if it has
    one. An episode is given as `get_quick_action` takes it.
    """
    if episode is not None:
        air_date = episode["air_date"]
        if air_date and not helpers.is_released_date(air_date):
            return ButtonState(Status.PLANNING.value, release=True)
        status = episode_status(media, episode)
        return ButtonState(status) if status else None

    status = media.status if media else None
    event = getattr(media, "next_event", None)
    if event is None or event.is_max_datetime or status not in WAITING_STATUSES:
        return ButtonState(status) if status else None
    if is_caught_up(media):
        status = Status.PLANNING.value
    return ButtonState(status, release=True, event=event)


def episode_status(season, episode):
    """Return the status an episode of a season entry is in, or None untracked.

    Watched, it is completed, or planned again with a rewatch planned; else it
    is planned along with its tracked season.
    """
    if episode["watched"]:
        return (
            Status.PLANNING_REPEAT.value
            if episode["repeat_planned"]
            else Status.COMPLETED.value
        )
    return None if season is None else Status.PLANNING.value


def get_finish_action(media_type, media):
    """Return the action that marks a whole show, season, book or game done, or None.

    It's offered beside watching the next episode or logging progress, so for
    shows and seasons planned, in progress or repeated, and for books and games
    in progress or repeated, which it finishes as a new play.
    """
    if media is None:
        return None
    if media_type in LOGGED_TYPES:
        statuses = (Status.IN_PROGRESS.value, *REPEAT_STATUSES)
    elif media_type in EPISODIC_TYPES:
        statuses = EPISODE_STATUSES
    else:
        return None
    if media.status not in statuses:
        return None

    key = REPEAT if media.status in REPEAT_STATUSES else COMPLETE
    noun = {MediaTypes.TV.value: " show", MediaTypes.SEASON.value: " season"}.get(
        media_type,
        "",
    )
    again = "re" if key == REPEAT else ""
    past = config.get_verb(media_type, past_tense=True)
    return QuickAction(
        key,
        f"Mark{noun} {again}{past}",
        "app/icons/states/completed.svg",
        Status.COMPLETED.value,
    )


def _get_episode_action(season, episode):
    """Return the action of an episode of a season entry, or None.

    A watched one is planned to be watched again, which then watches it again, as
    do those left to watch in a season's rewatch. Once watched in that rewatch,
    or while it's still to air, there is nothing to do. `season` is None when the
    user doesn't track the season.
    """
    watched = season is not None and episode["watched"]
    if watched and not episode["repeat_planned"]:
        # Done in its season's rewatch, else watched and offered again
        if season.status in REPEAT_STATUSES:
            return None
        return _episode_action(PLAN_REPEAT)
    if episode["air_date"] and not helpers.is_released_date(episode["air_date"]):
        return None
    return _episode_action(WATCH, again=watched)


def _episode_action(key, *, again=False):
    """Return an episode's action, with a label for watching it again if `again`."""
    label, icon, target_status = ACTIONS[key]
    verb = config.get_verb(MediaTypes.EPISODE.value, past_tense=False)
    past = config.get_verb(MediaTypes.EPISODE.value, past_tense=True)
    return QuickAction(
        key,
        label.format(verb=verb, past=f"re{past}" if again else past),
        icon,
        target_status,
    )


def _action_key(media_type, media):
    """Return the key of the action for a media type and tracked entry, or None."""
    status = media.status if media else None
    if status is None:
        return PLAN
    if media_type == MediaTypes.SEASON.value and status in EPISODE_STATUSES:
        return _progress_action_key(media_type, media)
    if status in (Status.IN_PROGRESS.value, Status.REPEATING.value):
        return _progress_action_key(media_type, media)
    if status == Status.PLANNING.value:
        return COMPLETE if media_type in SINGLE_SITTING_TYPES else START
    return {
        Status.COMPLETED.value: PLAN_REPEAT,
        # Books and games are read or played again in sittings, which are logged
        Status.PLANNING_REPEAT.value: (
            START_REPEAT if media_type in LOGGED_TYPES else REPEAT
        ),
        Status.PAUSED.value: RESUME,
    }.get(status)


def _progress_action_key(media_type, media):
    """Return the action key for an entry in progress or being repeated, or None."""
    if media_type == MediaTypes.SEASON.value and media.status in REPEAT_STATUSES:
        # A rewatch goes through the episodes watched before, if there are any
        return None if media.next_repeat_episode_number is None else NEXT
    if media_type in COUNTED_TYPES:
        # Nothing to add once everything released so far is done
        max_progress = getattr(media, "max_progress", None)
        caught_up = max_progress and media.progress >= max_progress
        return None if caught_up else NEXT
    if media_type == MediaTypes.TV.value:
        # A show's progress comes from its seasons, so act on a season instead
        return None
    if media_type in LOGGED_TYPES:
        return LOG
    return REPEAT if media.status == Status.REPEATING.value else COMPLETE


def _label_and_icon(key, media_type, media):
    """Return the label and icon of an action on a media type and entry."""
    label, icon, _ = ACTIONS[key]
    if key == PLAN and _watched(media_type):
        label = "Watchlist"
    elif key == NEXT and _watched(media_type):
        # Watching names the episode, e.g. Mark S1E4 watched
        label = f"Mark {_next_episode_code(media_type, media)} {{past}}"
        icon = config.get_status_icon(
            Status.REPEATING.value
            if media.status in REPEAT_STATUSES
            else Status.IN_PROGRESS.value,
        )
    label = label.format(
        verb=config.get_verb(media_type, past_tense=False),
        past=config.get_verb(media_type, past_tense=True),
        # Only counted media has a unit
        unit=config.get_unit(media_type, short=False).lower() if key == NEXT else "",
        logged=LOGGED_TYPES.get(media_type, ""),
    )
    return label, icon


def apply_quick_action(action, media, episode_number=None):
    """Apply a quick action to a tracked entry, or to an unsaved one to create it.

    For an episode's action, `media` is its season and `episode_number` the
    episode: watching it now, or planning to watch it again. A season not saved
    yet is, with the episode, started.

    Saves `media` and returns it, or the new entry that planning a repeat
    starts, which keeps the finished one as an earlier play. A start or end date
    is filled in only when it is empty, and not at all for media whose dates come
    from its episodes. Planning a show plans its seasons too, and planning a
    season its episodes.
    """
    if episode_number is not None:
        _apply_episode_action(action, media, episode_number)
        return media

    if action.key == NEXT:
        if media.item.media_type == MediaTypes.TV.value:
            _watch_show_next_episode(media)
        else:
            media.increase_progress()
        return media

    if action.key == PLAN_REPEAT and media.item.media_type not in EPISODIC_TYPES:
        media = type(media)(item=media.item, user=media.user)

    media.status = action.target_status
    date_field = {
        START: "start_date",
        START_REPEAT: "start_date",
        RESUME: "start_date",
        COMPLETE: "end_date",
        REPEAT: "end_date",
    }.get(action.key)
    if (
        date_field
        and media.item.media_type not in DERIVED_DATE_TYPES
        and getattr(media, date_field) is None
    ):
        setattr(media, date_field, _now())
    # A show on the watchlist brings its seasons with it, and a season its
    # episodes, or isn't saved at all when they can't be planned, so trying
    # again plans them
    with transaction.atomic():
        media.save()
        if action.key == PLAN and media.item.media_type == MediaTypes.TV.value:
            media.plan_seasons()
        elif action.key == PLAN and media.item.media_type == MediaTypes.SEASON.value:
            media.plan_episodes()
    return media


def _apply_episode_action(action, season, episode_number):
    """Watch an episode of a season now, or plan to watch it again.

    Saves a season not tracked yet as in progress.
    """
    with transaction.atomic():
        if season.pk is None:
            season.status = Status.IN_PROGRESS.value
            season.save()
        if action.key == WATCH:
            season.watch(
                episode_number,
                timezone.now().replace(second=0, microsecond=0),
            )
        else:
            season.plan_repeat_entries(episode_number)


def book_total(book):
    """Return a book's page count, or None when it isn't known.

    The item's own length goes first, else the provider's, which is fetched.
    """
    if book.item.length:
        return book.item.length
    return helpers.positive_int_or_none(
        services.get_media_metadata(
            book.item.media_type,
            book.item.media_id,
            book.item.source,
        )["max_progress"],
    )


def log_progress(media, amount):
    """Log a book's page or a game session's minutes on an in progress entry.

    `amount` is the absolute page for a book, which finishes it from its last
    page on, and the minutes to add for a game. Saves `media`, which must be in
    progress or repeating, and returns the confirmation message.
    """
    if media.item.media_type == MediaTypes.GAME.value:
        media.progress += amount
        media.save()
        return f"Logged {helpers.minutes_to_hhmm(amount)} of {media.item}."

    total = book_total(media)
    if total and amount >= total:
        finish = get_finish_action(media.item.media_type, media)
        media.progress = total
        apply_quick_action(finish, media)
        return success_message(finish, media)
    media.progress = amount
    media.save()
    return f"{media.item} is now on page {amount}."


def _watch_show_next_episode(tv):
    """Watch the next episode of a show, planning its seasons if it has none.

    A show with no seasons to plan is started instead.
    """
    with transaction.atomic():
        season = get_show_next_season(tv)
        if season is None:
            tv.plan_seasons()
            # Drops any seasons prefetched before they were planned
            tv.refresh_from_db()
            season = get_show_next_season(tv)
        if season is None:
            tv.status = Status.IN_PROGRESS.value
            tv.save()
            return
        season.increase_progress()


def success_message(action, media, episode_number=None):
    """Return the confirmation message for a quick action applied to `media`.

    An episode's action names the episode of the season `media`.
    """
    verb = config.get_verb(media.item.media_type, past_tense=False)
    past = config.get_verb(media.item.media_type, past_tense=True)
    if episode_number is not None:
        subject = f"{media.item} E{episode_number}"
        return {
            WATCH: f"{subject} marked as {past}.",
            PLAN_REPEAT: f"Planning to re{verb} {subject}.",
        }[action.key]
    messages = {
        PLAN: (
            f"{media.item} added to your watchlist."
            if _watched(media.item.media_type)
            else f"{media.item} added to Planning."
        ),
        START: f"Started {verb}ing {media.item}.",
        START_REPEAT: f"Started re{verb}ing {media.item}.",
        RESUME: f"Resumed {verb}ing {media.item}.",
        COMPLETE: f"{media.item} marked as {past}.",
        NEXT: f"{media.item} progress updated.",
        PLAN_REPEAT: f"Planning to re{verb} {media.item}.",
        REPEAT: f"{media.item} marked as re{past}.",
    }
    return messages[action.key]


def _next_episode_code(media_type, media):
    """Return the episode after a season or anime's progress, e.g. S1E4 or E4.

    Episodes are taken to be numbered one after another from 1, except in a
    season's rewatch, which knows its next episode.
    """
    if media_type == MediaTypes.SEASON.value and media.status in REPEAT_STATUSES:
        number = media.next_repeat_episode_number
    else:
        number = media.progress + 1
    episode = f"E{number}"
    if media_type == MediaTypes.SEASON.value:
        return f"S{media.item.season_number}{episode}"
    return episode


def get_show_next_season(tv):
    """Return the season of a show whose next episode is out, or None.

    That's the first season started, else the first planned, with released
    episodes left, by number with specials last. While the show is rewatched,
    only seasons being rewatched count. Its `max_progress` is annotated for its
    quick action.
    """
    if tv.status in REPEAT_STATUSES:
        started, planned = Status.REPEATING.value, Status.PLANNING_REPEAT.value
    else:
        started, planned = Status.IN_PROGRESS.value, Status.PLANNING.value
    # From the show's prefetched seasons when it has them, as in lists
    seasons = [
        season for season in tv.seasons.all() if season.status in (started, planned)
    ]
    BasicMedia.objects.annotate_max_progress(seasons, MediaTypes.SEASON.value)
    seasons.sort(
        key=lambda season: (
            season.status != started,
            season.item.season_number == 0,
            season.item.season_number,
        ),
    )
    for season in seasons:
        if get_quick_action(MediaTypes.SEASON.value, season):
            return season
    return None


def _waiting(media):
    """Return whether an entry waits on its releases, so there is nothing to act on.

    That's when it is caught up on everything out, or is planned with every known
    release still to come. Seasons look their releases up when they weren't
    loaded, as in a list; other media goes by those loaded, so a card costs no
    query.
    """
    if media.status in (Status.IN_PROGRESS.value, Status.REPEATING.value):
        return is_caught_up(media)
    item = getattr(media, "item", None)
    if item and media.status == Status.PLANNING.value:
        return _unaired(item, lookup=item.media_type == MediaTypes.SEASON.value)
    return False


def _unaired(item, *, lookup):
    """Return whether all of an item's known releases are still to come.

    Uses the item's `prefetched_events` when they were loaded, else queries them
    if `lookup`.
    """
    events = getattr(item, "prefetched_events", None)
    if events is not None:
        first_release = min((event.datetime for event in events), default=None)
    elif lookup:
        first_release = Event.objects.filter(item=item).aggregate(
            first=Min("datetime"),
        )["first"]
    else:
        return False
    return first_release is not None and first_release > timezone.now()


def _watched(media_type):
    """Return whether a media type is watched, so planning it means a watchlist."""
    return config.get_verb(media_type, past_tense=False) == "watch"


def _now():
    """Return the current time the way the tracking form fills it in."""
    now = timezone.localtime().replace(second=0, microsecond=0)
    if settings.TRACK_TIME:
        return now
    return now.replace(hour=0, minute=0)
