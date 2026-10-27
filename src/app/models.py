import logging
import uuid
from collections import Counter

from django.apps import apps
from django.conf import settings
from django.core.validators import (
    DecimalValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.db.models import (
    CheckConstraint,
    Count,
    F,
    Max,
    Prefetch,
    Q,
    UniqueConstraint,
    Window,
)
from django.db.models.functions import FirstValue, RowNumber
from django.utils import timezone
from model_utils import FieldTracker
from model_utils.fields import MonitorField
from simple_history.models import HistoricalRecords
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

import app
import events
import users
from app import providers
from app.mixins import CalendarTriggerMixin

logger = logging.getLogger(__name__)


class Sources(models.TextChoices):
    """Choices for the source of the item."""

    TMDB = "tmdb", "The Movie Database"
    MAL = "mal", "MyAnimeList"
    MANGAUPDATES = "mangaupdates", "MangaUpdates"
    IGDB = "igdb", "Internet Game Database"
    OPENLIBRARY = "openlibrary", "Open Library"
    HARDCOVER = "hardcover", "Hardcover"
    COMICVINE = "comicvine", "Comic Vine"
    BGG = "bgg", "BoardGameGeek"
    MANUAL = "manual", "Manual"


class MediaTypes(models.TextChoices):
    """Choices for the media type of the item."""

    TV = "tv", "TV Show"
    SEASON = "season", "TV Season"
    EPISODE = "episode", "Episode"
    MOVIE = "movie", "Movie"
    ANIME = "anime", "Anime"
    MANGA = "manga", "Manga"
    GAME = "game", "Game"
    BOOK = "book", "Book"
    COMIC = "comic", "Comic"
    BOARDGAME = "boardgame", "Boardgame"


class AiringStatus(models.TextChoices):
    """Choices for whether a title is still coming out."""

    UPCOMING = "upcoming", "Upcoming"
    AIRING = "airing", "Airing"
    ENDED = "ended", "Ended"


class Item(CalendarTriggerMixin, models.Model):
    """Model to store basic information about media items."""

    # limited by uuid for manual entries
    media_id = models.CharField(max_length=36)
    source = models.CharField(
        max_length=20,
        choices=Sources,
    )
    media_type = models.CharField(
        max_length=10,
        choices=MediaTypes,
        default=MediaTypes.MOVIE.value,
    )
    title = models.TextField()
    image = models.URLField()  # if add default, custom media entry will show the value
    season_number = models.PositiveIntegerField(null=True, blank=True)
    episode_number = models.PositiveIntegerField(null=True, blank=True)

    # Details from the provider, kept to sort and filter libraries. Null or
    # empty when unknown. See app.item_details
    release_date = models.DateField(null=True, blank=True)
    # Minutes: a movie, an episode or a board game's play time
    runtime = models.PositiveIntegerField(null=True, blank=True)
    # Episodes, chapters, pages or issues
    length = models.PositiveIntegerField(null=True, blank=True)
    season_count = models.PositiveIntegerField(null=True, blank=True)
    provider_score = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        null=True,
        blank=True,
    )
    provider_score_count = models.PositiveIntegerField(null=True, blank=True)
    imdb_score = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        null=True,
        blank=True,
    )
    rotten_tomatoes = models.PositiveSmallIntegerField(null=True, blank=True)
    metacritic = models.PositiveSmallIntegerField(null=True, blank=True)
    airing_status = models.CharField(
        max_length=10,
        choices=AiringStatus,
        blank=True,
        default="",
    )
    # Minutes
    time_to_beat = models.PositiveIntegerField(null=True, blank=True)
    min_players = models.PositiveSmallIntegerField(null=True, blank=True)
    max_players = models.PositiveSmallIntegerField(null=True, blank=True)
    # "|Drama|Comedy|" so a genre is matched as "|Drama|"
    genres = models.TextField(blank=True, default="")
    platforms = models.TextField(blank=True, default="")
    details_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        constraints = [
            # Ensures items without season/episode numbers are unique
            UniqueConstraint(
                fields=["media_id", "source", "media_type"],
                condition=Q(season_number__isnull=True, episode_number__isnull=True),
                name="unique_item_without_season_episode",
            ),
            # Ensures seasons are unique within a show
            UniqueConstraint(
                fields=["media_id", "source", "media_type", "season_number"],
                condition=Q(season_number__isnull=False, episode_number__isnull=True),
                name="unique_item_with_season",
            ),
            # Ensures episodes are unique within a season
            UniqueConstraint(
                fields=[
                    "media_id",
                    "source",
                    "media_type",
                    "season_number",
                    "episode_number",
                ],
                condition=Q(season_number__isnull=False, episode_number__isnull=False),
                name="unique_item_with_season_episode",
            ),
            # Enforces that season items must have a season number but no episode number
            CheckConstraint(
                condition=Q(
                    media_type=MediaTypes.SEASON.value,
                    season_number__isnull=False,
                    episode_number__isnull=True,
                )
                | ~Q(media_type=MediaTypes.SEASON.value),
                name="season_number_required_for_season",
            ),
            # Enforces that episode items must have both season and episode numbers
            CheckConstraint(
                condition=Q(
                    media_type=MediaTypes.EPISODE.value,
                    season_number__isnull=False,
                    episode_number__isnull=False,
                )
                | ~Q(media_type=MediaTypes.EPISODE.value),
                name="season_and_episode_required_for_episode",
            ),
            # Prevents season/episode numbers from being set on non-TV media types
            CheckConstraint(
                condition=Q(
                    ~Q(
                        media_type__in=[
                            MediaTypes.SEASON.value,
                            MediaTypes.EPISODE.value,
                        ],
                    ),
                    season_number__isnull=True,
                    episode_number__isnull=True,
                )
                | Q(media_type__in=[MediaTypes.SEASON.value, MediaTypes.EPISODE.value]),
                name="no_season_episode_for_other_types",
            ),
            # Validate source choices
            CheckConstraint(
                condition=Q(source__in=Sources.values),
                name="%(app_label)s_%(class)s_source_valid",
            ),
            # Validate media_type choices
            CheckConstraint(
                condition=Q(media_type__in=MediaTypes.values),
                name="%(app_label)s_%(class)s_media_type_valid",
            ),
        ]
        ordering = ["media_id"]

    def __str__(self):
        """Return the name of the item."""
        name = self.title
        if self.season_number is not None:
            name += f" S{self.season_number}"
            if self.episode_number is not None:
                name += f"E{self.episode_number}"
        return name

    @classmethod
    def generate_manual_id(cls):
        """Generate a new ID for manual items.

        Uses a UUID to ensure uniqueness.
        """
        return str(uuid.uuid4())

    def fetch_releases(self, delay):
        """Fetch releases for the item."""
        if self._disable_calendar_triggers:
            return

        if self.media_type == MediaTypes.SEASON.value:
            # Get or create the TV item for this season
            try:
                tv_item = Item.objects.get(
                    media_id=self.media_id,
                    source=self.source,
                    media_type=MediaTypes.TV.value,
                )
            except Item.DoesNotExist:
                # Get metadata for the TV show
                tv_metadata = providers.services.get_media_metadata(
                    MediaTypes.TV.value,
                    self.media_id,
                    self.source,
                )
                tv_item = Item.objects.create(
                    media_id=self.media_id,
                    source=self.source,
                    media_type=MediaTypes.TV.value,
                    title=tv_metadata["title"],
                    image=tv_metadata["image"],
                )
                logger.info("Created TV item %s for season %s", tv_item, self)

            # Process the TV item instead of the season
            items_to_process = [tv_item]
        else:
            items_to_process = [self]

        if delay:
            events.tasks.reload_calendar.delay(items_to_process=items_to_process)
        else:
            events.tasks.reload_calendar(items_to_process=items_to_process)


def is_caught_up(media):
    """Return whether media has nothing released left to go through, only more to come.

    Takes media annotated with its `max_progress` and `next_event`, like a show
    airing weekly whose latest episode is watched.
    """
    next_event = getattr(media, "next_event", None)
    if not next_event or next_event.is_max_datetime:
        return False
    max_progress = getattr(media, "max_progress", None)
    return max_progress is None or media.progress >= max_progress


def _seasons_of_watched_shows(seasons):
    """Return the seasons whose shows have another season finished.

    Those of shows not started yet are left out.
    """
    if not seasons:
        return []
    watched_show_ids = set(
        Season.objects.filter(
            related_tv_id__in={season.related_tv_id for season in seasons},
            status__in=(Status.COMPLETED.value, *REPEAT_STATUSES),
        ).values_list("related_tv_id", flat=True),
    )
    return [season for season in seasons if season.related_tv_id in watched_show_ids]


def _first_season_of_each_show(seasons):
    """Return the lowest numbered of each show's seasons, specials last."""
    first = {}
    for season in seasons:
        order = (season.item.season_number == 0, season.item.season_number)
        current = first.get(season.related_tv_id)
        if current is None or order < current[0]:
            first[season.related_tv_id] = (order, season)
    return [season for _, season in first.values()]


class MediaManager(models.Manager):
    """Custom manager for media models."""

    def get_historical_models(self):
        """Return list of historical model names."""
        return [f"historical{media_type}" for media_type in MediaTypes.values]

    def get_media_list(  # noqa: PLR0913
        self,
        user,
        media_type,
        status_filter,
        sort_filter,
        search=None,
        *,
        sort_order=None,
        filters=None,
        exclude_caught_up=True,
    ):
        """Get media list based on filters, sorting and search.

        Each media is listed by its latest entry. Filtered to in progress or
        repeating, media caught up to its latest release is left out unless
        `exclude_caught_up` is False, as it waits for the next, and filtered to
        caught up, it's all that's listed. `filters` are the library's filters by
        field key, as `library_fields.parse_filters` returns them, applied to
        each media's latest entry. The sort goes in `sort_order`, else its usual
        direction.
        """
        model = apps.get_model(app_label="app", model_name=media_type)
        queryset = model.objects.filter(user=user.id)

        if search:
            search_filter = Q(item__title__icontains=search)
            if any(field.name == "notes" for field in model._meta.get_fields()):
                search_filter |= Q(notes__icontains=search)
            queryset = queryset.filter(search_filter)

        caught_up = status_filter == users.models.LibraryStatusChoices.CAUGHT_UP
        statuses = None
        if caught_up:
            statuses = (Status.IN_PROGRESS.value, Status.REPEATING.value)
        elif status_filter and status_filter != users.models.MediaStatusChoices.ALL:
            statuses = (status_filter,)

        if statuses:
            # Only items with an entry in that status, so the windows scan fewer rows
            queryset = queryset.filter(
                item__in=model.objects.filter(
                    user=user.id,
                    status__in=statuses,
                ).values("item"),
            )

        queryset = self._filter_latest_entries(
            queryset,
            media_type,
            statuses,
            filters,
        )

        queryset = queryset.select_related("item")
        queryset = self._apply_prefetch_related(queryset, media_type)

        if sort_filter:
            queryset = self._sort_media_list(
                queryset,
                sort_filter,
                media_type,
                sort_order,
            )

        media_list = queryset

        # Media caught up to its latest release isn't listed as being gone
        # through, so it's a list of what is instead of a queryset
        if caught_up:
            # Only media with a release to come can be caught up, so the rest
            # isn't loaded; whole items are kept, so the windows are unchanged
            upcoming = events.models.Event.objects.filter(datetime__gt=timezone.now())
            if media_type == MediaTypes.TV.value:
                media_list = media_list.filter(
                    item__media_id__in=upcoming.filter(
                        item__media_type=MediaTypes.SEASON.value,
                    ).values("item__media_id"),
                )
            else:
                media_list = media_list.filter(item__in=upcoming.values("item"))
            return self.only_caught_up(media_list, media_type)
        if exclude_caught_up and status_filter in (
            Status.IN_PROGRESS.value,
            Status.REPEATING.value,
        ):
            return self.without_caught_up(media_list, media_type)
        return media_list

    def _filter_latest_entries(self, queryset, media_type, statuses, filters):
        """Return the latest entry of each media whose entries match the filters.

        Media is filtered by its latest entry's status and the library's
        `filters`, and annotated with its number of entries as `repeats`, when it
        was first added as `added_at` and its number of completed entries as
        `completed_entries`.
        """
        latest = {"partition_by": [F("item")], "order_by": F("created_at").desc()}
        queryset = queryset.annotate(
            repeats=Window(expression=Count("id"), partition_by=[F("item")]),
            added_at=Window(
                expression=models.Min("created_at"),
                partition_by=[F("item")],
            ),
            # Windowed with the others, as the latest entries are picked after
            completed_entries=Window(
                expression=models.Sum(
                    models.Case(
                        models.When(status=Status.COMPLETED.value, then=1),
                        default=0,
                    ),
                ),
                partition_by=[F("item")],
            ),
            row_number=Window(expression=RowNumber(), **latest),
        ).filter(row_number=1)

        # Filtered in windows, as filters outside one leave out the entries it
        # picks the latest from, e.g. so a repeat in progress isn't also listed
        # as completed by its earlier play
        if statuses:
            queryset = queryset.annotate(
                latest_status=Window(expression=FirstValue("status"), **latest),
            ).filter(latest_status__in=statuses)
        for flt in (filters or {}).values():
            queryset = flt.field.filter(queryset, media_type, flt)
        return queryset

    def get_status_counts(self, user, media_type, search=None, filters=None):
        """Return how many media of a type the list has in each status.

        Takes the list's search and filters. Media caught
        up to its latest release counts as caught up instead of in progress or
        repeating, and "All" counts every media.
        """
        all_status = users.models.MediaStatusChoices.ALL
        media_list = self.get_media_list(
            user,
            media_type,
            all_status,
            None,
            search,
            filters=filters,
        )
        if isinstance(media_list, list):
            statuses = [media.status for media in media_list]
        else:
            statuses = media_list.prefetch_related(None).values_list(
                "status",
                flat=True,
            )
        counts = Counter(statuses)
        counts[all_status] = sum(counts.values())

        if counts[Status.IN_PROGRESS.value] or counts[Status.REPEATING.value]:
            for media in self.get_media_list(
                user,
                media_type,
                users.models.LibraryStatusChoices.CAUGHT_UP,
                None,
                search,
                filters=filters,
            ):
                counts[media.status] -= 1
                counts[users.models.LibraryStatusChoices.CAUGHT_UP] += 1
        return counts

    def _apply_prefetch_related(self, queryset, media_type):
        """Apply appropriate prefetch_related based on media type."""
        # Apply media-specific prefetches
        if media_type == MediaTypes.TV.value:
            return queryset.prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.select_related("item"),
                ),
                Prefetch(
                    "seasons__episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            )

        base_queryset = queryset.prefetch_related(
            Prefetch(
                "item__event_set",
                queryset=events.models.Event.objects.all(),
                to_attr="prefetched_events",
            ),
        )

        if media_type == MediaTypes.SEASON.value:
            return base_queryset.prefetch_related(
                Prefetch(
                    "episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            )

        return base_queryset

    def _sort_media_list(self, queryset, sort_filter, media_type, sort_order=None):
        """Sort a media list by a sort's value, in `sort_order` or its usual direction.

        A sort the media type doesn't have is its default sort. Media without
        the value comes last either way, and ties go by title.
        """
        # Imported here as the fields use this module's models
        from app import library_fields  # noqa: PLC0415

        field = library_fields.get_sort_field(media_type, sort_filter)
        if sort_order is None:
            sort_order = field.default_order
        sort_value = field.sort(media_type)
        # A show's aggregate is annotated once, as the filter did if it has one
        if (
            getattr(sort_value, "contains_aggregate", False)
            and f"filter_{field.key}" in queryset.query.annotations
        ):
            sort_value = F(f"filter_{field.key}")
        # Aggregates are ordered by from an annotation, as they group the rows
        if getattr(sort_value, "contains_aggregate", False):
            queryset = queryset.annotate(sort_value=sort_value)
            sort_value = F("sort_value")
        ordering = (
            sort_value.asc(nulls_last=True)
            if sort_order == users.models.SortOrderChoices.ASC
            else sort_value.desc(nulls_last=True)
        )
        return queryset.order_by(ordering, models.functions.Lower("item__title"))

    def get_home_status(
        self,
        user,
        status,
        sort_by,
        items_limit=None,
        specific_media_type=None,
        *,
        caught_up=False,
        loaded_lists=None,
    ):
        """Get a home media list for a specific status grouped by media type.

        With `caught_up`, it's the media in progress or repeated that's caught
        up to its latest release instead. Planned seasons, for the first time or
        again, are only each show's first, and seasons count their episodes as
        `episode_count`. `loaded_lists` is a dict to keep each status's media in,
        so the rows of one page load them once.
        """
        list_by_type = {}
        media_types = self._get_media_types_to_process(user, specific_media_type)
        if loaded_lists is None:
            loaded_lists = {}

        for media_type in media_types:
            media_list = self._get_home_media(
                user,
                media_type,
                status,
                caught_up,
                loaded_lists,
            )
            if not media_list:
                continue

            # Sort the media list
            sorted_list = self._sort_home_media(media_list, sort_by)

            # Apply pagination
            total_count = len(sorted_list)
            if items_limit is None:
                paginated_list = sorted_list
            elif specific_media_type:
                paginated_list = sorted_list[items_limit:]
            else:
                paginated_list = sorted_list[:items_limit]

            list_by_type[media_type] = {
                "items": paginated_list,
                "total": total_count,
            }

        return list_by_type

    def _get_home_media(self, user, media_type, status, caught_up, loaded_lists):
        """Return a home row's media of a type, annotated for its cards.

        Caught up media is all that's left of what's in progress or repeated,
        and of finished shows, their planned next seasons still to air. Each
        status's media is loaded into `loaded_lists` once.
        """
        statuses = (status,)
        if caught_up:
            statuses = (Status.IN_PROGRESS.value, Status.REPEATING.value)
            if media_type == MediaTypes.SEASON.value:
                statuses += (Status.PLANNING.value,)
        media_list = []
        for row_status in statuses:
            key = (media_type, row_status)
            if key not in loaded_lists:
                loaded = list(
                    self.get_media_list(
                        user=user,
                        media_type=media_type,
                        status_filter=row_status,
                        sort_filter=None,
                        exclude_caught_up=False,
                    ),
                )
                self.annotate_max_progress(loaded, media_type)
                self._annotate_next_event(loaded)
                loaded_lists[key] = loaded
            media_list.extend(loaded_lists[key])

        if media_type == MediaTypes.SEASON.value:
            return self._home_seasons(media_list, status, caught_up=caught_up)
        if caught_up:
            return [media for media in media_list if is_caught_up(media)]
        if status in (Status.IN_PROGRESS.value, Status.REPEATING.value):
            return [media for media in media_list if not is_caught_up(media)]
        return media_list

    def _home_seasons(self, seasons, status, *, caught_up):
        """Return a home row's seasons, each counting its episodes.

        A finished show's planned season still to air is caught up rather than
        planned, and the planning rows have each show's first planned season.
        """
        waiting = set(
            _seasons_of_watched_shows(
                [
                    season
                    for season in seasons
                    if season.status == Status.PLANNING.value and is_caught_up(season)
                ],
            ),
        )
        if caught_up:
            seasons = [
                season
                for season in seasons
                if season in waiting
                or (season.status != Status.PLANNING.value and is_caught_up(season))
            ]
        elif status == Status.PLANNING.value:
            seasons = _first_season_of_each_show(
                [season for season in seasons if season not in waiting],
            )
        elif status == Status.PLANNING_REPEAT.value:
            seasons = _first_season_of_each_show(seasons)
        else:
            seasons = [season for season in seasons if not is_caught_up(season)]

        for season in seasons:
            # Announced episodes too, which release events are made for
            episodes = [
                event.content_number
                for event in getattr(season.item, "prefetched_events", [])
                if event.content_number
            ]
            season.episode_count = max(episodes) if episodes else None
        return seasons

    def _get_media_types_to_process(self, user, specific_media_type):
        """Determine which media types to process based on user settings."""
        if specific_media_type:
            return [specific_media_type]

        # Get active types excluding TV
        return [
            media_type
            for media_type in user.get_active_media_types()
            if media_type != MediaTypes.TV.value
        ]

    def without_caught_up(self, media_list, media_type):
        """Return media from a list that isn't caught up to its latest release."""
        media_list = self.annotate_releases(media_list, media_type)
        return [media for media in media_list if not is_caught_up(media)]

    def only_caught_up(self, media_list, media_type):
        """Return media from a list that's caught up to its latest release."""
        media_list = self.annotate_releases(media_list, media_type)
        return [media for media in media_list if is_caught_up(media)]

    def annotate_releases(self, media_list, media_type):
        """Return a list of the media annotated with how much is out and what's next."""
        media_list = list(media_list)
        self.annotate_max_progress(media_list, media_type)
        # Shows get their next releases from their seasons with max_progress
        if media_type != MediaTypes.TV.value:
            self._annotate_next_event(media_list)
        return media_list

    def _annotate_next_event(self, media_list):
        """Annotate next_event for media items."""
        current_time = timezone.now()

        for media in media_list:
            # Get future events sorted by datetime
            future_events = sorted(
                [
                    event
                    for event in getattr(media.item, "prefetched_events", [])
                    if event.datetime > current_time
                ],
                key=lambda e: e.datetime,
            )

            media.next_event = future_events[0] if future_events else None

    def _sort_home_media(self, media_list, sort_by):
        """Sort home media based on the selected sort criteria."""
        # Define primary sort functions based on sort_by
        primary_sort_functions = {
            users.models.HomeSortChoices.UPCOMING: lambda x: (
                x.next_event is None,
                x.next_event.datetime if x.next_event else None,
            ),
            users.models.HomeSortChoices.RECENT: lambda x: (
                -timezone.datetime.timestamp(
                    x.progressed_at if x.progressed_at is not None else x.created_at,
                )
            ),
            users.models.HomeSortChoices.COMPLETION: lambda x: (
                x.max_progress is None,
                -(
                    x.progress / x.max_progress * 100
                    if x.max_progress and x.max_progress > 0
                    else 0
                ),
            ),
            users.models.HomeSortChoices.EPISODES_LEFT: lambda x: (
                x.max_progress is None,
                (x.max_progress - x.progress if x.max_progress else 0),
            ),
            users.models.HomeSortChoices.TITLE: lambda x: x.item.title.lower(),
        }

        primary_sort_function = primary_sort_functions[sort_by]

        return sorted(
            media_list,
            key=lambda x: (
                primary_sort_function(x),
                -timezone.datetime.timestamp(
                    x.progressed_at if x.progressed_at is not None else x.created_at,
                ),
                x.item.title.lower(),
            ),
        )

    def annotate_max_progress(self, media_list, media_type):
        """Annotate max_progress for all media items."""
        current_datetime = timezone.now()

        if media_type == MediaTypes.MOVIE.value:
            for media in media_list:
                media.max_progress = 1
            return

        if media_type == MediaTypes.TV.value:
            self._annotate_tv_released_episodes(media_list, current_datetime)
            return

        # For other media types, calculate max_progress from events
        # Create a dictionary mapping item_id to max content_number
        max_progress_dict = {}

        item_ids = [media.item.id for media in media_list]

        # Fetch all relevant events in a single query
        events_data = events.models.Event.objects.filter(
            item_id__in=item_ids,
            datetime__lte=current_datetime,
        ).values("item_id", "content_number")

        # Process events to find max content number per item
        for event in events_data:
            item_id = event["item_id"]
            content_number = event["content_number"]
            if content_number is not None:
                current_max = max_progress_dict.get(item_id, 0)
                max_progress_dict[item_id] = max(current_max, content_number)

        for media in media_list:
            media.max_progress = max_progress_dict.get(media.item.id)

    def _annotate_tv_released_episodes(self, tv_list, current_datetime):
        """Annotate TV shows with the number of released episodes."""
        # Prefetch all relevant events in one query
        season_events = events.models.Event.objects.filter(
            item__media_id__in=[tv.item.media_id for tv in tv_list],
            item__source=tv_list[0].item.source if tv_list else None,
            item__media_type=MediaTypes.SEASON.value,
            item__season_number__gt=0,
            content_number__isnull=False,
        ).select_related("item")

        # Create a dictionary to store max episode numbers per season per show,
        # and one of each show's next release with a known date
        released_episodes = {}
        next_events = {}

        for event in season_events:
            media_id = event.item.media_id
            if event.datetime > current_datetime:
                upcoming = next_events.get(media_id)
                if not event.is_max_datetime and (
                    upcoming is None or event.datetime < upcoming.datetime
                ):
                    next_events[media_id] = event
                continue
            season_number = event.item.season_number
            episode_number = event.content_number

            if media_id not in released_episodes:
                released_episodes[media_id] = {}

            if (
                season_number not in released_episodes[media_id]
                or episode_number > released_episodes[media_id][season_number]
            ):
                released_episodes[media_id][season_number] = episode_number

        # Calculate total released episodes and seasons per TV show
        for tv in tv_list:
            tv_episodes = released_episodes.get(tv.item.media_id, {})
            tv.max_progress = sum(tv_episodes.values()) if tv_episodes else 0
            tv.released_seasons = len(tv_episodes)
            tv.next_event = next_events.get(tv.item.media_id)

    def fetch_media_for_items(self, media_types, item_ids, user, status_filter=None):
        """Fetch media objects for given items, optionally filtering by status.

        Args:
            media_types: Iterable of media type strings to query
            item_ids: QuerySet or list of item IDs to fetch media for
            user: User to filter media by
            status_filter: Optional status value to filter by

        Returns:
            dict mapping item_id to media object
        """
        media_by_item_id = {}

        for media_type in media_types:
            model = apps.get_model("app", media_type)

            if media_type == MediaTypes.EPISODE.value:
                filter_kwargs = {
                    "item__in": item_ids,
                    "related_season__user": user,
                }
                if status_filter:
                    filter_kwargs["related_season__status"] = status_filter
            else:
                filter_kwargs = {
                    "item__in": item_ids,
                    "user": user,
                }
                if status_filter:
                    filter_kwargs["status"] = status_filter

            queryset = model.objects.filter(**filter_kwargs).select_related("item")
            queryset = self._apply_prefetch_related(queryset, media_type)
            self.annotate_max_progress(queryset, media_type)

            for entry in queryset:
                media_by_item_id.setdefault(entry.item_id, entry)

        return media_by_item_id

    def get_media(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get user media object given the media type and item."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._get_media_params(
            user,
            media_type,
            instance_id,
        )

        return model.objects.get(**params)

    def get_media_prefetch(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get user media object with prefetch_related applied."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._get_media_params(
            user,
            media_type,
            instance_id,
        )

        queryset = model.objects.filter(**params)

        queryset = self._apply_prefetch_related(queryset, media_type)
        media = queryset.get()
        self.annotate_max_progress([media], media_type)

        return media

    def _get_media_params(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get the common filter parameters for media queries."""
        params = {"id": instance_id}

        if media_type == MediaTypes.EPISODE.value:
            params["related_season__user"] = user
        else:
            params["user"] = user

        return params

    def filter_media(
        self,
        user,
        media_id,
        media_type,
        source,
        season_number=None,
        episode_number=None,
    ):
        """Filter media objects based on parameters."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._filter_media_params(
            media_type,
            media_id,
            source,
            user,
            season_number,
            episode_number,
        )

        return model.objects.filter(**params)

    def filter_media_prefetch(
        self,
        user,
        media_id,
        media_type,
        source,
        season_number=None,
        episode_number=None,
    ):
        """Filter user media object with prefetch_related applied."""
        queryset = self.filter_media(
            user,
            media_id,
            media_type,
            source,
            season_number,
            episode_number,
        ).select_related("item")
        queryset = self._apply_prefetch_related(queryset, media_type)
        self.annotate_max_progress(queryset, media_type)

        return queryset

    def _filter_media_params(
        self,
        media_type,
        media_id,
        source,
        user,
        season_number=None,
        episode_number=None,
    ):
        """Get the common filter parameters for media queries."""
        params = {
            "item__media_type": media_type,
            "item__source": source,
            "item__media_id": media_id,
        }

        if media_type == MediaTypes.SEASON.value:
            params["item__season_number"] = season_number
            params["user"] = user
        elif media_type == MediaTypes.EPISODE.value:
            params["item__season_number"] = season_number
            params["item__episode_number"] = episode_number
            params["related_season__user"] = user
        else:
            params["user"] = user

        return params


class Status(models.TextChoices):
    """Choices for item status."""

    COMPLETED = "Completed", "Completed"
    IN_PROGRESS = "In progress", "In Progress"
    PLANNING = "Planning", "Planning"
    # Going through finished media again, which keeps the earlier plays. Shown
    # in each media type's words where it's known, e.g. Rereading for books.
    REPEATING = "Repeating", "Repeating"
    PLANNING_REPEAT = "Planning repeat", "Planning Repeat"
    PAUSED = "Paused", "Paused"
    DROPPED = "Dropped", "Dropped"


# The statuses of media being gone through again after it was finished
REPEAT_STATUSES = (Status.PLANNING_REPEAT.value, Status.REPEATING.value)

# An episode's entry is a watch, one play of it, when it's completed. The
# episode has been watched when it has an entry in any of these, as one planned
# or being watched again was watched before.
WATCH_STATUS = Status.COMPLETED.value
WATCHED_STATUSES = (WATCH_STATUS, *REPEAT_STATUSES)

# The statuses of an episode's entries for a first watch not made yet. Entries
# in REPEAT_STATUSES are for watching an episode watched before again.
UNFINISHED_STATUSES = (
    Status.PLANNING.value,
    Status.IN_PROGRESS.value,
    Status.PAUSED.value,
    Status.DROPPED.value,
)

# The order a list sorted by status starts in
STATUS_SORT_ORDER = (
    Status.IN_PROGRESS.value,
    Status.REPEATING.value,
    Status.PLANNING.value,
    Status.PLANNING_REPEAT.value,
    Status.PAUSED.value,
    Status.COMPLETED.value,
    Status.DROPPED.value,
)


class UserMessageLevel(models.TextChoices):
    """Choices for persistent user messages."""

    SUCCESS = "success", "Success"
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"
    INFO = "info", "Info"


class UserMessage(models.Model):
    """Persistent user notification shown in the toast UI."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    level = models.CharField(
        max_length=20,
        choices=UserMessageLevel,
        default=UserMessageLevel.INFO,
    )
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    shown_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["user", "shown_at"],
                name="app_umsg_user_shown_idx",
            ),
        ]

    def __str__(self):
        """Return the message text."""
        return self.message

    @property
    def tags(self):
        """Return a Django-messages-compatible level tag."""
        return self.level


class Media(models.Model):
    """Abstract model for all media types."""

    history = HistoricalRecords(
        cascade_delete_history=True,
        inherit=True,
        excluded_fields=[
            "item",
            "progressed_at",
            "user",
            "related_tv",
            "created_at",
        ],
    )

    created_at = models.DateTimeField(auto_now_add=True)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    score = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(0),
            MaxValueValidator(10),
        ],
    )
    progress = models.PositiveIntegerField(default=0)
    progressed_at = MonitorField(monitor="progress")
    status = models.CharField(
        max_length=20,
        choices=Status,
        default=Status.COMPLETED.value,
    )
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        """Meta options for the model."""

        abstract = True
        ordering = ["user", "item", "-created_at"]

    def __str__(self):
        """Return the title of the media."""
        return self.item.__str__()

    def save(self, *args, **kwargs):
        """Save the media instance."""
        if self.tracker.has_changed("progress"):
            self.process_progress()

        if self.tracker.has_changed("status"):
            self.process_status()

        super().save(*args, **kwargs)

    def create_user_message(self, message, level):
        """Create a persistent user notification."""
        message_context = str(self)
        if message_context and not message.startswith(message_context):
            message = f"{message_context} {message}"

        logger.info("Creating user message for %s: %s", self.user, message)

        UserMessage.objects.create(
            user=self.user,
            level=level,
            message=message,
        )

    def process_progress(self):
        """Update fields depending on the progress of the media."""
        if self.progress < 0:
            self.progress = 0
        elif self.status in (Status.IN_PROGRESS.value, Status.REPEATING.value):
            max_progress = providers.services.get_media_metadata(
                self.item.media_type,
                self.item.media_id,
                self.item.source,
            )["max_progress"]

            if max_progress:
                self.progress = min(self.progress, max_progress)

                if self.progress == max_progress:
                    self.status = Status.COMPLETED.value

                    now = timezone.now().replace(second=0, microsecond=0)
                    self.end_date = now

    def process_status(self):
        """Update fields depending on the status of the media."""
        if self.status == Status.COMPLETED.value:
            max_progress = providers.services.get_media_metadata(
                self.item.media_type,
                self.item.media_id,
                self.item.source,
            )["max_progress"]

            if max_progress:
                self.progress = max_progress

        self.item.fetch_releases(delay=True)

    @property
    def formatted_score(self):
        """Return as int if score is 10.0 or 0.0, otherwise show decimal."""
        if self.score is not None:
            max_score = 10
            min_score = 0
            if self.score in (max_score, min_score):
                return int(self.score)
            return self.score
        return None

    @property
    def formatted_progress(self):
        """Return the progress of the media in a formatted string."""
        return str(self.progress)

    def increase_progress(self):
        """Increase the progress of the media by one."""
        self.progress += 1
        self.save()
        logger.info("Incresed progress of %s to %s", self, self.progress)

    def decrease_progress(self):
        """Decrease the progress of the media by one."""
        self.progress -= 1
        self.save()
        logger.info("Decreased progress of %s to %s", self, self.progress)


class BasicMedia(Media):
    """Model for basic media types."""

    objects = MediaManager()


class TV(Media):
    """Model for TV shows."""

    tracker = FieldTracker()

    class Meta:
        """Meta options for the model."""

        ordering = ["user", "item"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "item"],
                name="%(app_label)s_%(class)s_unique_item_user",
            ),
        ]

    @tracker  # postpone field reset until after the save
    def save(self, *args, **kwargs):
        """Save the media instance."""
        super(Media, self).save(*args, **kwargs)

        if self.tracker.has_changed("status"):
            previous_status = self.tracker.previous("status")
            if previous_status in REPEAT_STATUSES and self.status not in (
                Status.COMPLETED.value,
                *REPEAT_STATUSES,
            ):
                self._cancel_repeat()

            if (
                self.status == Status.COMPLETED.value
                and previous_status in REPEAT_STATUSES
            ):
                self._complete_repeat()

            elif self.status == Status.COMPLETED.value:
                self._completed()

            elif self.status == Status.PLANNING_REPEAT.value:
                self._plan_repeat()

            elif (
                self.status == Status.REPEATING.value
                and not self.seasons.filter(status__in=REPEAT_STATUSES).exists()
            ):
                self._plan_repeat()
                self._start_next_repeat_season()

            elif self.status == Status.DROPPED.value:
                self._mark_in_progress_seasons_as_dropped()

            elif (
                self.status == Status.IN_PROGRESS.value
                and not self.seasons.filter(status=Status.IN_PROGRESS.value).exists()
            ):
                self._start_next_available_season()

            self.item.fetch_releases(delay=True)

    @property
    def progress(self):
        """Return the total episodes watched for the TV show."""
        return sum(
            season.progress
            for season in self.seasons.all()
            if season.item.season_number != 0
        )

    @property
    def plays(self):
        """Return how many times the show was watched through, specials aside.

        Seasons not watched yet, like a planned new one, don't count.
        """
        plays = [
            season.plays
            for season in self.seasons.all()
            if season.item.season_number != 0 and season.watches
        ]
        return min(plays) if plays else 0

    @property
    def last_watched(self):
        """Return the latest watched episode in SxxExx format."""
        watched_episodes = [
            {
                "season": season.item.season_number,
                "episode": episode.item.episode_number,
                "end_date": episode.end_date,
            }
            for season in self.seasons.all()
            if season.item.season_number != 0
            for episode in season.watches
            if episode.end_date is not None
        ]

        if not watched_episodes:
            return ""

        latest_episode = max(
            watched_episodes,
            key=lambda x: (x["end_date"], x["season"], x["episode"]),
        )

        return f"S{latest_episode['season']:02d}E{latest_episode['episode']:02d}"

    @property
    def progressed_at(self):
        """Return the date when the last episode was watched."""
        dates = [
            season.progressed_at
            for season in self.seasons.all()
            if season.progressed_at and season.item.season_number != 0
        ]
        return max(dates) if dates else None

    @property
    def start_date(self):
        """Return the date of the first episode watched."""
        dates = [
            season.start_date
            for season in self.seasons.all()
            if season.start_date and season.item.season_number != 0
        ]
        return min(dates) if dates else None

    @property
    def end_date(self):
        """Return the date of the last episode watched."""
        dates = [
            season.end_date
            for season in self.seasons.all()
            if season.end_date and season.item.season_number != 0
        ]
        return max(dates) if dates else None

    def _completed(self):
        """Create remaining seasons and episodes for a TV show."""
        tv_metadata = providers.services.get_media_metadata(
            self.item.media_type,
            self.item.media_id,
            self.item.source,
        )
        max_progress = tv_metadata["max_progress"]

        if not max_progress or self.progress > max_progress:
            return

        seasons_to_create = []
        seasons_to_update = []
        episodes_to_create = []
        current_date = timezone.localdate()
        tv_completed = True
        # What the seasons not finished are left as
        remaining_statuses = set()
        previous_statuses = {}

        season_numbers = [
            season["season_number"]
            for season in tv_metadata["related"]["seasons"]
            if season["season_number"] != 0
        ]
        tv_with_seasons_metadata = providers.services.get_media_metadata(
            "tv_with_seasons",
            self.item.media_id,
            self.item.source,
            season_numbers,
        )
        for season_number in season_numbers:
            season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

            item, _ = Item.objects.get_or_create(
                media_id=self.item.media_id,
                source=self.item.source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
                defaults={
                    "title": self.item.title,
                    "image": season_metadata["image"],
                },
            )
            try:
                season_instance = Season.objects.get(
                    item=item,
                    user=self.user,
                )
                target_status = season_instance.get_completion_status(
                    season_metadata,
                    unreleased_only_status=Status.PLANNING.value,
                    current_date=current_date,
                )

                if season_instance.status != target_status:
                    previous_statuses[season_instance.pk] = season_instance.status
                    season_instance.status = target_status
                    seasons_to_update.append(season_instance)

            except Season.DoesNotExist:
                season_instance = Season(
                    item=item,
                    score=None,
                    notes="",
                    related_tv=self,
                    user=self.user,
                )
                target_status = season_instance.get_completion_status(
                    season_metadata,
                    unreleased_only_status=Status.PLANNING.value,
                    current_date=current_date,
                )
                season_instance.status = target_status
                seasons_to_create.append(
                    season_instance,
                )

            if target_status != Status.COMPLETED.value:
                tv_completed = False
                remaining_statuses.add(target_status)

        bulk_create_with_history(seasons_to_create, Season)
        bulk_update_with_history(seasons_to_update, Season, ["status"])
        # Completing the show gives up its seasons' rewatches, as `_cancel_repeat`
        Episode.objects.filter(
            related_season_id__in=[
                pk
                for pk, status in previous_statuses.items()
                if status in REPEAT_STATUSES
            ],
            status__in=REPEAT_STATUSES,
        ).delete()

        for season_instance in seasons_to_create + seasons_to_update:
            season_metadata = tv_with_seasons_metadata[
                f"season/{season_instance.item.season_number}"
            ]
            season_instance.move_entries(previous_statuses.get(season_instance.pk))
            episodes_to_create.extend(
                season_instance.get_remaining_eps(
                    season_metadata,
                    current_date=current_date,
                ),
            )
        save_watches(episodes_to_create)

        if episodes_to_create:
            created_episodes_count = len(episodes_to_create)
            episode_label = "episode" if created_episodes_count == 1 else "episodes"
            self.create_user_message(
                f"had {created_episodes_count} released {episode_label} marked "
                "as watched automatically.",
                level=UserMessageLevel.INFO,
            )

        if remaining_statuses == {Status.PLANNING.value}:
            # Only seasons still to air are left
            self._plan_until_next_season()
        elif not tv_completed:
            self.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )
            self.create_user_message(
                "was left in progress because unreleased episodes or seasons remain.",
                level=UserMessageLevel.WARNING,
            )

    def _plan_until_next_season(self):
        """Plan a show whose released seasons are all watched, for the next one.

        It's started again when the next season's first episode is watched.
        """
        if self.status == Status.PLANNING.value:
            return
        self.status = Status.PLANNING.value
        bulk_update_with_history([self], TV, fields=["status"])
        self.create_user_message(
            "was planned until its next season starts.",
            level=UserMessageLevel.INFO,
        )

    def plan_seasons(self):
        """Add the show's seasons, other than specials, to the watchlist with it.

        Called when the user puts the show on their watchlist. Seasons with
        watched episodes, and seasons the user dropped or finished, are left as
        they are. The episodes of the seasons planned are planned too.
        """
        tv_metadata = providers.services.get_media_metadata(
            self.item.media_type,
            self.item.media_id,
            self.item.source,
        )
        existing_seasons = {
            season.item.season_number: season
            for season in self.seasons.select_related("item").prefetch_related(
                "episodes",
            )
        }
        seasons_to_create = []
        seasons_to_update = []
        previous_statuses = {}

        for season_data in tv_metadata.get("related", {}).get("seasons", []):
            season_number = season_data["season_number"]
            if season_number == 0:
                continue

            season = existing_seasons.get(season_number)
            if season is None:
                item, _ = Item.objects.get_or_create(
                    media_id=self.item.media_id,
                    source=self.item.source,
                    media_type=MediaTypes.SEASON.value,
                    season_number=season_number,
                    defaults={
                        "title": self.item.title,
                        "image": season_data["image"],
                    },
                )
                seasons_to_create.append(
                    Season(
                        item=item,
                        user=self.user,
                        related_tv=self,
                        status=Status.PLANNING.value,
                        score=None,
                        notes="",
                    ),
                )
            elif (
                season.status in (Status.IN_PROGRESS.value, Status.PAUSED.value)
                and not season.watched_entries
            ):
                previous_statuses[season.pk] = season.status
                season.status = Status.PLANNING.value
                seasons_to_update.append(season)

        bulk_create_with_history(seasons_to_create, Season)
        bulk_update_with_history(seasons_to_update, Season, ["status"])
        for season in seasons_to_update:
            season.move_entries(previous_statuses[season.pk])
        # The episodes of its planned seasons are planned with them, from their
        # seasons fetched together
        planned = list(
            self.seasons.filter(
                item__season_number__gt=0,
                status=Status.PLANNING.value,
            ).select_related("item"),
        )
        if planned:
            seasons_metadata = providers.services.get_media_metadata(
                "tv_with_seasons",
                self.item.media_id,
                self.item.source,
                [season.item.season_number for season in planned],
            )
        for season in planned:
            season.plan_episodes(
                seasons_metadata[f"season/{season.item.season_number}"]
            )

    def _plan_repeat(self):
        """Plan to rewatch the show's watched seasons, other than specials.

        Each of their watched episodes gets an entry to rewatch it.
        """
        seasons = list(
            self.seasons.filter(
                item__season_number__gt=0,
                episodes__status__in=WATCHED_STATUSES,
            )
            .exclude(status__in=REPEAT_STATUSES)
            .distinct(),
        )
        for season in seasons:
            season.status = Status.PLANNING_REPEAT.value
        bulk_update_with_history(seasons, Season, fields=["status"])
        for season in seasons:
            season.plan_repeat_entries()

    def _start_next_repeat_season(self, min_season_number=0):
        """Start rewatching the first planned season after a season number.

        Returns whether one was started.
        """
        season = (
            self.seasons.filter(
                item__season_number__gt=min_season_number,
                status=Status.PLANNING_REPEAT.value,
            )
            .select_related("item")
            .order_by("item__season_number")
            .first()
        )
        if season is None:
            return False

        season.status = Status.REPEATING.value
        bulk_update_with_history([season], Season, fields=["status"])
        self.continue_repeat()
        self.create_user_message(
            f"Season {season.item.season_number} was marked as rewatching "
            "automatically.",
            level=UserMessageLevel.INFO,
        )
        return True

    def continue_repeat(self):
        """Mark the show as rewatched now that one of its seasons is."""
        if self.status in (Status.COMPLETED.value, Status.PLANNING_REPEAT.value):
            self.status = Status.REPEATING.value
            bulk_update_with_history([self], TV, fields=["status"])

    def _complete_repeat(self):
        """Finish the rewatch of every season being rewatched, as a new play now.

        Each episode not yet watched again in the rewatch gets a watch.
        """
        seasons = list(
            self.seasons.filter(status__in=REPEAT_STATUSES)
            .select_related("item")
            .prefetch_related("episodes__item"),
        )
        now = timezone.now().replace(second=0, microsecond=0)
        episodes_to_create = []
        for season in seasons:
            episodes_to_create.extend(season.get_remaining_repeat_eps(now))
            season.status = Status.COMPLETED.value

        save_watches(episodes_to_create)
        bulk_update_with_history(seasons, Season, fields=["status"])

        if episodes_to_create:
            count = len(episodes_to_create)
            self.create_user_message(
                f"had {count} episode{'' if count == 1 else 's'} marked as "
                "rewatched automatically.",
                level=UserMessageLevel.INFO,
            )

        if self._waits_for_next_season():
            self._plan_until_next_season()

    def _cancel_repeat(self):
        """Return the seasons being rewatched to finished, without new watches."""
        seasons = list(self.seasons.filter(status__in=REPEAT_STATUSES))
        for season in seasons:
            season.status = Status.COMPLETED.value
        bulk_update_with_history(seasons, Season, fields=["status"])
        Episode.objects.filter(
            related_season__in=seasons,
            status__in=REPEAT_STATUSES,
        ).delete()

    def _waits_for_next_season(self):
        """Return whether the only seasons left to watch are planned and unaired.

        Seasons missing from the show's metadata aren't known to be unaired.
        """
        seasons_left = list(
            self.seasons.filter(item__season_number__gt=0)
            .exclude(status__in=(Status.COMPLETED.value, *REPEAT_STATUSES))
            .select_related("item"),
        )
        if not seasons_left or any(
            season.status != Status.PLANNING.value for season in seasons_left
        ):
            return False

        tv_metadata = providers.services.get_media_metadata(
            self.item.media_type,
            self.item.media_id,
            self.item.source,
        )
        air_dates = {
            season_data["season_number"]: season_data.get("first_air_date")
            for season_data in tv_metadata.get("related", {}).get("seasons", [])
        }
        current_date = timezone.localdate()
        return all(
            season.item.season_number in air_dates
            and not app.helpers.is_released_date(
                air_dates[season.item.season_number],
                current_date,
            )
            for season in seasons_left
        )

    def _mark_in_progress_seasons_as_dropped(self):
        """Mark all in-progress seasons as dropped, with their unfinished episodes."""
        in_progress_seasons = list(
            self.seasons.filter(status=Status.IN_PROGRESS.value),
        )

        for season in in_progress_seasons:
            season.status = Status.DROPPED.value

        if in_progress_seasons:
            bulk_update_with_history(
                in_progress_seasons,
                Season,
                fields=["status"],
            )
        for season in in_progress_seasons:
            season.move_entries(Status.IN_PROGRESS.value)

    def _start_next_available_season(
        self,
        min_season_number=0,
    ):
        """Find the next available season to watch and set it to in-progress."""
        min_season_number = int(min_season_number or 0)
        current_date = timezone.localdate()
        existing_seasons = {
            season.item.season_number: season
            for season in self.seasons.filter(
                item__season_number__gt=min_season_number,
            ).order_by("item__season_number")
        }
        tv_metadata = providers.services.get_media_metadata(
            self.item.media_type,
            self.item.media_id,
            self.item.source,
        )
        related_seasons = tv_metadata.get("related", {}).get("seasons", [])

        season_started = False
        started_season_number = None

        for season_data in related_seasons:
            season_number = season_data["season_number"]
            if season_number <= min_season_number:
                continue

            next_unwatched_season = existing_seasons.get(season_number)
            # Seasons being rewatched were finished before
            if next_unwatched_season and next_unwatched_season.status in (
                Status.COMPLETED.value,
                *REPEAT_STATUSES,
            ):
                continue

            if not app.helpers.is_released_date(
                season_data.get("first_air_date"),
                current_date,
            ):
                continue

            if next_unwatched_season is None:
                item, _ = Item.objects.get_or_create(
                    media_id=self.item.media_id,
                    source=self.item.source,
                    media_type=MediaTypes.SEASON.value,
                    season_number=season_number,
                    defaults={
                        "title": self.item.title,
                        "image": season_data["image"],
                    },
                )

                next_unwatched_season = Season(
                    item=item,
                    user=self.user,
                    related_tv=self,
                    status=Status.IN_PROGRESS.value,
                )
                bulk_create_with_history([next_unwatched_season], Season)
                season_started = True
                started_season_number = season_number
                break

            if next_unwatched_season.status != Status.IN_PROGRESS.value:
                previous_status = next_unwatched_season.status
                next_unwatched_season.status = Status.IN_PROGRESS.value
                bulk_update_with_history(
                    [next_unwatched_season],
                    Season,
                    fields=["status"],
                )
                next_unwatched_season.move_entries(previous_status)
                season_started = True
                started_season_number = season_number
            else:
                season_started = True
            break

        if season_started and self.status != Status.IN_PROGRESS.value:
            self.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )

        if started_season_number is not None:
            self.create_user_message(
                f"Season {started_season_number} was marked as in progress "
                "automatically.",
                level=UserMessageLevel.INFO,
            )

        return season_started

    def _handle_completed_season(
        self,
        completed_season_number,
    ):
        """Start the next season, or complete the TV show if no seasons remain.

        While the show is rewatched, the next season planned to be rewatched
        comes first, and the show stays rewatched until none are left. Then it's
        finished again, as it was before.
        """
        if self.status in REPEAT_STATUSES:
            if self._start_next_repeat_season(completed_season_number):
                return
            if not self.seasons.filter(status__in=REPEAT_STATUSES).exists():
                self.create_user_message(
                    "was marked as rewatched automatically.",
                    level=UserMessageLevel.SUCCESS,
                )
                if self._waits_for_next_season():
                    self._plan_until_next_season()
                else:
                    self.status = Status.COMPLETED.value
                    bulk_update_with_history([self], TV, fields=["status"])
            return

        if self._start_next_available_season(
            completed_season_number,
        ):
            return

        # Seasons being repeated were finished before
        incomplete_seasons_exist = (
            self.seasons.filter(
                item__season_number__gt=0,
            )
            .exclude(
                status__in=(Status.COMPLETED.value, *REPEAT_STATUSES),
            )
            .exists()
        )

        if self._waits_for_next_season():
            self._plan_until_next_season()

        elif incomplete_seasons_exist and self.status != Status.IN_PROGRESS.value:
            self.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )
            self.create_user_message(
                "remains in progress because another season is still "
                "pending or has not aired yet.",
                level=UserMessageLevel.INFO,
            )

        elif not incomplete_seasons_exist and self.status != Status.COMPLETED.value:
            self.status = Status.COMPLETED.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )
            self.create_user_message(
                "was marked as completed automatically.",
                level=UserMessageLevel.SUCCESS,
            )


class Season(Media):
    """Model for seasons of TV shows."""

    related_tv = models.ForeignKey(
        TV,
        on_delete=models.CASCADE,
        related_name="seasons",
    )
    tracker = FieldTracker()

    class Meta:
        """Limit the uniqueness of seasons.

        Only one season per media can have the same season number.
        """

        constraints = [
            models.UniqueConstraint(
                fields=["related_tv", "item"],
                name="%(app_label)s_season_unique_tv_item",
            ),
        ]

    def __str__(self):
        """Return the title of the media and season number."""
        return f"{self.item.title} S{self.item.season_number}"

    @tracker  # postpone field reset until after the save
    def save(self, *args, **kwargs):
        """Save the season, moving its episodes' entries and its show on.

        See `move_entries` for what a new status does to the entries.
        """
        # if related_tv is not set
        if self.related_tv_id is None:
            self.related_tv = self.get_tv()

        repeat_status_from = self.tracker.previous("status")

        super(Media, self).save(*args, **kwargs)

        if self.tracker.has_changed("status"):
            self.move_entries(repeat_status_from)
            if (
                self.status == Status.COMPLETED.value
                and repeat_status_from in REPEAT_STATUSES
            ):
                self._complete_repeat()

            elif self.status == Status.COMPLETED.value:
                self._completed()

            elif (
                self.status == Status.DROPPED.value
                and self.related_tv.status != Status.DROPPED.value
                and self.item.season_number != 0
            ):
                self.related_tv.status = Status.DROPPED.value
                bulk_update_with_history(
                    [self.related_tv],
                    TV,
                    fields=["status"],
                )

            elif (
                self.status == Status.IN_PROGRESS.value
                and self.related_tv.status != Status.IN_PROGRESS.value
            ):
                self.related_tv.status = Status.IN_PROGRESS.value
                bulk_update_with_history(
                    [self.related_tv],
                    TV,
                    fields=["status"],
                )

            elif self.status == Status.REPEATING.value:
                self.related_tv.continue_repeat()

            self.item.fetch_releases(delay=True)

    def _completed(self):
        """Watch the season's released episodes left, then move its show on.

        A season with unreleased episodes is left in progress.
        """
        season_metadata = providers.services.get_media_metadata(
            MediaTypes.SEASON.value,
            self.item.media_id,
            self.item.source,
            [self.item.season_number],
        )
        current_date = timezone.localdate()
        target_status = self.get_completion_status(
            season_metadata,
            unreleased_only_status=Status.IN_PROGRESS.value,
            current_date=current_date,
        )
        episodes_to_create = self.get_remaining_eps(
            season_metadata,
            current_date=current_date,
        )
        if episodes_to_create:
            save_watches(episodes_to_create)
            created_episodes_count = len(episodes_to_create)
            episode_label = "episode" if created_episodes_count == 1 else "episodes"
            self.create_user_message(
                f"had {created_episodes_count} released {episode_label} "
                "marked as watched automatically.",
                level=UserMessageLevel.INFO,
            )

        if target_status == Status.COMPLETED.value:
            self.related_tv._handle_completed_season(
                self.item.season_number,
            )
        else:
            self.status = target_status
            bulk_update_with_history(
                [self],
                Season,
                fields=["status"],
            )
            self.move_entries(Status.COMPLETED.value)
            self.create_user_message(
                "was left in progress because unreleased episodes remain.",
                level=UserMessageLevel.WARNING,
            )

            if self.related_tv.status != Status.IN_PROGRESS.value:
                self.related_tv.status = Status.IN_PROGRESS.value
                bulk_update_with_history(
                    [self.related_tv],
                    TV,
                    fields=["status"],
                )

    def _complete_repeat(self):
        """Finish the season's rewatch as a new play now, then move its show on.

        Each episode not yet watched again in the rewatch gets a watch.
        """
        now = timezone.now().replace(second=0, microsecond=0)
        episodes_to_create = self.get_remaining_repeat_eps(now)
        save_watches(episodes_to_create)
        if episodes_to_create:
            count = len(episodes_to_create)
            self.create_user_message(
                f"had {count} episode{'' if count == 1 else 's'} marked as "
                "rewatched automatically.",
                level=UserMessageLevel.INFO,
            )
        self.related_tv._handle_completed_season(self.item.season_number)

    @property
    def watches(self):
        """Return the season's episode entries that are watches.

        Reads its `episodes` prefetch when there is one.
        """
        return [episode for episode in self.episodes.all() if episode.is_watch]

    @property
    def watched_entries(self):
        """Return the season's entries that make their episodes watched.

        Unlike `watches`, these include those planned or being watched again.
        """
        return [episode for episode in self.episodes.all() if episode.marks_watched]

    @property
    def plays(self):
        """Return how many times every watched episode of the season was watched."""
        counts = Counter(episode.item.episode_number for episode in self.watches)
        return min(counts.values()) if counts else 0

    @property
    def repeat_entries(self):
        """Return the season's entries for watching an episode again, still to do.

        Planning a rewatch adds one for each watched episode, and watching the
        episode turns it into a watch.
        """
        return [
            episode
            for episode in self.episodes.all()
            if episode.status in REPEAT_STATUSES
        ]

    @property
    def repeated_episode_numbers(self):
        """Return the numbers of the episodes watched again in the season's rewatch.

        Those are its watched episodes with no entry left to watch them again.
        Seasons not being rewatched have none.
        """
        if self.status not in REPEAT_STATUSES:
            return set()
        left = {episode.item.episode_number for episode in self.repeat_entries}
        return {episode.item.episode_number for episode in self.watches} - left

    @property
    def next_repeat_episode_number(self):
        """Return the first episode left to watch again, or None."""
        return min(
            (episode.item.episode_number for episode in self.repeat_entries),
            default=None,
        )

    def finish_repeat_if_done(self):
        """Finish the season's rewatch when no episode is left to watch again.

        It's reloaded first, to see its entries as saved. Returns whether it
        was finished, which moves its show on.
        """
        self.refresh_from_db()
        if (
            self.status not in REPEAT_STATUSES
            or self.next_repeat_episode_number is not None
        ):
            return False

        self.status = Status.COMPLETED.value
        bulk_update_with_history([self], Season, fields=["status"])
        self.create_user_message(
            "was marked as rewatched automatically.",
            level=UserMessageLevel.SUCCESS,
        )
        self.related_tv._handle_completed_season(self.item.season_number)
        return True

    def get_remaining_repeat_eps(self, end_date):
        """Return the entries left to watch again, each made a watch on a date.

        They're saved with `save_watches`.
        """
        entries = sorted(
            self.repeat_entries,
            key=lambda episode: episode.item.episode_number,
        )
        for entry in entries:
            entry.status = WATCH_STATUS
            entry.end_date = end_date
        return entries

    def move_entries(self, previous_status):
        """Move the season's episode entries on after its status changed.

        `previous_status` is the status before the change. Pausing or dropping
        it pauses or drops its unfinished entries, and starting or planning it
        resumes them: those with a start date in
        progress when it's started, the others planned. Planning a rewatch plans
        one for each watched episode, and leaving a rewatch other than by
        finishing it drops what was left of it. Episodes without an entry are
        planned by `plan_episodes`, as that looks their season up.
        """
        entries = Episode.objects.filter(related_season=self)
        if self.status in (Status.PAUSED.value, Status.DROPPED.value):
            entries.filter(status__in=UNFINISHED_STATUSES).update(status=self.status)
        elif self.status in (Status.IN_PROGRESS.value, Status.PLANNING.value):
            stopped = entries.filter(
                status__in=(Status.PAUSED.value, Status.DROPPED.value),
            )
            if self.status == Status.IN_PROGRESS.value:
                stopped.filter(start_date__isnull=False).update(status=self.status)
            stopped.update(status=Status.PLANNING.value)

        if self.status in REPEAT_STATUSES and previous_status not in REPEAT_STATUSES:
            self.plan_repeat_entries()
        elif previous_status in REPEAT_STATUSES and self.status not in (
            WATCH_STATUS,
            *REPEAT_STATUSES,
        ):
            entries.filter(status__in=REPEAT_STATUSES).delete()

    def plan_episodes(self, season_metadata=None):
        """Plan each of the season's known episodes that has no entry yet.

        Called when the user plans the season, or its show. `season_metadata`
        is fetched when it isn't given.
        """
        if season_metadata is None:
            season_metadata = providers.services.get_media_metadata(
                MediaTypes.SEASON.value,
                self.item.media_id,
                self.item.source,
                [self.item.season_number],
            )
        tracked = set(
            Episode.objects.filter(related_season=self).values_list(
                "item__episode_number",
                flat=True,
            ),
        )
        numbers = [
            episode["episode_number"]
            for episode in season_metadata["episodes"]
            if episode["episode_number"] not in tracked
        ]
        items = self._episode_items(season_metadata, numbers)
        Episode.objects.bulk_create(
            [
                Episode(
                    related_season=self,
                    item=items[number],
                    status=Status.PLANNING.value,
                )
                for number in numbers
            ],
        )

    def plan_repeat_entries(self, episode_number=None):
        """Plan to watch again each watched episode not planned to be already.

        With `episode_number`, only that episode. Returns the entries added.
        """
        entries = Episode.objects.filter(related_season=self)
        if episode_number is not None:
            entries = entries.filter(item__episode_number=episode_number)
        entries = list(entries)
        planned = {
            entry.item_id for entry in entries if entry.status in REPEAT_STATUSES
        }
        watched = {
            entry.item_id
            for entry in entries
            if entry.is_watch and entry.item_id not in planned
        }
        return Episode.objects.bulk_create(
            [
                Episode(
                    related_season=self,
                    item_id=item_id,
                    status=Status.PLANNING_REPEAT.value,
                )
                for item_id in sorted(watched)
            ],
        )

    def _episode_items(self, season_metadata, numbers):
        """Return the items of some of the season's episodes by number.

        Those missing are created.
        """
        existing = Item.objects.filter(
            media_id=self.item.media_id,
            source=self.item.source,
            media_type=MediaTypes.EPISODE.value,
            season_number=self.item.season_number,
            episode_number__in=numbers,
        )
        items = {item.episode_number: item for item in existing}
        missing = [
            Item(
                media_id=self.item.media_id,
                source=self.item.source,
                media_type=MediaTypes.EPISODE.value,
                season_number=self.item.season_number,
                episode_number=episode["episode_number"],
                title=self.item.title,
                image=_episode_image(episode),
            )
            for episode in season_metadata["episodes"]
            if episode["episode_number"] in numbers
            and episode["episode_number"] not in items
        ]
        if missing:
            # Another request may have made some meanwhile
            Item.objects.bulk_create(missing, ignore_conflicts=True)
            items = {item.episode_number: item for item in existing.all()}
        return items

    def _get_latest_watched_episode_number(self):
        """Return the highest watched episode number for the season.

        An episode only planned or being watched again was watched.
        """
        if self.pk is None:
            return 0

        latest_watched_ep_num = Episode.objects.filter(
            related_season=self, status__in=WATCHED_STATUSES
        ).aggregate(latest_watched_ep_num=Max("item__episode_number"))[
            "latest_watched_ep_num"
        ]

        return latest_watched_ep_num or 0

    def get_completion_status(
        self,
        season_metadata,
        unreleased_only_status,
        current_date,
    ):
        """Return the season status after completing all already released episodes."""
        latest_watched_ep_num = self._get_latest_watched_episode_number()
        released_remaining_exists = False
        unreleased_remaining_exists = False

        for episode in season_metadata["episodes"]:
            if episode["episode_number"] <= latest_watched_ep_num:
                continue

            if app.helpers.is_released_date(episode.get("air_date"), current_date):
                released_remaining_exists = True
            else:
                unreleased_remaining_exists = True

        if not unreleased_remaining_exists:
            return Status.COMPLETED.value

        if latest_watched_ep_num > 0 or released_remaining_exists:
            return Status.IN_PROGRESS.value

        return unreleased_only_status

    @property
    def progress(self):
        """Return the current episode number of the season.

        While it's rewatched, that's how many episodes were watched again.
        """
        if self.status in REPEAT_STATUSES:
            return len(self.repeated_episode_numbers)

        # An episode planned or being watched again was watched
        episodes = self.watched_entries
        if not episodes:
            return 0

        if self.status == Status.IN_PROGRESS.value:
            # Sort by most recently watched, then by episode number
            sorted_episodes = sorted(
                episodes,
                key=lambda e: (
                    e.end_date is not None,
                    e.end_date.timestamp() if e.end_date else 0,
                    e.item.episode_number,
                ),
                reverse=True,
            )
        else:
            # Default sorting by episode_number
            sorted_episodes = sorted(
                episodes,
                key=lambda e: -e.item.episode_number,
            )

        return sorted_episodes[0].item.episode_number

    @property
    def progressed_at(self):
        """Return the date when the last episode was watched."""
        dates = [
            episode.end_date for episode in self.watches if episode.end_date is not None
        ]
        return max(dates) if dates else None

    @property
    def start_date(self):
        """Return the date of the first episode watched."""
        dates = [
            episode.end_date for episode in self.watches if episode.end_date is not None
        ]
        return min(dates) if dates else None

    @property
    def end_date(self):
        """Return the date of the last episode watched."""
        dates = [
            episode.end_date for episode in self.watches if episode.end_date is not None
        ]
        return max(dates) if dates else None

    def increase_progress(self):
        """Watch the next episode of the season."""
        if self.status in REPEAT_STATUSES:
            next_episode_number = self.next_repeat_episode_number
            if next_episode_number:
                now = timezone.now().replace(second=0, microsecond=0)
                self.watch(next_episode_number, now)
            return

        season_metadata = providers.services.get_media_metadata(
            MediaTypes.SEASON.value,
            self.item.media_id,
            self.item.source,
            [self.item.season_number],
        )
        episodes = season_metadata["episodes"]

        if self.progress == 0:
            # start watching from the first episode
            next_episode_number = episodes[0]["episode_number"]
        else:
            next_episode_number = providers.tmdb.find_next_episode(
                self.progress,
                episodes,
            )

        now = timezone.now().replace(second=0, microsecond=0)

        if next_episode_number:
            self.watch(next_episode_number, now)
        else:
            logger.info("No more episodes to watch.")

    def watch(
        self,
        episode_number,
        end_date,
        notes="",
        season_metadata=None,
        start_date=None,
        score=None,
    ):
        """Watch an episode of the season, turning the entry planning it into a watch.

        While the season is rewatched, that's its entry to watch it again, else
        its first watch's, else one planning to watch it again on its own. An
        episode without one gets a new watch. Notes, a start date and a score,
        when given, replace the entry's. `season_metadata` is fetched when it
        isn't given.
        """
        item = self.get_episode_item(episode_number, season_metadata)
        first = (
            REPEAT_STATUSES if self.status in REPEAT_STATUSES else UNFINISHED_STATUSES
        )
        entry = min(
            Episode.objects.filter(related_season=self, item=item).exclude(
                status=WATCH_STATUS,
            ),
            key=lambda entry: (entry.status not in first, entry.created_at),
            default=None,
        )
        if entry is None:
            entry = Episode(related_season=self, item=item, notes=notes)
        elif notes:
            entry.notes = notes
        if start_date is not None:
            entry.start_date = start_date
        if score is not None:
            entry.score = score
        entry.status = WATCH_STATUS
        entry.end_date = end_date
        entry.save()
        logger.info("%s watched.", entry)

    def decrease_progress(self):
        """Unwatch the current episode of the season."""
        if self.status in REPEAT_STATUSES:
            self._undo_latest_repeat()
            return

        self.unwatch(self.progress)

    def _undo_latest_repeat(self):
        """Plan to watch again the episode last watched again in the rewatch.

        Its latest watch goes back to being the entry to watch it again.
        """
        rewatched = self.repeated_episode_numbers
        counts = Counter(episode.item.episode_number for episode in self.watches)
        latest = max(
            (
                episode
                for episode in self.watches
                if episode.item.episode_number in rewatched
                # An earlier watch stays, as rewatching added one
                and counts[episode.item.episode_number] > 1
            ),
            key=lambda episode: (
                episode.end_date is not None,
                episode.end_date or episode.created_at,
                episode.created_at,
            ),
            default=None,
        )
        if latest is not None:
            latest.status = Status.PLANNING_REPEAT.value
            latest.end_date = None
            latest.save()

    def unwatch(self, episode_number):
        """Unwatch the episode instance."""
        item = self.get_episode_item(episode_number)

        episodes = (
            Episode.objects.watches()
            .filter(
                related_season=self,
                item=item,
            )
            .order_by("-end_date")
        )

        episode = episodes.first()

        if episode is None:
            logger.warning(
                "Episode %s does not exist.",
                self.item,
            )
            return

        # Get count before deletion for logging
        remaining_count = episodes.count() - 1

        episode.delete()
        logger.info(
            "Deleted %s S%02dE%02d (%d remaining instances)",
            self.item.title,
            self.item.season_number,
            episode_number,
            remaining_count,
        )

    def get_tv(self):
        """Get related TV instance for a season and create it if it doesn't exist."""
        try:
            tv = TV.objects.get(
                item__media_id=self.item.media_id,
                item__media_type=MediaTypes.TV.value,
                item__season_number=None,
                item__source=self.item.source,
                user=self.user,
            )
        except TV.DoesNotExist:
            tv_metadata = providers.services.get_media_metadata(
                MediaTypes.TV.value,
                self.item.media_id,
                self.item.source,
            )

            # creating tv with multiple seasons from a completed season
            if (
                self.status == Status.COMPLETED.value
                and tv_metadata["details"]["seasons"] > 1
            ):
                status = Status.IN_PROGRESS.value
            else:
                status = self.status

            item, _ = Item.objects.get_or_create(
                media_id=self.item.media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                defaults={
                    "title": tv_metadata["title"],
                    "image": tv_metadata["image"],
                },
            )

            tv = TV(
                item=item,
                score=None,
                status=status,
                notes="",
                user=self.user,
            )

            # save_base to avoid custom save method
            TV.save_base(tv)

            logger.info("%s did not exist, it was created successfully.", tv)

        return tv

    def get_remaining_eps(
        self,
        season_metadata,
        current_date,
    ):
        """Return the watches needed to complete a season, saved by `save_watches`.

        Each released episode after the latest watched gets one, and so does
        every released episode not watched with an unfinished entry, which
        becomes it.
        """
        latest_watched_ep_num = self._get_latest_watched_episode_number()
        unfinished = {}
        if self.pk is not None:
            unfinished = {
                entry.item.episode_number: entry
                for entry in Episode.objects.filter(
                    related_season=self,
                    status__in=UNFINISHED_STATUSES,
                )
                # An episode watched already isn't watched again
                .exclude(
                    item__in=Episode.objects.watches()
                    .filter(related_season=self)
                    .values("item"),
                )
                .select_related("item")
            }
        episodes_to_create = []

        # Calculate current time once before the loop
        now = timezone.now().replace(second=0, microsecond=0)

        # Create Episode objects for the remaining episodes
        for episode in reversed(season_metadata["episodes"]):
            entry = unfinished.get(episode["episode_number"])
            if (
                episode["episode_number"] <= latest_watched_ep_num and entry is None
            ) or not app.helpers.is_released_date(
                episode.get("air_date"),
                current_date,
            ):
                continue

            if entry is None:
                item = self.get_episode_item(episode["episode_number"], season_metadata)
                entry = Episode(related_season=self, item=item)
            entry.status = WATCH_STATUS
            # Resolve end_date based on user preference
            entry.end_date = self.user.resolve_watch_date(now, episode.get("air_date"))
            episodes_to_create.append(entry)

        return episodes_to_create

    def get_episode_item(self, episode_number, season_metadata=None):
        """Get the episode item instance, create it if it doesn't exist."""
        if not season_metadata:
            season_metadata = providers.services.get_media_metadata(
                MediaTypes.SEASON.value,
                self.item.media_id,
                self.item.source,
                [self.item.season_number],
            )

        image = next(
            (
                _episode_image(episode)
                for episode in season_metadata["episodes"]
                if episode["episode_number"] == int(episode_number)
            ),
            settings.IMG_NONE,
        )

        item, _ = Item.objects.get_or_create(
            media_id=self.item.media_id,
            source=self.item.source,
            media_type=MediaTypes.EPISODE.value,
            season_number=self.item.season_number,
            episode_number=episode_number,
            defaults={
                "title": self.item.title,
                "image": image,
            },
        )

        return item


def _episode_image(episode):
    """Return the image of an episode as its season's metadata lists it."""
    if episode.get("still_path"):
        return f"https://image.tmdb.org/t/p/original{episode['still_path']}"
    # Manual seasons give their episodes' own
    return episode.get("image", settings.IMG_NONE)


def save_watches(episodes):
    """Save episode entries made watches, creating the new ones and updating the rest.

    Like other bulk saves, it doesn't move their seasons on, but the watches
    fulfil their episodes' other entries, as `settle_plans` does.
    """
    bulk_create_with_history(
        [episode for episode in episodes if episode.pk is None],
        Episode,
    )
    bulk_update_with_history(
        [episode for episode in episodes if episode.pk is not None],
        Episode,
        fields=["status", "end_date"],
    )
    settle_plans(episodes)


def settle_plans(episodes):
    """Delete the entries still to do of episodes given watches saved in bulk.

    A watch fulfils its episode's plans, as saving one does. `episodes` are
    saved entries of one user's seasons, and those that aren't watches stay.
    """
    watches = [episode for episode in episodes if episode.is_watch]
    if not watches:
        return
    # An episode's item is in only one of a user's seasons
    Episode.objects.filter(
        related_season_id__in={episode.related_season_id for episode in watches},
        item_id__in={episode.item_id for episode in watches},
    ).exclude(status=WATCH_STATUS).exclude(
        pk__in=[episode.pk for episode in episodes],
    ).delete()


class EpisodeQuerySet(models.QuerySet):
    """Queries of episode entries."""

    def watches(self):
        """Return the entries that are watches, the plays of their episodes."""
        return self.filter(status=WATCH_STATUS)


class Episode(models.Model):
    """Model for an entry of an episode of a season, e.g. one watch of it."""

    history = HistoricalRecords(
        cascade_delete_history=True,
        excluded_fields=["item", "related_season", "created_at"],
    )

    created_at = models.DateTimeField(auto_now_add=True)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    related_season = models.ForeignKey(
        Season,
        on_delete=models.CASCADE,
        related_name="episodes",
    )
    status = models.CharField(
        max_length=20,
        choices=Status,
        default=Status.COMPLETED.value,
        db_index=True,
    )
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    # The user's rating of this watch of the episode
    score = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(0),
            MaxValueValidator(10),
        ],
    )

    objects = EpisodeQuerySet.as_manager()
    tracker = FieldTracker(fields=["status"])

    class Meta:
        """Meta options for the model."""

        ordering = [
            "related_season",
            "item__episode_number",
            "-end_date",
            "-created_at",
        ]

    def __str__(self):
        """Return the season and episode number."""
        return self.item.__str__()

    @tracker  # postpone field reset until after the save
    def save(self, *args, **kwargs):
        """Save the entry, moving its season and show on when it becomes a watch.

        A watch fulfils the episode's other entries still to do, which are
        deleted. A watch that no longer marks its episode watched moves them
        back, as deleting it does, and an entry to watch the episode again
        that stops being one may finish its season's rewatch. Changing
        anything else leaves their statuses as they are.
        """
        previous = None if self._state.adding else self.tracker.previous("status")
        super().save(*args, **kwargs)
        if self.is_watch and previous != WATCH_STATUS:
            settle_plans([self])
            self._move_parents_on()
        elif previous == WATCH_STATUS and not self.marks_watched:
            season = self.related_season
            self._update_parent_statuses_after_delete(
                season,
                season.related_tv,
                self.item.episode_number,
            )
        elif previous in REPEAT_STATUSES and self.status not in REPEAT_STATUSES:
            self.related_season.finish_repeat_if_done()

    def _move_parents_on(self):
        """Move the season and show on now that the episode was watched."""
        season = self.related_season
        if season.status in REPEAT_STATUSES:
            self._continue_repeat(season)
            return
        if (
            season.status == Status.COMPLETED.value
            and season.episodes.watches().filter(item=self.item).count() > 1
        ):
            # Watching a finished season's episode again leaves it finished
            return

        season_number = self.item.season_number
        tv_with_seasons_metadata = providers.services.get_media_metadata(
            "tv_with_seasons",
            self.item.media_id,
            self.item.source,
            [season_number],
        )
        season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]
        max_progress = len(season_metadata["episodes"])

        # clear prefetch cache to get the updated episodes
        self.related_season.refresh_from_db()

        is_finale = self.item.episode_number == max_progress
        season_just_completed = False
        if is_finale:
            if self.related_season.status != Status.COMPLETED.value:
                self.related_season.status = Status.COMPLETED.value
                bulk_update_with_history(
                    [self.related_season],
                    Season,
                    fields=["status"],
                )
                season_just_completed = True
                self.related_season.create_user_message(
                    "was marked as completed automatically.",
                    level=UserMessageLevel.SUCCESS,
                )

        elif self.related_season.status != Status.IN_PROGRESS.value:
            previous_status = self.related_season.status
            self.related_season.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self.related_season],
                Season,
                fields=["status"],
            )
            # A paused or dropped season's other episodes are resumed with it
            self.related_season.move_entries(previous_status)

        if season_just_completed:
            self.related_season.related_tv._handle_completed_season(season_number)
        elif (
            not is_finale
            and self.related_season.related_tv.status != Status.IN_PROGRESS.value
        ):
            self.related_season.related_tv.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self.related_season.related_tv],
                TV,
                fields=["status"],
            )

    @property
    def is_watch(self):
        """Return whether the entry is a watch, one play of its episode."""
        return self.status == WATCH_STATUS

    @property
    def marks_watched(self):
        """Return whether the entry shows its episode was watched, e.g. a watch."""
        return self.status in WATCHED_STATUSES

    def _continue_repeat(self, season):
        """Move a rewatched season on, finishing it once every episode is seen again."""
        if season.finish_repeat_if_done():
            return

        if season.status != Status.REPEATING.value:
            season.status = Status.REPEATING.value
            bulk_update_with_history([season], Season, fields=["status"])
        season.related_tv.continue_repeat()

    def delete(self, *args, **kwargs):
        """Delete the entry, moving its season and show back when it was a watch.

        A watch deleted during its season's rewatch leaves its episode to watch
        again when it has another watch, and deleting the last entry to watch
        an episode again finishes the rewatch.
        """
        season = self.related_season
        tv = season.related_tv
        deleted_episode_number = self.item.episode_number
        was_watch = self.is_watch
        was_repeat = self.status in REPEAT_STATUSES

        result = super().delete(*args, **kwargs)

        if was_watch:
            self._update_parent_statuses_after_delete(
                season,
                tv,
                deleted_episode_number,
            )
            if season.status in REPEAT_STATUSES:
                season.plan_repeat_entries(deleted_episode_number)
        elif was_repeat:
            season.finish_repeat_if_done()
        return result

    def _update_parent_statuses_after_delete(self, season, tv, deleted_episode_number):
        """Move completed parents back to in progress after unwatching progress."""
        season.refresh_from_db()
        tv.refresh_from_db()

        if (
            season.status == Status.COMPLETED.value
            and season.progress < deleted_episode_number
        ):
            season.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [season],
                Season,
                fields=["status"],
                default_user=season.user,
            )

        # Seasons being rewatched were finished before
        if (
            season.status not in (Status.COMPLETED.value, *REPEAT_STATUSES)
            and tv.status == Status.COMPLETED.value
        ):
            tv.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [tv],
                TV,
                fields=["status"],
                default_user=season.user,
            )


class Manga(Media):
    """Model for manga."""

    tracker = FieldTracker()


class Anime(Media):
    """Model for anime."""

    tracker = FieldTracker()


class Movie(Media):
    """Model for movies."""

    tracker = FieldTracker()


class Game(Media):
    """Model for games."""

    tracker = FieldTracker()

    @property
    def formatted_progress(self):
        """Return progress in hours:minutes format."""
        return app.helpers.minutes_to_hhmm(self.progress)

    def increase_progress(self):
        """Increase the progress of the media by 30 minutes."""
        self.progress += 30
        self.save()
        logger.info("Changed playtime of %s to %s", self, self.formatted_progress)

    def decrease_progress(self):
        """Decrease the progress of the media by 30 minutes."""
        self.progress -= 30
        self.save()
        logger.info("Changed playtime of %s to %s", self, self.formatted_progress)


class Book(Media):
    """Model for books."""

    tracker = FieldTracker()


class Comic(Media):
    """Model for comics."""

    tracker = FieldTracker()


class BoardGame(Media):
    """Model for board games."""

    tracker = FieldTracker()
