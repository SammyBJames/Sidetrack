"""The fields a library can be sorted or filtered by, for each media type.

Each media type has its own fields, named in its words, e.g. "Times watched" and
"Pages". A field's sort is an expression the database orders by, so lists page
in SQL.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from functools import lru_cache

from django.apps import apps
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
    Window,
)
from django.db.models.functions import Coalesce, FirstValue, Lower
from django.utils import timezone

from app import config
from app.models import STATUS_SORT_ORDER, AiringStatus, Episode, MediaTypes
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


# The headings the sort menu lists sorts under, by field key; a sort not named
# here is about the title
SORT_GROUPS = {
    **dict.fromkeys(
        (
            MediaSortChoices.RECENT,
            MediaSortChoices.ADDED,
            MediaSortChoices.STATUS,
            MediaSortChoices.SCORE,
            MediaSortChoices.PLAYS,
            MediaSortChoices.PROGRESS,
            MediaSortChoices.START_DATE,
            MediaSortChoices.END_DATE,
        ),
        "Your activity",
    ),
    **dict.fromkeys(
        (
            MediaSortChoices.PROVIDER_SCORE,
            MediaSortChoices.IMDB,
            MediaSortChoices.ROTTEN_TOMATOES,
            MediaSortChoices.METACRITIC,
        ),
        "Ratings",
    ),
}
ABOUT_GROUP = "About the title"


@dataclass(frozen=True)
class LibraryField:
    """A field of one media type's library, as its sort and how to show it.

    `sort` takes the media type and returns the expression a list orders by, or
    is None for a field that is only filtered by. `filter` is None for a field
    that is only sorted by. `kind` is number, date, year, choice, genre or text
    (titles, which are sorted but not filtered), and `unit` names what a number
    counts, e.g. "pages", "min" or "%".
    """

    key: str
    label: str
    kind: str
    sort: Callable | None
    default_order: str
    order_labels: tuple
    unit: str = ""
    # How the field is filtered, or None for a field that isn't: takes the media
    # list, its media type and the parsed `LibraryFilter`, and returns the list
    filter: Callable | None = None
    # The values a choice filter accepts with their labels, as (value, label)
    # pairs, empty for any
    choices: tuple = ()
    # A value of the field's own param that matches media without a value
    unset_choice: str = ""
    # The item column a choice filter reads: one value, or delimited text (as
    # genres) when the field has no `choices`
    column: str = ""

    @property
    def sort_group(self):
        """Return the heading the sort menu lists the field's sort under."""
        return SORT_GROUPS.get(self.key, ABOUT_GROUP)

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
            return _hours_minutes(value)
        number = f"{value:.1f}" if isinstance(value, Decimal) else f"{value:,}"
        unit = self.unit
        if unit.endswith("s") and value == 1:
            unit = unit[:-1]
        if unit == "%":
            return f"{number}%"
        return f"{number} {unit}" if unit else number


def _hours_minutes(minutes):
    """Return a number of minutes as hours and minutes, e.g. "2h 5m"."""
    hours, minutes = divmod(int(minutes), 60)
    if not hours:
        return f"{minutes}m"
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


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


# Where a field's value is read from to filter by it
ITEM = "item"  # a column of the item, filtered as a plain `item__` lookup
ENTRY = "entry"  # a column of the entry, of the latest entry of each media
DERIVED = "derived"  # of the latest entry, or of a show's watched episodes
DIRECT = "direct"  # already a value of the media list, as its sort reads it

# The most a number filter takes, so a huge value isn't sent to the database
MAX_FILTER_NUMBER = Decimal(10**9)
MAX_FILTER_YEAR = 9999


@dataclass(frozen=True)
class LibraryFilter:
    """A filter asked for on a media type's list, parsed from its params.

    A range has `min` and/or `max` (numbers, dates or years as the field's kind
    says), a choice has `values`, and `unset` asks for media without a value too.
    """

    field: LibraryField
    min: Decimal | date | int | None = None
    max: Decimal | date | int | None = None
    values: tuple = ()
    unset: bool = False
    # The date preset a date range was asked for by, e.g. "30d", already in `min`
    within: str = ""


def _latest_entry_value(expression):
    """Return a window of an expression's value in each media's latest entry."""
    return Window(
        expression=FirstValue(expression),
        partition_by=[F("item")],
        order_by=F("created_at").desc(),
    )


def _bound(value, *, upper, as_datetime):
    """Return a date or year bound as what the column compares with."""
    if not as_datetime:
        return value
    # Whole days, so the end of the last day is the start of the next
    day = value + timedelta(days=1) if upper else value
    return timezone.make_aware(datetime.combine(day, time.min))


def _range_condition(name, flt, *, as_datetime=False):
    """Return the condition of a value being in a filter's range or unset."""
    condition = Q()
    if flt.min is not None:
        lookup = f"{name}__gte"
        condition &= Q(
            **{lookup: _bound(flt.min, upper=False, as_datetime=as_datetime)}
        )
    if flt.max is not None:
        lookup = f"{name}__lt" if as_datetime else f"{name}__lte"
        condition &= Q(**{lookup: _bound(flt.max, upper=True, as_datetime=as_datetime)})
    if flt.unset:
        condition |= Q(**{f"{name}__isnull": True})
    return condition


def _value_name(queryset, media_type, field, scope):
    """Return the media list and the name its filter reads the field's value by.

    An entry's value is read in a window over the media's entries, as filters
    outside one change which entry is the latest. A show's dates and progress
    are those of its episodes, annotated the way its sort reads them.
    """
    expression = field.sort(media_type)
    if scope == ITEM or (scope == DIRECT and isinstance(expression, F)):
        return queryset, expression.name
    if scope == ENTRY or (scope == DERIVED and media_type not in SHOWS):
        expression = _latest_entry_value(expression)
    name = f"filter_{field.key}"
    return queryset.annotate(**{name: expression}), name


def _year_dates(flt):
    """Return a year filter as the dates its years span."""
    return LibraryFilter(
        flt.field,
        min=date(flt.min, 1, 1) if flt.min is not None else None,
        max=date(flt.max, 12, 31) if flt.max is not None else None,
        unset=flt.unset,
    )


# The fields whose numbers have a fraction, by the step their inputs take
FRACTION_STEPS = {
    MediaSortChoices.SCORE: "0.5",
    MediaSortChoices.PROVIDER_SCORE: "0.1",
    MediaSortChoices.IMDB: "0.1",
}


def _whole_bounds(flt):
    """Return a filter's bounds as whole numbers that keep the same media.

    A minimum is rounded up and a maximum down, as the database would cut a
    fraction off both.
    """
    return LibraryFilter(
        flt.field,
        min=flt.min.to_integral_value(ROUND_CEILING) if flt.min is not None else None,
        max=flt.max.to_integral_value(ROUND_FLOOR) if flt.max is not None else None,
        unset=flt.unset,
    )


def _range_filter(scope, *, as_datetime=False, year=False):
    """Return the filter of a field's range, read the way its scope says."""

    def apply(queryset, media_type, flt):
        """Return the queryset narrowed to the range, unchanged if it has no bounds."""
        queryset, name = _value_name(queryset, media_type, flt.field, scope)
        if flt.field.kind == NUMBER and flt.field.key not in FRACTION_STEPS:
            flt = _whole_bounds(flt)
        if year:
            flt = _year_dates(flt)
        condition = _range_condition(name, flt, as_datetime=as_datetime)
        return queryset.filter(condition) if condition else queryset

    return apply


def _overlap_filter(queryset, _media_type, flt):
    """Return the games whose players meet the player counts asked for."""
    flt = _whole_bounds(flt)
    if flt.min is not None:
        queryset = queryset.filter(item__max_players__gte=flt.min)
    if flt.max is not None:
        queryset = queryset.filter(item__min_players__lte=flt.max)
    return queryset


def _choice_filter(queryset, _media_type, flt):
    """Return the media whose item's value in the field's column is any asked for."""
    return queryset.filter(**{f"item__{flt.field.column}__in": flt.values})


def _delimited_filter(queryset, _media_type, flt):
    """Return the media whose item's delimited text has any of the names.

    Names are matched whole, ignoring case, as the text is "|Drama|Comedy|".
    """
    condition = Q()
    for name in flt.values:
        condition |= Q(**{f"item__{flt.field.column}__icontains": f"|{name}|"})
    return queryset.filter(condition)


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
        "filter": _range_filter(DERIVED, as_datetime=True),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.ADDED,
        "label": _same("Date added"),
        "kind": DATE,
        "types": ALL_TYPES,
        "sort": _entry_column("added_at"),
        "filter": _range_filter(DIRECT, as_datetime=True),
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
        "filter": _range_filter(ENTRY),
        "unset_choice": "unrated",
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
        "filter": _range_filter(DIRECT),
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
        "filter": _range_filter(DERIVED),
        "unit": lambda media_type: LENGTH_UNITS.get(media_type, "min"),
        "order_labels": COUNT_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.START_DATE,
        "label": _same("Start date"),
        "kind": DATE,
        "types": ALL_TYPES,
        "sort": _from_episodes(MediaSortChoices.START_DATE),
        "filter": _range_filter(DERIVED, as_datetime=True),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.END_DATE,
        "label": _same("End date"),
        "kind": DATE,
        "types": ALL_TYPES,
        "sort": _from_episodes(MediaSortChoices.END_DATE),
        "filter": _range_filter(DERIVED, as_datetime=True),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.RELEASE,
        "label": _same("Release year"),
        "kind": YEAR,
        "types": ALL_TYPES,
        "sort": _item_column("release_date"),
        "filter": _range_filter(ITEM, year=True),
        "order_labels": DATE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.PROVIDER_SCORE,
        "label": _default_source_label,
        "kind": NUMBER,
        # Comic Vine gives no score
        "types": tuple(t for t in ALL_TYPES if t != MediaTypes.COMIC.value),
        "sort": _item_column("provider_score"),
        "filter": _range_filter(ITEM),
        "order_labels": VALUE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.IMDB,
        "label": _same("IMDb rating"),
        "kind": NUMBER,
        "types": (MediaTypes.MOVIE.value, MediaTypes.TV.value),
        "sort": _item_column("imdb_score"),
        "filter": _range_filter(ITEM),
        "order_labels": VALUE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.ROTTEN_TOMATOES,
        "label": _same("Rotten Tomatoes"),
        "kind": NUMBER,
        # OMDb has it for movies, but rarely for shows
        "types": (MediaTypes.MOVIE.value,),
        "sort": _item_column("rotten_tomatoes"),
        "filter": _range_filter(ITEM),
        "unit": _same("%"),
        "order_labels": VALUE_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.METACRITIC,
        "label": _same("Metacritic"),
        "kind": NUMBER,
        "types": (MediaTypes.MOVIE.value,),
        "sort": _item_column("metacritic"),
        "filter": _range_filter(ITEM),
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
        "filter": _range_filter(ITEM),
        "unit": _same("min"),
        "order_labels": LENGTH_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.SEASONS,
        "label": _same("Seasons"),
        "kind": NUMBER,
        "types": (MediaTypes.TV.value,),
        "sort": _item_column("season_count"),
        "filter": _range_filter(ITEM),
        "unit": _same("seasons"),
        "order_labels": COUNT_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.LENGTH,
        "label": lambda media_type: LENGTH_UNITS[media_type].capitalize(),
        "kind": NUMBER,
        "types": tuple(LENGTH_UNITS),
        "sort": _item_column("length"),
        "filter": _range_filter(ITEM),
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
        "filter": _choice_filter,
        "column": "airing_status",
        "choices": tuple(AiringStatus.choices),
        "order_labels": ("Airing first", "Ended first"),
    },
    {
        "key": MediaSortChoices.TIME_TO_BEAT,
        "label": _same("Time to beat"),
        "kind": NUMBER,
        "types": (MediaTypes.GAME.value,),
        "sort": _item_column("time_to_beat"),
        "filter": _range_filter(ITEM),
        "unit": _same("min"),
        "order_labels": LENGTH_ORDER_LABELS,
    },
    {
        "key": MediaSortChoices.PLAYERS,
        "label": _same("Players"),
        "kind": NUMBER,
        "types": (MediaTypes.BOARDGAME.value,),
        "sort": _item_column("max_players"),
        "filter": _overlap_filter,
        "unit": _same("players"),
        "order_labels": ("Fewest first", "Most first"),
    },
    {
        "key": "genre",
        "label": _same("Genre"),
        "kind": GENRE,
        "types": ALL_TYPES,
        "sort": None,
        "filter": _delimited_filter,
        "column": "genres",
        "order_labels": (),
    },
    {
        "key": "platform",
        "label": _same("Platform"),
        "kind": CHOICE,
        "types": (MediaTypes.GAME.value,),
        "sort": None,
        "filter": _delimited_filter,
        "column": "platforms",
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
                sort=definition["sort"],
                default_order=(
                    default_sort_order(key)
                    if definition["sort"]
                    else SortOrderChoices.DESC
                ),
                order_labels=definition["order_labels"],
                unit=definition.get("unit", _same(""))(media_type),
                filter=definition.get("filter"),
                choices=definition.get("choices", ()),
                unset_choice=definition.get("unset_choice", ""),
                column=definition.get("column", ""),
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


def get_filterable_fields(media_type):
    """Return a media type's fields a list can be filtered by."""
    return [field for field in get_fields(media_type).values() if field.filter]


def _parse_number(value):
    """Return a number param as a Decimal, or None if it isn't a usable one."""
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        return None
    if not number.is_finite() or abs(number) > MAX_FILTER_NUMBER:
        return None
    return number


def _parse_date(value):
    """Return a YYYY-MM-DD param as a date, or None if it isn't one."""
    try:
        day = date.fromisoformat(value.strip())
    except ValueError:
        return None
    # The first and last years overflow once moved a day or to UTC
    return day if 1 < day.year < MAX_FILTER_YEAR else None


def _parse_year(value):
    """Return a year param as an int, or None if it isn't a year."""
    year = _parse_number(value)
    if year is None or year != year.to_integral_value():
        return None
    return int(year) if 1 <= year < MAX_FILTER_YEAR + 1 else None


_PARSERS = {NUMBER: _parse_number, DATE: _parse_date, YEAR: _parse_year}

# The date presets a `<key>_within` param takes, as (days back or None for the
# start of this year, how a pill says it, the preset's name in the panel)
WITHIN = {
    "7d": (7, "in the past week", "Past week"),
    "30d": (30, "in the past month", "Past month"),
    "90d": (90, "in the past 3 months", "Past 3 months"),
    "365d": (365, "in the past year", "Past year"),
    "year": (None, "this year", "This year"),
}
# What a date field's pill says was done in the preset's time
DATE_VERBS = {
    MediaSortChoices.RECENT: "Active",
    MediaSortChoices.ADDED: "Added",
    MediaSortChoices.START_DATE: "Started",
    MediaSortChoices.END_DATE: "Ended",
}
PLAYER_CHIPS = 6
# The most names a choice row lists before it counts the rest
MAX_NAMED = 2
# The presets the panel offers; the others are still read from older links
PANEL_PRESETS = ("7d", "30d", "365d", "year")


def _within_start(preset):
    """Return the first day a date preset covers, as of today."""
    days = WITHIN[preset][0]
    today = timezone.localdate()
    return date(today.year, 1, 1) if days is None else today - timedelta(days=days)


def parse_filters(media_type, params):
    """Return the filters a request asks for on a media type's list, by key.

    Params are `<key>_min` and `<key>_max` for numbers, dates (YYYY-MM-DD) and
    years, and `<key>` repeated for choices, with `score=unrated` for media with
    no rating. What isn't valid, such as other text in a number, a choice the
    field doesn't have, a field the media type lacks or an unknown param, is
    ignored. A minimum above the maximum is kept and leaves no media. A date
    field also takes `<key>_within`, a preset counted back from today that
    replaces its minimum.
    """
    raw = {name: params.getlist(name) for name in params}
    filters = {}
    for field in get_filterable_fields(media_type):
        if field.kind in _PARSERS:
            parse = _PARSERS[field.kind]
            low = next(iter(raw.get(f"{field.key}_min", [])), None)
            high = next(iter(raw.get(f"{field.key}_max", [])), None)
            flt = LibraryFilter(
                field,
                min=parse(low) if low is not None else None,
                max=parse(high) if high is not None else None,
                unset=field.unset_choice in raw.get(field.key, []),
            )
            preset = next(iter(raw.get(f"{field.key}_within", [])), None)
            if field.kind == DATE and preset in WITHIN:
                flt = replace(flt, min=_within_start(preset), within=preset)
            if flt.min is not None or flt.max is not None or flt.unset:
                filters[field.key] = flt
        else:
            values = []
            for asked in raw.get(field.key, []):
                value = asked.replace("|", "").strip()[:MAX_NAME_LENGTH]
                valid = value in dict(field.choices) if field.choices else value
                if valid and value.lower() not in {v.lower() for v in values}:
                    values.append(value)
            if values:
                filters[field.key] = LibraryFilter(field, values=tuple(values))
    return filters


# The longest genre or platform name a filter takes
MAX_NAME_LENGTH = 100


def _plain(value):
    """Return a number without a trailing .0, as the filter was typed."""
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return value


def _fits(flt):
    """Return "Fits 4 players" for a players filter a chip sets, else None."""
    if flt.field.key != MediaSortChoices.PLAYERS or flt.min is None:
        return None
    if flt.min == flt.max and flt.min <= PLAYER_CHIPS:
        return f"Fits {flt.min:g} player{'' if flt.min == 1 else 's'}"
    if flt.min == PLAYER_CHIPS and flt.max is None:
        return f"Fits {PLAYER_CHIPS}+ players"
    return None


def _label(flt):
    """Return the label of a range filter, e.g. "IMDb ≥ 7" or "Released 2000-2010"."""
    field = flt.field
    if flt.within:
        return f"{DATE_VERBS[field.key]} {WITHIN[flt.within][1]}"
    if fits := _fits(flt):
        return fits
    name = "Released" if field.kind == YEAR else field.label
    low, high = (
        None if bound is None else field.format(_plain(bound))
        for bound in (flt.min, flt.max)
    )
    dated = field.kind in (DATE, YEAR)
    if low is not None and high is not None:
        text = low if low == high else f"{low}\u2013{high}"
    elif low is not None:
        text = f"from {low}" if dated else f"≥ {low}"
    elif high is not None:
        text = f"until {high}" if dated else f"≤ {high}"
    else:
        return "Unrated"
    suffix = " or unrated" if flt.unset else ""
    return f"{name} {text}{suffix}"


def describe_filters(filters):
    """Return the filters as pills, each with the label it shows.

    Each has the `params` it's asked for by and, for one of a choice's values,
    that `value`, so removing it leaves the choice's other values. Others are
    removed by dropping all their params.
    """
    pills = []
    for key, flt in filters.items():
        if flt.values:
            for value in flt.values:
                label = dict(flt.field.choices).get(value, value)
                pills.append({"label": label, "params": [key], "value": value})
        else:
            params = [f"{key}_min", f"{key}_max"]
            if flt.field.kind == DATE:
                params.append(f"{key}_within")
            if flt.field.unset_choice:
                params.append(key)
            pills.append({"label": _label(flt), "params": params, "value": None})
    return pills


def present_choices(user, media_type):
    """Return the genres and platforms of a user's media of a type, with counts.

    By field key, each a list of `value` and `count` of titles, most first.
    Names that differ only in case count as one, in the first spelling seen.
    """
    model = apps.get_model("app", media_type)
    choices = {}
    for field in get_filterable_fields(media_type):
        if not field.column or field.choices:
            continue
        # Without the model's ordering, whose columns DISTINCT would keep, so a
        # title with several entries counts once
        rows = (
            model.objects.filter(user=user)
            .order_by()
            .values_list("item_id", f"item__{field.column}")
            .distinct()
        )
        counts = {}
        spellings = {}
        for _item_id, names in rows:
            for name in {n for n in names.split("|") if n}:
                counts[name.lower()] = counts.get(name.lower(), 0) + 1
                spellings.setdefault(name.lower(), name)
        choices[field.key] = [
            {"value": spellings[name], "count": count}
            for name, count in sorted(counts.items(), key=lambda c: (-c[1], c[0]))
        ]
    return choices


# The groups the panel lists filters in, by field key; the rest are about the title
RATING_KEYS = (
    MediaSortChoices.PROVIDER_SCORE,
    MediaSortChoices.IMDB,
    MediaSortChoices.ROTTEN_TOMATOES,
    MediaSortChoices.METACRITIC,
)
ACTIVITY_KEYS = (
    MediaSortChoices.SCORE,
    MediaSortChoices.PLAYS,
    MediaSortChoices.PROGRESS,
    MediaSortChoices.START_DATE,
    MediaSortChoices.END_DATE,
    MediaSortChoices.ADDED,
    MediaSortChoices.RECENT,
)


def _input_value(value):
    """Return a filter's bound as its input shows it."""
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(_plain(value))


# The scales of the rating fields, the same for every library
FIXED_BOUNDS = {
    MediaSortChoices.SCORE: (0, 10),
    MediaSortChoices.PROVIDER_SCORE: (0, 10),
    MediaSortChoices.IMDB: (0, 10),
    MediaSortChoices.ROTTEN_TOMATOES: (0, 100),
    MediaSortChoices.METACRITIC: (0, 100),
}


def _most_plays(user, model, media_type):
    """Return the most times any of a user's media was finished, or None.

    Shows and seasons read it by the same expression their sort and filter do.
    """
    if media_type in SHOWS:
        shows = model.objects.filter(user=user).annotate(plays=_plays(media_type))
        return shows.aggregate(Max("plays"))["plays__max"]
    watches = model.objects.filter(user=user).values("item")
    return (
        watches.order_by()
        .annotate(count=Count("id"))
        .aggregate(Max("count"))["count__max"]
    )


def _bound_columns(field, media_type):
    """Return the item or entry columns whose lowest and highest value bound a field.

    The low one is None for a field that starts at 0, and the whole result is None
    for a field that is bound some other way.
    """
    if field.key == MediaSortChoices.PROGRESS:
        # A show's progress is its episodes watched, up to its length
        return (None, "item__length" if media_type in SHOWS else "progress")
    expression = field.sort(media_type)
    if isinstance(expression, F) and field.key != MediaSortChoices.PLAYS:
        return (expression.name, expression.name)
    return None


# The fields about the title, which are only worth listing when some title in the
# library has a value for them. Those of your own entries always are.
DETAIL_KEYS = (
    MediaSortChoices.RELEASE,
    MediaSortChoices.PROVIDER_SCORE,
    MediaSortChoices.IMDB,
    MediaSortChoices.ROTTEN_TOMATOES,
    MediaSortChoices.METACRITIC,
    MediaSortChoices.RUNTIME,
    MediaSortChoices.SEASONS,
    MediaSortChoices.LENGTH,
    MediaSortChoices.AIRING_STATUS,
    MediaSortChoices.TIME_TO_BEAT,
    MediaSortChoices.PLAYERS,
)


def _detail_column(field, media_type):
    """Return the item column of a field about the title."""
    return f"item__{field.column}" if field.column else field.sort(media_type).name


def _found_bounds(columns, found):
    """Return the `(low, high)` bounds of the fields in `columns` from an aggregate."""
    bounds = {}
    for key in columns:
        low, high = found.get(f"{key}_low") or 0, found[f"{key}_high"]
        if high is None:
            continue
        if key == MediaSortChoices.RELEASE:
            low, high = low.year, high.year
        bounds[key] = (math.floor(low), math.ceil(high))
    return bounds


def range_bounds(user, media_type):
    """Return the range fields' bounds and the fields about the title with values.

    The bounds are by field key, as `(low, high)`: the fixed scale of a rating
    field, or what the user's library holds in the field, from 0 for times
    finished and progress. Fields about the title (`DETAIL_KEYS`) the library has
    no value for are left out of the bounds, and out of the set returned beside
    them. Costs two queries.
    """
    model = apps.get_model("app", media_type)
    all_fields = get_fields(media_type)
    fields = [
        field
        for field in get_filterable_fields(media_type)
        if field.kind in (NUMBER, YEAR) and field.key != MediaSortChoices.PLAYERS
    ]
    fixed = [field.key for field in fields if field.key in FIXED_BOUNDS]
    bounds = {}
    if MediaSortChoices.PLAYS in (field.key for field in fields):
        most = _most_plays(user, model, media_type)
        if most:
            bounds[MediaSortChoices.PLAYS] = (0, most)
    columns = {
        field.key: _bound_columns(field, media_type)
        for field in fields
        if field.key not in fixed and field.key not in bounds
    }
    columns = {key: pair for key, pair in columns.items() if pair}
    aggregates = {}
    for key, (low, high) in columns.items():
        aggregates[f"{key}_high"] = Max(high)
        if low:
            aggregates[f"{key}_low"] = Min(low)
    details = {
        key: _detail_column(all_fields[key], media_type)
        for key in DETAIL_KEYS
        if key in all_fields
    }
    for key, column in details.items():
        aggregates.setdefault(f"{key}_high", Max(column))
    found = model.objects.filter(user=user).aggregate(**aggregates)
    present = {key for key in details if found[f"{key}_high"] not in (None, "")}
    for key in fixed:
        if key == MediaSortChoices.SCORE or key in present:
            bounds[key] = FIXED_BOUNDS[key]
    bounds.update(_found_bounds(columns, found))
    return bounds, present


def listed_sort_fields(media_type, present, current):
    """Return the fields a list can be sorted by that are worth listing.

    A field about the title that no title in the library has a value for, as
    `range_bounds` finds in `present`, is left out, but never the `current` sort.
    """
    return [
        field
        for field in get_sortable_fields(media_type)
        if field.key == current or field.key not in DETAIL_KEYS or field.key in present
    ]


def _bare(value, number_format):
    """Return a bound as a slider shows it, without its unit, e.g. "7", "2h 5m"."""
    value = _plain(value)
    if number_format == "minutes":
        return _hours_minutes(value)
    if number_format == "year":
        return str(value)
    if isinstance(value, Decimal) and number_format == "decimal":
        return f"{value:.1f}"
    return f"{value:,}"


def _range_summary(low, high, *, unset=False, suffix=""):
    """Return a row's value as words from its lower and upper bound, e.g. "7+".

    The bounds are text, None for one not set. The client builds the same words
    from the controls (libraryFilters.js), so keep them in step.
    """
    if low is not None and high is not None:
        text = low if low == high else f"{low}\u2013{high}"
        text += suffix
    elif low is not None:
        # A unit in words reads "10+ episodes", a symbol or time "50%+" or "2h+"
        text = f"{low}+{suffix}" if suffix.startswith(" ") else f"{low}{suffix}+"
    elif high is not None:
        text = f"up to {high}{suffix}"
    else:
        text = ""
    if unset:
        return f"{text} or unrated" if text else "Unrated"
    return text or "Any"


def _slider(field, flt, bounds):
    """Return a number or year field's slider, or None without a range to slide.

    Its scale is the library's `bounds` of the field, widened to take what the
    address asks for. `min` and `max` are the values chosen, the ends of the
    scale (`floor` and `ceiling`) for a bound not asked for, which is no filter.
    """
    step = FRACTION_STEPS.get(field.key, "1")
    asked = [b for b in (flt.min, flt.max) if b is not None] if flt else []
    values = [*bounds.get(field.key, ()), *asked]
    if not values:
        return None
    low, high = min(values), max(values)
    if field.key not in FRACTION_STEPS:
        low, high = math.floor(low), math.ceil(high)
    if low == high and not asked:
        return None
    high = max(high, low + Decimal(step))
    if field.unit == "min":
        number_format = "minutes"
    elif field.kind == YEAR:
        number_format = "year"
    else:
        number_format = "decimal" if field.key in FRACTION_STEPS else "count"
    suffix = ""
    if field.unit == "%":
        suffix = "%"
    elif number_format == "count" and field.unit:
        suffix = f" {field.unit}"
    low_text, high_text = (
        None if bound is None else _bare(bound, number_format)
        for bound in (flt.min if flt else None, flt.max if flt else None)
    )
    summary = _range_summary(
        low_text,
        high_text,
        unset=bool(flt and flt.unset),
        suffix=suffix,
    )
    return {
        "key": field.key,
        "label": field.label,
        "summary": summary,
        "active": summary != "Any",
        "floor_text": _bare(low, number_format),
        "ceiling_text": _bare(high, number_format),
        "unit": field.unit,
        "control": "slider",
        "format": number_format,
        "step": step,
        "floor": _input_value(low),
        "ceiling": _input_value(high),
        # A bound asked for at the end of the scale is kept until it is moved
        "min_asked": bool(flt and flt.min is not None),
        "max_asked": bool(flt and flt.max is not None),
        "min": _input_value(flt.min if flt and flt.min is not None else low),
        "max": _input_value(flt.max if flt and flt.max is not None else high),
        "unset": field.unset_choice,
        "unset_checked": bool(flt and flt.unset),
    }


def _date_presets(field, flt):
    """Return a date field's preset chips and date inputs with their current values."""
    within = flt.within if flt else ""
    custom = bool(flt and not within and (flt.min or flt.max))
    start, end = (
        None if within or not flt else getattr(flt, bound) for bound in ("min", "max")
    )
    summary = WITHIN[within][2] if within else _date_summary(start, end)
    return {
        "key": field.key,
        "label": field.label,
        "summary": summary,
        "active": summary != "Any",
        "control": "dates",
        "mode": within or ("custom" if custom else ""),
        "presets": [
            {"value": value, "label": WITHIN[value][2]} for value in PANEL_PRESETS
        ],
        "inputs": [
            {
                "name": f"{field.key}_{bound}",
                "model": model,
                "value": _input_value(None if within else getattr(flt, bound, None)),
                "label": f"{field.label}, {words}",
            }
            for bound, model, words in (
                ("min", "from", "from"),
                ("max", "to", "up to"),
            )
        ],
    }


def _date_summary(start, end):
    """Return custom dates as words, e.g. "Mar 1 \u2013 Apr 2, 2026", "Any" for none.

    The client builds the same words from the date inputs (libraryFilters.js).
    """
    if start and end:
        first = f"{start:%b} {start.day}"
        if start.year != end.year:
            first = f"{first}, {start.year}"
        return f"{first} \u2013 {end:%b} {end.day}, {end.year}"
    if start:
        return f"From {start:%b} {start.day}, {start.year}"
    if end:
        return f"Until {end:%b} {end.day}, {end.year}"
    return "Any"


def _player_chips(field, flt):
    """Return the players chips, 1 to 6+, with the counts asked for."""
    low, high = (
        None if bound is None else f"{_plain(bound):g}"
        for bound in (flt.min if flt else None, flt.max if flt else None)
    )
    summary = (_fits(flt) if flt else None) or _range_summary(low, high)
    return {
        "key": field.key,
        "label": "Players",
        "summary": summary,
        "active": summary != "Any",
        "control": "players",
        "min": _input_value(flt.min if flt else None),
        "max": _input_value(flt.max if flt else None),
        "chips": [
            {"value": str(count), "label": str(count)}
            for count in range(1, PLAYER_CHIPS)
        ]
        + [{"value": str(PLAYER_CHIPS), "label": f"{PLAYER_CHIPS}+"}],
    }


def _names_summary(names):
    """Return the chosen names as words, e.g. "Drama, Crime" or "Drama +2".

    The client builds the same words from the checked options (libraryFilters.js).
    """
    if len(names) > MAX_NAMED:
        return f"{names[0]} +{len(names) - 1}"
    return ", ".join(names) or "Any"


def _choice_row(field, flt, choices, present):
    """Return a choice field's row for the panel, or None without options."""
    asked = {value.lower() for value in flt.values} if flt else set()
    if field.choices:
        if field.key not in present and not asked:
            return None
        options = [
            {"value": value, "label": label, "count": None}
            for value, label in field.choices
        ]
    else:
        options = [
            dict(option, label=option["value"]) for option in choices.get(field.key, [])
        ]
        known = {option["value"].lower() for option in options}
        options += [
            {"value": value, "label": value, "count": 0}
            for value in (flt.values if flt else ())
            if value.lower() not in known
        ]
        if not options:
            return None
    for option in options:
        option["checked"] = option["value"].lower() in asked
    names = [option["label"] for option in options if option["checked"]]
    return {
        "key": field.key,
        "label": field.label,
        "options": options,
        "summary": _names_summary(names),
        "active": bool(names),
    }


def panel_filters(media_type, filters, choices, bounds, present):
    """Return the filters the panel offers, in groups with their current values.

    Each group has a `title`, its `ranges` and its `choices`. Every range and
    choice has the `summary` its row shows for its current value ("Any" for
    none) and whether it is `active`. A range is a field's control: a `slider`
    for numbers and years within the library's `bounds` and fields about the
    title with values in `present` (both from `range_bounds`), preset chips with
    date inputs for `dates`, or chips for `players`. A choice is a field of
    toggles, each option with its `value`, `label`, `count` and whether it is
    `checked`. Groups left empty are dropped. Genres and platforms are those of
    the user's library, plus any asked for that it lacks, so no filter in the
    address is lost.
    """
    groups = {
        "activity": {"title": "Your activity", "ranges": [], "choices": []},
        "about": {"title": "About the title", "ranges": [], "choices": []},
        "ratings": {"title": "Ratings", "ranges": [], "choices": []},
    }
    for field in get_filterable_fields(media_type):
        flt = filters.get(field.key)
        if field.kind not in _PARSERS:
            if row := _choice_row(field, flt, choices, present):
                groups["about"]["choices"].append(row)
            continue
        if field.kind == DATE:
            control = _date_presets(field, flt)
        elif field.key == MediaSortChoices.PLAYERS:
            control = _player_chips(field, flt) if field.key in present or flt else None
        else:
            control = _slider(field, flt, bounds)
        if control:
            if field.key in ACTIVITY_KEYS:
                group = "activity"
            elif field.key in RATING_KEYS:
                group = "ratings"
            else:
                group = "about"
            groups[group]["ranges"].append(control)
    return [group for group in groups.values() if group["ranges"] or group["choices"]]


def list_text(field, media, format_datetime):
    """Return a field's value in a listed media as text, e.g. "2h 5m", or "".

    The value is read without queries from the media's item, its annotations or
    what it has gone through. Dates of the entry go through `format_datetime`.
    A field with no column of its own, such as the title, has none.
    """
    key = field.key
    if key == MediaSortChoices.RECENT:
        value = media.progressed_at or media.created_at
    elif key == MediaSortChoices.PLAYS:
        shown = media.item.media_type in SHOWS
        value = media.plays if shown else media.completed_entries
    elif key == MediaSortChoices.AIRING_STATUS:
        value = media.item.airing_status
    elif key == MediaSortChoices.PLAYERS:
        low, high = media.item.min_players, media.item.max_players
        if low and high and low != high:
            return f"{low}\u2013{high} {field.unit}"
        value = high
    else:
        expression = field.sort(media.item.media_type)
        name = expression.name if isinstance(expression, F) else key
        if name.startswith("item__"):
            value = getattr(media.item, name.removeprefix("item__"))
        else:
            value = getattr(media, name, None)
    if isinstance(value, datetime):
        return format_datetime(value) or ""
    return field.format(value)
