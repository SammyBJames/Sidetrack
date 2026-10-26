"""The fields a library can be sorted or filtered by, for each media type.

Each media type has its own fields, named in its words, e.g. "Times watched" and
"Pages". A field's sort is an expression the database orders by, so lists page
in SQL.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from django.db.models import (
    Case,
    Count,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Lower

from app import config
from app.models import STATUS_SORT_ORDER, Episode, MediaTypes
from users.models import MediaSortChoices, SortOrderChoices, default_sort_order

# The sort a list starts with, and falls back to when its sort doesn't exist for
# its media type
DEFAULT_SORT = MediaSortChoices.SCORE

NUMBER = "number"
DATE = "date"
YEAR = "year"
CHOICE = "choice"
GENRE = "genre"
TEXT = "text"

ALL_TYPES = (
    MediaTypes.TV.value,
    MediaTypes.SEASON.value,
    MediaTypes.MOVIE.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.BOARDGAME.value,
)
SHOWS = (MediaTypes.TV.value, MediaTypes.SEASON.value)
EPISODIC = (*SHOWS, MediaTypes.ANIME.value)
PROGRESSED = (
    *EPISODIC,
    MediaTypes.MANGA.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
)
# Seasons and comics have no airing status from their providers
AIRING = (MediaTypes.TV.value, MediaTypes.ANIME.value, MediaTypes.MANGA.value)

# How a sort reads in each direction, ascending then descending
DATE_ORDER_LABELS = ("Oldest first", "Newest first")
VALUE_ORDER_LABELS = ("Lowest first", "Highest first")
COUNT_ORDER_LABELS = ("Least first", "Most first")
LENGTH_ORDER_LABELS = ("Shortest first", "Longest first")

# What a media type's length is counted in
LENGTH_UNITS = {
    MediaTypes.TV.value: "episodes",
    MediaTypes.SEASON.value: "episodes",
    MediaTypes.ANIME.value: "episodes",
    MediaTypes.MANGA.value: "chapters",
    MediaTypes.BOOK.value: "pages",
    MediaTypes.COMIC.value: "issues",
}


@dataclass(frozen=True)
class LibraryField:
    """A field of one media type's library, as its sort and how to show it.

    `sort` takes the media type and returns the expression a list orders by, or
    is None for a field that is only filtered by. `filter` is the field's
    filter, None while it has none. `kind` is number, date, year, choice, genre or text
    (titles, which are sorted but not filtered), and `unit` names what a number
    counts, e.g. "pages", "min" or "%".
    """

    key: str
    label: str
    kind: str
    media_type: str
    sort: Callable | None
    default_order: str
    order_labels: tuple
    unit: str = ""
    filter: Callable | None = None

    def format(self, value):
        """Return a value of the field as a list shows it, e.g. "2h 5m"."""
        if value is None or value == "":
            return ""
        if self.kind == DATE:
            return f"{value:%b} {value.day}, {value.year}"
        if self.kind == YEAR:
            return str(getattr(value, "year", value))
        if self.kind == NUMBER:
            return self._format_number(value)
        if self.kind == CHOICE:
            return str(value).replace("_", " ").capitalize()
        return str(value)

    def _format_number(self, value):
        """Return a number with its unit, minutes as hours and minutes."""
        if self.unit == "min":
            hours, minutes = divmod(int(value), 60)
            if not hours:
                return f"{minutes}m"
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        number = f"{value:.1f}" if isinstance(value, Decimal) else f"{value:,}"
        unit = self.unit
        if unit.endswith("s") and value == 1:
            unit = unit[:-1]
        if unit == "%":
            return f"{number}%"
        return f"{number} {unit}" if unit else number


def _item_column(column):
    """Return a sort by a column of the entry's item."""
    return lambda _media_type: F(f"item__{column}")


def _entry_column(column):
    """Return a sort by a column of the entry."""
    return lambda _media_type: F(column)


def _title(_media_type):
    """Return the sort expression of titles, ignoring case."""
    return Lower("item__title")


def _status(_media_type):
    """Return the sort expression of statuses, from being gone through to put aside."""
    return Case(
        *(
            When(status=status, then=rank)
            for rank, status in enumerate(STATUS_SORT_ORDER)
        ),
        output_field=IntegerField(),
    )


def _airing_status(_media_type):
    """Return the sort expression of airing statuses, from airing to ended."""
    ranks = ("airing", "upcoming", "ended")
    return Case(
        *(
            When(item__airing_status=status, then=rank)
            for rank, status in enumerate(ranks)
        ),
        output_field=IntegerField(),
    )


def _episode_values(media_type):
    """Return the expression each sort of the entry's own dates and progress takes.

    Shows and seasons have no dates or progress columns, so they're those of
    their watched episodes, specials aside.
    """
    sorts = MediaSortChoices
    if media_type == MediaTypes.TV.value:
        episodes = "seasons__episodes"
        regular = Q(seasons__item__season_number__gt=0)
        return {
            sorts.RECENT: Max(f"{episodes}__end_date", filter=regular),
            sorts.PROGRESS: Count(episodes, filter=regular),
            sorts.START_DATE: Min(f"{episodes}__end_date", filter=regular),
            sorts.END_DATE: Max(f"{episodes}__end_date", filter=regular),
        }
    if media_type == MediaTypes.SEASON.value:
        return {
            sorts.RECENT: Max("episodes__end_date"),
            sorts.PROGRESS: Max("episodes__item__episode_number"),
            sorts.START_DATE: Min("episodes__end_date"),
            sorts.END_DATE: Max("episodes__end_date"),
        }
    return {sorts.RECENT: F("progressed_at")}


def _from_episodes(key):
    """Return a sort by the entry's own value, or its episodes' for shows."""

    def build(media_type):
        """Return the sort expression for a media type."""
        value = _episode_values(media_type).get(key, F(key))
        if key == MediaSortChoices.RECENT:
            # Last activity, or when it was added for media never gone through
            return Coalesce(value, "created_at")
        return value

    return build


def _plays(media_type):
    """Return the sort expression of how many times media was finished.

    Shows and seasons count it as `TV.plays` and `Season.plays` do, by their
    least watched episode, specials aside. Other media counts its completed
    entries, annotated by the media list as `completed_entries`.
    """
    if media_type == MediaTypes.TV.value:
        watches = Episode.objects.filter(
            related_season__related_tv=OuterRef("pk"),
            related_season__item__season_number__gt=0,
        ).values("related_season", "item__episode_number")
    elif media_type == MediaTypes.SEASON.value:
        watches = Episode.objects.filter(related_season=OuterRef("pk")).values(
            "item__episode_number",
        )
    else:
        return F("completed_entries")
    least = watches.annotate(count=Count("id")).order_by("count").values("count")
    return Coalesce(Subquery(least[:1]), Value(0))


def _default_source_label(media_type):
    """Return the name of a media type's provider score, by its default source."""
    source = config.get_default_source_name(media_type)
    return f"{config.get_rating_source(source)['label']} rating"


def _by_type(values):
    """Return a label or unit that goes by the media type, from a dict of them."""
    return lambda media_type: values[media_type]


def _same(value):
    """Return a label or unit that is the same for every media type."""
    return lambda _media_type: value


# Every field with the media types it's for. Labels and units are a function of
# the media type. Genre and platform are only filtered by: they hold many values
# per title, so they have no sort. The order they're listed in is the order they
# show in.
_DEFINITIONS = (
    {
        "key": MediaSortChoices.RECENT,
        "label": _same("Last activity"),
        "kind": DATE,
        "types": ALL_TYPES,
        "sort": _from_episodes(MediaSortChoices.RECENT),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.ADDED,
        "label": _same("Date added"),
        "kind": DATE,
        "types": ALL_TYPES,
        "sort": _entry_column("added_at"),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.TITLE,
        "label": _same("Title"),
        "kind": TEXT,
        "types": ALL_TYPES,
        "sort": _title,
        "order_labels": ("A to Z", "Z to A"),
    },
    {
        "key": MediaSortChoices.STATUS,
        "label": _same("Status"),
        "kind": CHOICE,
        "types": ALL_TYPES,
        "sort": _status,
        "order_labels": ("In progress first", "Dropped first"),
    },
    {
        "key": MediaSortChoices.SCORE,
        "label": _same("Your rating"),
        "kind": NUMBER,
        "types": ALL_TYPES,
        "sort": _entry_column("score"),
        "order_labels": VALUE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.PLAYS,
        "label": lambda media_type: (
            f"Times {config.get_verb(media_type, past_tense=True)}"
        ),
        "kind": NUMBER,
        "types": ALL_TYPES,
        "sort": _plays,
        "order_labels": COUNT_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.PROGRESS,
        "label": lambda media_type: (
            "Time played" if media_type == MediaTypes.GAME.value else "Progress"
        ),
        "kind": NUMBER,
        "types": (*PROGRESSED, MediaTypes.GAME.value),
        "sort": _from_episodes(MediaSortChoices.PROGRESS),
        "unit": lambda media_type: LENGTH_UNITS.get(media_type, "min"),
        "order_labels": COUNT_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.START_DATE,
        "label": _same("Start date"),
        "kind": DATE,
        "types": ALL_TYPES,
        "sort": _from_episodes(MediaSortChoices.START_DATE),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.END_DATE,
        "label": _same("End date"),
        "kind": DATE,
        "types": ALL_TYPES,
        "sort": _from_episodes(MediaSortChoices.END_DATE),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.RELEASE,
        "label": _same("Release year"),
        "kind": YEAR,
        "types": ALL_TYPES,
        "sort": _item_column("release_date"),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.PROVIDER_SCORE,
        "label": _default_source_label,
        "kind": NUMBER,
        # Comic Vine gives no score
        "types": tuple(t for t in ALL_TYPES if t != MediaTypes.COMIC.value),
        "sort": _item_column("provider_score"),
        "order_labels": VALUE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.IMDB,
        "label": _same("IMDb rating"),
        "kind": NUMBER,
        "types": (MediaTypes.MOVIE.value, MediaTypes.TV.value),
        "sort": _item_column("imdb_score"),
        "order_labels": VALUE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.ROTTEN_TOMATOES,
        "label": _same("Rotten Tomatoes"),
        "kind": NUMBER,
        "types": (MediaTypes.MOVIE.value, MediaTypes.TV.value),
        "sort": _item_column("rotten_tomatoes"),
        "unit": _same("%"),
        "order_labels": VALUE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.METACRITIC,
        "label": _same("Metacritic"),
        "kind": NUMBER,
        "types": (MediaTypes.MOVIE.value, MediaTypes.TV.value),
        "sort": _item_column("metacritic"),
        "order_labels": VALUE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.RUNTIME,
        "label": lambda media_type: {
            MediaTypes.MOVIE.value: "Runtime",
            MediaTypes.BOARDGAME.value: "Play time",
        }.get(media_type, "Episode length"),
        "kind": NUMBER,
        "types": (*EPISODIC, MediaTypes.MOVIE.value, MediaTypes.BOARDGAME.value),
        "sort": _item_column("runtime"),
        "unit": _same("min"),
        "order_labels": LENGTH_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.SEASONS,
        "label": _same("Seasons"),
        "kind": NUMBER,
        "types": (MediaTypes.TV.value,),
        "sort": _item_column("season_count"),
        "unit": _same("seasons"),
        "order_labels": COUNT_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.LENGTH,
        "label": lambda media_type: LENGTH_UNITS[media_type].capitalize(),
        "kind": NUMBER,
        "types": tuple(LENGTH_UNITS),
        "sort": _item_column("length"),
        "unit": _by_type(LENGTH_UNITS),
        "order_labels": LENGTH_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.AIRING_STATUS,
        "label": lambda media_type: (
            "Publishing status"
            if media_type == MediaTypes.MANGA.value
            else "Airing status"
        ),
        "kind": CHOICE,
        "types": AIRING,
        "sort": _airing_status,
        "order_labels": ("Airing first", "Ended first"),
    },
    {
        "key": MediaSortChoices.TIME_TO_BEAT,
        "label": _same("Time to beat"),
        "kind": NUMBER,
        "types": (MediaTypes.GAME.value,),
        "sort": _item_column("time_to_beat"),
        "unit": _same("min"),
        "order_labels": LENGTH_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.PLAYERS,
        "label": _same("Players"),
        "kind": NUMBER,
        "types": (MediaTypes.BOARDGAME.value,),
        "sort": _item_column("max_players"),
        "unit": _same("players"),
        "order_labels": ("Fewest first", "Most first"),
    },
    {
        "key": "genre",
        "label": _same("Genre"),
        "kind": GENRE,
        "types": ALL_TYPES,
        "sort": None,
        "order_labels": (),
    },
    {
        "key": "platform",
        "label": _same("Platform"),
        "kind": CHOICE,
        "types": (MediaTypes.GAME.value,),
        "sort": None,
        "order_labels": (),
    },
)


@lru_cache(maxsize=1)
def _registry():
    """Return each media type's fields by key, in the order they show in."""
    registry = {media_type: {} for media_type in ALL_TYPES}
    for definition in _DEFINITIONS:
        key = definition["key"]
        for media_type in definition["types"]:
            registry[media_type][key] = LibraryField(
                key=key,
                label=definition["label"](media_type),
                kind=definition["kind"],
                media_type=media_type,
                sort=definition["sort"],
                default_order=(
                    default_sort_order(key)
                    if definition["sort"]
                    else SortOrderChoices.DESC
                ),
                order_labels=definition["order_labels"],
                unit=definition.get("unit", _same(""))(media_type),
            )
    return registry


def get_fields(media_type):
    """Return a media type's fields by key, sortable or not."""
    return _registry()[media_type]


def get_sortable_fields(media_type):
    """Return a media type's fields a list can be sorted by."""
    return [field for field in get_fields(media_type).values() if field.sort]


def get_sort_field(media_type, key):
    """Return the field of a sort for a media type.

    A sort the media type doesn't have, e.g. progress for movies, is the default
    sort's field.
    """
    fields = get_fields(media_type)
    field = fields.get(key)
    return field if field and field.sort else fields[DEFAULT_SORT]
