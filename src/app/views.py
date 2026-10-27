import logging
from pathlib import Path
from urllib.parse import urlencode

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, prefetch_related_objects
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from app import (
    config,
    helpers,
    history_processor,
    item_details,
    library_fields,
    quick_actions,
)
from app import home as home_helpers
from app import statistics as stats
from app.forms import EpisodeForm, ManualItemForm, get_form_class
from app.models import (
    REPEAT_STATUSES,
    TV,
    BasicMedia,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
    UserMessage,
    is_caught_up,
)
from app.providers import manual, services, tmdb
from app.templatetags import app_tags
from events.models import Event
from users.models import (
    DateFormatChoices,
    LayoutChoices,
    LibraryStatusChoices,
    MediaSortChoices,
    MediaStatusChoices,
    User,
)

logger = logging.getLogger(__name__)

# The highest score, matching the stars' ten half points
MAX_SCORE = 10

# Entries per page of a media type's list
LIST_PAGE_SIZE = 32

# Media gone through all at once, whose list rows have no progress to show
UNPROGRESSED_MEDIA_TYPES = (MediaTypes.MOVIE.value, MediaTypes.BOARDGAME.value)

# The sorts whose value a list's rows already show in a column of their own, so
# their rows' last column is the last activity instead
SORTS_WITH_COLUMNS = (
    MediaSortChoices.TITLE,
    MediaSortChoices.STATUS,
    MediaSortChoices.SCORE,
    MediaSortChoices.PROGRESS,
)


@require_GET
def home(request):
    """Home page with a row of entries per status and media type."""
    context = {"home_rows": home_helpers.build_home_rows(request.user, items_limit=20)}
    return render(request, "app/home.html", context)


@require_POST
def progress_edit(request, media_type, instance_id):
    """Increase or decrease the progress of a media item from the home page.

    Returns the updated card, or removes it and refreshes the home sections when
    the entry no longer belongs in the section named by `home_status`.
    """
    operation = request.POST["operation"]
    home_status = request.POST.get("home_status")

    media = helpers.get_owned_media_or_404(
        request, media_type, instance_id, prefetch=True
    )

    if operation == "increase":
        media.increase_progress()
    elif operation == "decrease":
        media.decrease_progress()

    if media_type == MediaTypes.SEASON.value:
        # clear prefetch cache to get the updated episodes
        media.refresh_from_db()
        prefetch_related_objects(
            [media],
            Prefetch(
                "episodes",
                queryset=Episode.objects.select_related("item"),
            ),
            Prefetch(
                "item__event_set",
                queryset=Event.objects.all(),
                to_attr="prefetched_events",
            ),
        )

    BasicMedia.objects.annotate_max_progress([media], media_type)
    BasicMedia.objects._annotate_next_event([media])

    # The card leaves its section when the entry is no longer in it, e.g. after
    # reaching the last episode marks it completed, or the latest released one
    # leaves it caught up
    if home_status is not None and (media.status != home_status or is_caught_up(media)):
        response = render(request, "app/components/messages/messages_oob.html")
        response["HX-Retarget"] = f"#home-media-{media.item.media_type}-{media.id}"
        response["HX-Reswap"] = "delete"
        # Refresh the sections, so the entry shows up where it belongs now
        response["HX-Trigger"] = "mediaChanged"
        return response

    context = {
        "media": media,
        "home_status": home_status,
    }
    return render(
        request,
        "app/components/home_card_response.html",
        context,
    )


@login_not_required
@require_GET
def season_list(request, username):
    """Redirect to the TV list, since seasons are listed with their shows."""
    url = reverse("medialist", args=[username, MediaTypes.TV.value])
    query = request.GET.urlencode()
    return redirect(f"{url}?{query}" if query else url)


@login_not_required
@require_GET
def media_list(request, username, media_type):
    """Return a user's list of a media type, filtered and sorted.

    The sort, its direction, the status and the layout are saved to the user's
    preferences when it's their own list. A sort given without a direction goes
    in its usual one. HTMX requests get the results alone, or the next page of
    entries when scrolled to.
    """
    target_user = get_object_or_404(User, username=username)
    own_list = request.user == target_user
    if not own_list:
        # privacy check then media type check
        if target_user.profile_private:
            msg = "User not found"
            raise Http404(msg)

        enabled_media_types = target_user.get_enabled_media_types()
        if not enabled_media_types:
            msg = "User doesn't have any media types enabled"
            raise Http404(msg)

        if media_type not in enabled_media_types:
            return redirect(
                "medialist",
                username=target_user.username,
                media_type=enabled_media_types[0],
            )

    # Someone else's list goes by what's asked or their own preferences
    preference = (
        target_user.update_preference if own_list else target_user.get_valid_preference
    )
    layout = preference(f"{media_type}_layout", request.GET.get("layout"))
    sort_field, sort_order = _list_sort(
        target_user,
        media_type,
        request.GET,
        preference,
        own_list=own_list,
    )
    sort_filter = sort_field.key
    status_filter = preference(f"{media_type}_status", request.GET.get("status"))

    search = request.GET.get("search", "").strip()
    filters = library_fields.parse_filters(media_type, request.GET)

    if request.GET.get("part") == "counts":
        # The status counts alone, after a card's action changed them
        return render(
            request,
            "app/components/library_status_chips.html",
            _library_context(target_user, media_type, status_filter, search, filters),
        )

    media_page = Paginator(
        BasicMedia.objects.get_media_list(
            user=target_user,
            media_type=media_type,
            status_filter=status_filter,
            sort_filter=sort_filter,
            search=search,
            sort_order=sort_order,
            filters=filters,
        ),
        LIST_PAGE_SIZE,
    ).get_page(request.GET.get("page", 1))
    BasicMedia.objects.annotate_releases(media_page.object_list, media_type)
    sorted_field = _annotate_sorted_values(
        media_page.object_list,
        media_type,
        sort_filter,
        target_user,
    )

    context = {
        "media_type": media_type,
        "target_user": target_user,
        "media_list": media_page,
        "current_layout": layout,
        "sorted_field": sorted_field,
        "show_progress": media_type not in UNPROGRESSED_MEDIA_TYPES,
    }

    # Soft-navigation requests (e.g. after saving from an edit modal) need the
    # full page for the body swap
    if request.headers.get("HX-Request") and not request.headers.get(
        "X-Soft-Navigation",
    ):
        if media_page.number > 1:
            # A further page is only its cards, so it needs none of the counts,
            # pills or sorts
            template_name = (
                "app/components/media_grid_items.html"
                if layout == LayoutChoices.GRID
                else "app/components/media_list_rows.html"
            )
            return render(request, template_name, context)
        template_name = "app/components/library_results.html"
    else:
        template_name = "app/media_list.html"
        # Only the whole page has the panel, which the results reloads leave be
        bounds, present = library_fields.range_bounds(target_user, media_type)
        context["filter_groups"] = library_fields.panel_filters(
            media_type,
            filters,
            library_fields.present_choices(target_user, media_type),
            bounds,
            present,
        )
        context["sort_choices"] = [
            {
                "value": field.key,
                "label": field.label,
                "group": field.group,
                "order": field.default_order,
                "order_labels": field.order_labels,
            }
            for field in library_fields.listed_sort_fields(
                media_type,
                present,
                sort_filter,
            )
        ]

    context.update(
        _library_context(target_user, media_type, status_filter, search, filters),
        active_filters=library_fields.describe_filters(filters),
        filter_count=len(filters),
        result_count=media_page.paginator.count,
        current_sort=sort_filter,
        current_order=sort_order,
        current_sort_label=sort_field.label,
    )
    return render(request, template_name, context)


def _list_sort(user, media_type, params, preference, *, own_list):
    """Return the sort field and direction a media type's list goes by.

    A saved sort the media type doesn't have, like progress for movies, is the
    default sort, which is saved in its place on the user's own list.
    """
    sort = preference(f"{media_type}_sort", params.get("sort"))
    order = params.get("order")
    field = library_fields.get_sort_field(media_type, sort)
    if field.key != sort and own_list:
        user.update_preference(f"{media_type}_sort", field.key)
    if order is None and ("sort" in params or field.key != sort):
        order = field.default_order
    return field, preference(f"{media_type}_sort_order", order)


def _annotate_sorted_values(media_list, media_type, sort_key, user):
    """Annotate media with the value its list row shows as `sorted_value`.

    It's the value of the field the list is sorted by, else its last activity,
    or when it was added for media not gone through yet. Returns the field the
    column is of, for its heading.
    """
    field = library_fields.get_sort_field(media_type, sort_key)
    if field.key in SORTS_WITH_COLUMNS:
        field = library_fields.get_fields(media_type)[MediaSortChoices.RECENT]
    for media in media_list:
        media.sorted_value = library_fields.list_text(
            field,
            media,
            lambda value: app_tags.date_format(value, user),
        )
    return field


def _library_context(user, media_type, status_filter, search, filters):
    """Return what a media type's list shows of its filters.

    Statuses are named in the media type's words, e.g. Rereading for books, with
    how many media each has. Those with none are left out, except all and the
    one filtered to.
    """
    counts = BasicMedia.objects.get_status_counts(
        user,
        media_type,
        search,
        filters=filters,
    )
    return {
        "media_type": media_type,
        "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
        "target_user": user,
        "current_status": status_filter,
        "current_search": search,
        "status_choices": [
            {
                "value": value,
                "label": label
                if value in (MediaStatusChoices.ALL, LibraryStatusChoices.CAUGHT_UP)
                else config.get_status_label(value, media_type),
                "count": counts[value],
                "all": value == MediaStatusChoices.ALL,
                "caught_up": value == LibraryStatusChoices.CAUGHT_UP,
            }
            for value, label in LibraryStatusChoices.choices
            if counts[value] or value in (MediaStatusChoices.ALL, status_filter)
        ],
    }


@require_GET
def media_search(request):
    """Return the media search page."""
    media_type = request.user.update_preference(
        "last_search_type",
        request.GET["media_type"],
    )
    query = request.GET["q"]
    page = int(request.GET.get("page", 1))
    layout = request.GET.get("layout", "grid")

    # only receives source when searching with secondary source
    source = request.GET.get(
        "source",
        config.get_default_source_name(media_type).value,
    )

    data = services.search(media_type, query, page, source)

    # Enrich search results with user tracking data
    if data.get("results"):
        data["results"] = helpers.enrich_items_with_user_data(
            request, data["results"], "search"
        )

    context = {
        "data": data,
        "source": source,
        "media_type": media_type,
        "layout": layout,
    }

    return render(request, "app/search.html", context)


@require_GET
def media_details(request, source, media_type, media_id, title):  # noqa: ARG001 title for URL
    """Return the details page for a media item."""
    media_metadata = services.get_media_metadata(media_type, media_id, source)
    user_medias = BasicMedia.objects.filter_media_prefetch(
        request.user,
        media_id,
        media_type,
        source,
    )
    current_instance = user_medias[0] if user_medias else None

    if current_instance is not None:
        helpers.refresh_item_image_if_missing(
            current_instance.item, media_metadata.get("image")
        )

    # Enrich related items with user tracking data
    if media_metadata.get("related"):
        for section_name, related_items in media_metadata["related"].items():
            if related_items:
                media_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request, related_items, section_name
                    )
                )

    # A show planned, in progress or rewatched offers its next episode, which is
    # watched in its season
    next_season = None
    if (
        media_type == MediaTypes.TV.value
        and current_instance is not None
        and current_instance.status in quick_actions.EPISODE_STATUSES
    ):
        next_season = quick_actions.get_show_next_season(current_instance)

    if media_type in ["tv", "movie"]:
        watch_providers = tmdb.filter_providers(
            media_metadata.get("providers"), request.user.watch_provider_region
        )
    else:
        watch_providers = None

    ratings = helpers.get_media_ratings(media_metadata)
    item_details.save_page_details(
        current_instance.item if current_instance else None,
        media_type,
        source,
        media_id,
        media_metadata,
        ratings,
    )

    context = {
        "media": media_metadata,
        "media_type": media_type,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "watch_providers": watch_providers,
        "watch_provider_region": request.user.watch_provider_region,
        "next_release": _next_release(current_instance),
        # The entry the tracking button acts on
        "track_target": {
            "entry": next_season or current_instance,
            "media_type": MediaTypes.SEASON.value if next_season else media_type,
            "season_number": (next_season.item.season_number if next_season else None),
        },
        "ratings": ratings,
        "certification": helpers.get_certification(
            media_metadata, request.user.watch_provider_region
        ),
    }
    return render(request, "app/media_details.html", context)


@require_GET
def season_details(request, source, media_id, title, season_number):  # noqa: ARG001 For URL
    """Return the details page for a season."""
    tv_with_seasons_metadata = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        [season_number],
    )
    season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

    user_medias = BasicMedia.objects.filter_media_prefetch(
        request.user,
        media_id,
        MediaTypes.SEASON.value,
        source,
        season_number=season_number,
    )

    current_instance = user_medias[0] if user_medias else None
    episodes_in_db = current_instance.episodes.all() if current_instance else []

    if current_instance is not None:
        helpers.refresh_item_image_if_missing(
            current_instance.item, season_metadata.get("image")
        )

    if source == Sources.MANUAL.value:
        season_metadata["episodes"] = manual.process_episodes(
            season_metadata,
            episodes_in_db,
        )
    else:
        season_metadata["episodes"] = tmdb.process_episodes(
            season_metadata,
            episodes_in_db,
        )

    # A season shares its show's recommendations
    season_metadata["related"] = {
        **season_metadata.get("related", {}),
        "recommendations": tv_with_seasons_metadata.get("related", {}).get(
            "recommendations",
            [],
        ),
    }

    # Enrich related items with user tracking data
    if season_metadata.get("related"):
        for section_name, related_items in season_metadata["related"].items():
            if related_items:
                season_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        section_name,
                    )
                )

    ratings = helpers.get_media_ratings(season_metadata)
    item_details.save_page_details(
        current_instance.item if current_instance else None,
        MediaTypes.SEASON.value,
        source,
        media_id,
        season_metadata,
        ratings,
    )

    context = {
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "next_release": _next_release(current_instance),
        # The entry the tracking button acts on
        "track_target": {
            "entry": current_instance,
            "media_type": MediaTypes.SEASON.value,
            "season_number": season_number,
        },
        "watch_providers": tmdb.filter_providers(
            season_metadata.get("providers"), request.user.watch_provider_region
        ),
        "watch_provider_region": request.user.watch_provider_region,
        "ratings": ratings,
        "certification": helpers.get_certification(
            season_metadata, request.user.watch_provider_region
        ),
    }
    return render(request, "app/media_details.html", context)


def _next_release(entry):
    """Return the next release of a show or season with episodes to watch, or None.

    Its tracking button shows it once there's nothing out to watch.
    """
    if (
        entry is None
        or entry.item.media_type not in quick_actions.EPISODIC_TYPES
        or entry.status not in quick_actions.EPISODE_STATUSES
    ):
        return None
    return helpers.get_next_release(entry)


@require_GET
def episode_details(
    request,
    source,
    media_id,
    title,  # noqa: ARG001 For URL
    season_number,
    episode_number,
):
    """Return the details page for an episode, with the user's watches of it."""
    tv_with_seasons_metadata = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        [season_number],
    )
    season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

    # With its episodes and how many are out, for its progress
    season_instances = BasicMedia.objects.filter_media_prefetch(
        request.user,
        media_id,
        MediaTypes.SEASON.value,
        source,
        season_number=season_number,
    )
    season_instance = season_instances[0] if season_instances else None
    episodes_in_db = season_instance.episodes.all() if season_instance else []

    if source == Sources.MANUAL.value:
        episodes = manual.process_episodes(season_metadata, episodes_in_db)
    else:
        episodes = tmdb.process_episodes(season_metadata, episodes_in_db)

    index = next(
        (
            index
            for index, episode in enumerate(episodes)
            if episode["episode_number"] == episode_number
        ),
        None,
    )
    if index is None:
        msg = "Episode not found"
        raise Http404(msg)
    episode = episodes[index]

    # The show's title, as on season pages, with the episode's own beside it
    media = {
        **episode,
        "title": tv_with_seasons_metadata["title"],
        "episode_title": episode["title"],
        "season_title": season_metadata["season_title"],
        "synopsis": episode["overview"] or "No synopsis available.",
        "backdrop": season_metadata.get("backdrop"),
        "certifications": season_metadata.get("certifications"),
        "details": {
            "air_date": episode["air_date"],
            "runtime": episode.get("runtime"),
        },
    }
    if source == Sources.TMDB.value:
        media.update(tmdb.process_episode_details(season_metadata["episodes"][index]))
        media["source_url"] = (
            f"{season_metadata['source_url']}/episode/{episode_number}"
        )

    context = {
        "media": media,
        "media_type": MediaTypes.EPISODE.value,
        "season_instance": season_instance,
        "episodes": episodes,
        "previous_episode": episodes[index - 1] if index > 0 else None,
        "next_episode": episodes[index + 1] if index + 1 < len(episodes) else None,
        "ratings": helpers.get_media_ratings(media),
        "certification": helpers.get_certification(
            media, request.user.watch_provider_region
        ),
    }
    return render(request, "app/episode_details.html", context)


@require_POST
def update_media_score(request, media_type, instance_id):
    """Update the user's score for a media item, clearing it when it's empty.

    A score that isn't a number from 0 to 10 is refused.
    """
    media = helpers.get_owned_media_or_404(request, media_type, instance_id)

    score = request.POST.get("score")
    if score:
        try:
            score = float(score)
        except ValueError:
            score = None
        if score is None or not 0 <= score <= MAX_SCORE:
            return HttpResponseBadRequest("Score must be a number from 0 to 10")
    else:
        score = None
    media.score = score
    media.save()
    logger.info(
        "%s score updated to %s",
        media,
        score,
    )

    return JsonResponse(
        {
            "success": True,
            "score": score,
        },
    )


@require_POST
def sync_metadata(request, source, media_type, media_id, season_number=None):
    """Refresh the metadata for a media item."""
    if source == Sources.MANUAL.value:
        msg = "Manual items cannot be synced."
        messages.error(request, msg)
        return HttpResponse(
            msg,
            status=400,
            headers={"HX-Redirect": request.POST.get("next", "/")},
        )

    cache_key = f"{source}_{media_type}_{media_id}"
    if media_type == MediaTypes.SEASON.value:
        cache_key += f"_{season_number}"

    ttl = cache.ttl(cache_key)
    logger.debug("%s - Cache TTL for: %s", cache_key, ttl)

    if ttl is not None and ttl > (settings.CACHE_TIMEOUT - 3):
        msg = "The data was recently synced, please wait a few seconds."
        messages.error(request, msg)
        logger.error(msg)
    else:
        deleted = cache.delete(cache_key)
        logger.debug("%s - Old cache deleted: %s", cache_key, deleted)

        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        item, _ = Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
            },
        )
        title = metadata["title"]
        if season_number:
            title += f" - Season {season_number}"

        if media_type == MediaTypes.SEASON.value:
            metadata["episodes"] = tmdb.process_episodes(
                metadata,
                [],
            )

            # Create a dictionary of existing episodes keyed by episode number
            existing_episodes = {
                ep.episode_number: ep
                for ep in Item.objects.filter(
                    source=source,
                    media_type=MediaTypes.EPISODE.value,
                    media_id=media_id,
                    season_number=season_number,
                )
            }

            episodes_to_update = []
            episode_count = 0

            for episode_data in metadata["episodes"]:
                episode_number = episode_data["episode_number"]
                if episode_number in existing_episodes:
                    episode_item = existing_episodes[episode_number]
                    episode_item.title = metadata["title"]
                    episode_item.image = episode_data["image"]
                    episodes_to_update.append(episode_item)
                    episode_count += 1

            logger.info(
                "Found %s existing episodes to update for %s",
                episode_count,
                title,
            )

            if episodes_to_update:
                updated_count = Item.objects.bulk_update(
                    episodes_to_update,
                    ["title", "image"],
                    batch_size=100,
                )
                logger.info(
                    "Successfully updated %s episodes for %s",
                    updated_count,
                    title,
                )

        item.fetch_releases(delay=False)

        msg = f"{title} was synced to {Sources(source).label} successfully."
        messages.success(request, msg)

    if request.headers.get("HX-Request"):
        return HttpResponse(
            status=204,
            headers={
                "HX-Redirect": request.POST["next"],
            },
        )
    return helpers.redirect_back(request)


@require_GET
def track_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
):
    """Return the tracking form for a media item."""
    instance_id = request.GET.get("instance_id")
    if instance_id:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    elif request.GET.get("is_create"):
        media = None
    else:
        # no specific instance, try to find the first one
        user_medias = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
        )
        media = user_medias.first()
        if media:
            instance_id = media.id

    initial_data = {
        "media_id": media_id,
        "source": source,
        "media_type": media_type,
        "season_number": season_number,
        "instance_id": instance_id,
    }

    # What progress counts up to, from what is already known: a saved item's
    # length, or the metadata fetched for the title of a new entry
    total = None
    if media:
        title = media.item
        total = media.item.length
        if media_type == MediaTypes.GAME.value:
            initial_data["progress"] = helpers.minutes_to_hhmm(media.progress)
    else:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        title = metadata["title"]
        total = metadata.get("max_progress")
        if media_type == MediaTypes.SEASON.value:
            title += f" S{season_number}"

    form = get_form_class(media_type)(instance=media, initial=initial_data)
    # Games count time and movies nothing, so only the counted types have a total
    counted = "progress" in form.fields and media_type != MediaTypes.GAME.value

    return render(
        request,
        "app/components/fill_track.html",
        {
            "title": title,
            "form": form,
            "media": media,
            "media_type": media_type,
            "progress_total": total if counted else None,
            "progress_unit": f"{config.get_unit(media_type, short=False)}s"
            if counted
            else "",
            "return_url": request.GET["return_url"],
        },
    )


@require_POST
def media_save(request):
    """Save or update media data to the database."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    season_number = request.POST.get("season_number")
    instance_id = request.POST.get("instance_id")

    if instance_id:
        instance = helpers.get_owned_media_or_404(request, media_type, instance_id)
    else:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
            },
        )
        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(item=item, user=request.user)

    # Only shows cascade, and episode watches have no status
    was_planned = (
        media_type == MediaTypes.TV.value
        and instance.pk is not None
        and instance.status == Status.PLANNING.value
    )

    # Validate the form and save the instance if it's valid
    form_class = get_form_class(media_type)
    form = form_class(request.POST, instance=instance)
    if form.is_valid():
        # A show put on the watchlist brings its seasons with it, or isn't
        # saved at all when they can't be planned, so saving again plans them
        with transaction.atomic():
            form.save()
            if (
                media_type == MediaTypes.TV.value
                and instance.status == Status.PLANNING.value
                and not was_planned
            ):
                instance.plan_seasons()
        logger.info("%s saved successfully.", form.instance)
    else:
        logger.error(form.errors.as_json())
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(
                    request,
                    f"{field.replace('_', ' ').title()}: {error}",
                )

    return helpers.redirect_back(request)


@require_POST
def quick_action(request):
    """Apply the one-click action of a media card and return the updated card.

    The entry is the one given by `instance_id`, else the user's entry for the
    media, which is created if missing. `action` is the key the card showed, or
    with `finish`, that of the button marking a whole show or season watched. If
    it no longer matches, nothing is changed. Removes the card instead of
    updating it when the entry left the section or status filter it was shown in,
    and reloads the page when the action came from a details page.
    """
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    season_number = request.POST.get("season_number") or None
    instance_id = request.POST.get("instance_id")
    expected_action = request.POST["action"]

    if instance_id:
        media = helpers.get_owned_media_or_404(request, media_type, instance_id)
    else:
        media = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
        ).first()

    # A show or season's second button marks all of it watched
    if request.POST.get("finish"):
        action = quick_actions.get_finish_action(media_type, media)
    else:
        action = quick_actions.get_quick_action(media_type, media)
    # Planning a repeat adds a newer entry, which leaves the older one's cards stale
    if (
        action is not None
        and action.key == quick_actions.PLAN_REPEAT
        and media_type not in quick_actions.EPISODIC_TYPES
        and BasicMedia.objects.filter_media(
            request.user,
            media.item.media_id,
            media_type,
            media.item.source,
        ).first()
        != media
    ):
        action = None
    # The card may be stale, e.g. after a double click, so only act on what it showed
    if action is None or action.key != expected_action:
        messages.info(request, "This entry has already changed. Reload to see it.")
        response = render(request, "app/components/messages/messages_oob.html")
        response["HX-Reswap"] = "none"
        return response

    if media is None:
        item = _get_or_create_item(media_id, source, media_type, season_number)
        model = apps.get_model(app_label="app", model_name=media_type)
        media = model(item=item, user=request.user)
    media = quick_actions.apply_quick_action(action, media)
    logger.info("Applied quick action %s to %s", action.key, media)
    messages.success(request, quick_actions.success_message(action, media))

    if request.POST.get("card") == "details":
        # The details page shows the entry in many places, so reload all of it
        response = HttpResponse()
        response["HX-Refresh"] = "true"
        return response

    if request.POST.get("card") == "home":
        # Home actions change the status, so the card moves to another section
        response = render(request, "app/components/messages/messages_oob.html")
        response["HX-Reswap"] = "delete"
        response["HX-Trigger"] = "mediaChanged"
        return response

    status_filter = request.POST.get("status")
    if not _matches_status_filter(media, status_filter):
        # The entry no longer matches the list's status filter
        response = render(request, "app/components/messages/messages_oob.html")
        response["HX-Reswap"] = "delete"
        response["HX-Trigger"] = "libraryChanged"
        return response

    return _quick_action_card(request, media_type, media.id)


def _quick_action_card(request, media_type, media_id):
    """Return an entry's updated card or list row after a quick action."""
    media = BasicMedia.objects.get_media_prefetch(request.user, media_type, media_id)
    BasicMedia.objects.annotate_releases([media], media_type)
    entries = list(
        BasicMedia.objects.filter_media(
            request.user,
            media.item.media_id,
            media_type,
            media.item.source,
            season_number=media.item.season_number,
        ),
    )
    media.repeats = len(entries)
    media.added_at = min(entry.created_at for entry in entries)
    media.completed_entries = sum(
        entry.status == Status.COMPLETED.value for entry in entries
    )

    # A new item's release is fetched in the background, so keep the card's year
    if request.POST.get("release_year"):
        media.item.release_year = request.POST["release_year"]

    # `title` and `active` are display state that only the posted card knows
    context = {
        "item": media.item,
        "media": media,
        "title": request.POST.get("title") or media.item,
        "filtered": "status" in request.POST,
        "active": request.POST.get("active") == "true",
    }
    if request.POST.get("card") == "row":
        context["sorted_field"] = _annotate_sorted_values(
            [media],
            media_type,
            request.POST.get("sort"),
            request.user,
        )
        context["media_type"] = media_type
        context["target_user"] = request.user
        context["show_progress"] = media_type not in UNPROGRESSED_MEDIA_TYPES
        template_name = "app/components/media_row_response.html"
    else:
        template_name = "app/components/media_card_response.html"
    response = render(request, template_name, context)
    # A library's status counts change with the entry
    if context["filtered"]:
        response["HX-Trigger"] = "libraryChanged"
    return response


def _matches_status_filter(media, status_filter):
    """Return whether an entry still belongs in a list filtered to a status.

    Caught up media is in progress or repeated, which it may stop being.
    """
    if status_filter == LibraryStatusChoices.CAUGHT_UP:
        return media.status in (Status.IN_PROGRESS.value, Status.REPEATING.value)
    return status_filter in {None, "", MediaStatusChoices.ALL, media.status}


def _get_or_create_item(media_id, source, media_type, season_number):
    """Return the item of a media, fetching its metadata only if it is new.

    Looks up an existing item before calling the metadata provider.
    """
    item = Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=media_type,
        season_number=season_number,
        episode_number=None,
    ).first()
    if item:
        return item

    metadata = services.get_media_metadata(
        media_type,
        media_id,
        source,
        [season_number],
    )
    item, _ = Item.objects.get_or_create(
        media_id=media_id,
        source=source,
        media_type=media_type,
        season_number=season_number,
        defaults={
            "title": metadata["title"],
            "image": metadata["image"],
        },
    )
    return item


@require_POST
def media_delete(request):
    """Delete media data from the database."""
    instance_id = request.POST["instance_id"]
    media_type = request.POST["media_type"]
    media = helpers.get_owned_media_or_404(request, media_type, instance_id)
    media.delete()
    logger.info("%s deleted successfully.", media)

    return helpers.redirect_back(request)


@require_POST
def mark_user_messages_shown(request):
    """Mark all unseen persistent messages for the user as shown."""
    message_ids = [
        int(message_id)
        for message_id in request.POST.getlist("message_ids")
        if message_id.isdigit()
    ]
    if not message_ids:
        return HttpResponse(status=204)

    UserMessage.objects.filter(
        id__in=message_ids,
        user=request.user,
        shown_at__isnull=True,
    ).update(shown_at=timezone.now())
    return HttpResponse(status=204)


@require_POST
def episode_save(request):
    """Handle the creation, deletion, and updating of episodes for a season."""
    media_id = request.POST["media_id"]
    season_number = int(request.POST["season_number"])
    episode_number = int(request.POST["episode_number"])
    source = request.POST["source"]

    if request.POST.get("quick"):
        # One click marks it watched now, like a season's next episode action
        end_date = timezone.now().replace(second=0, microsecond=0)
        notes = ""
    else:
        form = EpisodeForm(request.POST)
        if not form.is_valid():
            logger.error("Form validation failed: %s", form.errors)
            return HttpResponseBadRequest("Invalid form data")
        end_date = form.cleaned_data["end_date"]
        notes = form.cleaned_data["notes"]

    try:
        related_season = Season.objects.get(
            item__media_id=media_id,
            item__source=source,
            item__season_number=season_number,
            item__episode_number=None,
            user=request.user,
        )
    except Season.DoesNotExist:
        tv_with_seasons_metadata = services.get_media_metadata(
            "tv_with_seasons",
            media_id,
            source,
            [season_number],
        )
        season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_with_seasons_metadata["title"],
                "image": season_metadata["image"],
            },
        )
        related_season = Season.objects.create(
            item=item,
            user=request.user,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
        )

        logger.info("%s did not exist, it was created successfully.", related_season)

    # One click is only offered for an unwatched episode, so a second one, e.g.
    # while the page reloads, doesn't add another watch
    # While the season is rewatched, that's an episode not watched again yet
    already_watched = Episode.objects.filter(
        related_season=related_season,
        item__episode_number=episode_number,
    )
    if episode_number in related_season.planned_repeat_episodes:
        already_watched = already_watched.none()
    elif related_season.status in REPEAT_STATUSES and related_season.repeat_started_at:
        already_watched = already_watched.filter(
            created_at__gte=related_season.repeat_started_at,
        )
    if not (request.POST.get("quick") and already_watched.exists()):
        related_season.watch(episode_number, end_date, notes)

    return helpers.redirect_back(request)


@require_POST
def episode_plan_repeat(request):
    """Plan to watch a watched episode of the user's season again on its own."""
    season = get_object_or_404(
        Season,
        item__media_id=request.POST["media_id"],
        item__source=request.POST["source"],
        item__season_number=int(request.POST["season_number"]),
        user=request.user,
    )
    episode_number = int(request.POST["episode_number"])
    watched = season.episodes.filter(item__episode_number=episode_number).exists()
    if watched and episode_number not in season.planned_repeat_episodes:
        season.planned_repeat_episodes.append(episode_number)
        Season.objects.filter(pk=season.pk).update(
            planned_repeat_episodes=season.planned_repeat_episodes,
        )
        logger.info("Planned to rewatch %s E%s", season, episode_number)
    return helpers.redirect_back(request)


@require_http_methods(["GET", "POST"])
def create_entry(request):
    """Return the form for manually adding media items."""
    if request.method == "GET":
        media_types = MediaTypes.values
        return render(request, "app/create_entry.html", {"media_types": media_types})

    # Process the form submission
    form = ManualItemForm(request.POST, user=request.user)
    if not form.is_valid():
        # Handle form validation errors
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)
        return redirect("create_entry")

    # Try to save the item
    try:
        item = form.save()
    except IntegrityError:
        # Handle duplicate item
        media_name = form.cleaned_data["title"]
        if form.cleaned_data.get("season_number"):
            media_name += f" - Season {form.cleaned_data['season_number']}"
        if form.cleaned_data.get("episode_number"):
            media_name += f" - Episode {form.cleaned_data['episode_number']}"

        logger.exception("%s already exists in the database.", media_name)
        messages.error(request, f"{media_name} already exists in the database.")
        return redirect("create_entry")

    # Prepare and validate the media form
    updated_request = request.POST.copy()
    updated_request.update({"source": item.source, "media_id": item.media_id})
    media_form = get_form_class(item.media_type)(updated_request)

    if not media_form.is_valid():
        # Handle media form validation errors
        logger.error(media_form.errors.as_json())
        helpers.form_error_messages(media_form, request)

        # Delete the item since the media creation failed
        item.delete()
        logger.info("%s was deleted due to media form validation failure", item)
        return redirect("create_entry")

    # Save the media instance
    media_form.instance.user = request.user
    media_form.instance.item = item

    # Handle relationships based on media type
    if item.media_type == MediaTypes.SEASON.value:
        media_form.instance.related_tv = form.cleaned_data["parent_tv"]
    elif item.media_type == MediaTypes.EPISODE.value:
        media_form.instance.related_season = form.cleaned_data["parent_season"]

    media_form.save()

    # Success message
    msg = f"{item} added successfully."
    messages.success(request, msg)
    logger.info(msg)

    return redirect("create_entry")


@require_GET
def search_parent_tv(request):
    """Return the search results for parent TV shows."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for TV shows with query: %s",
        request.user.username,
        query,
    )

    parent_tvs = TV.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.TV.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_tv.html",
        {"results": parent_tvs, "query": query},
    )


@require_GET
def search_parent_season(request):
    """Return the search results for parent seasons."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for seasons with query: %s",
        request.user.username,
        query,
    )

    parent_seasons = Season.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.SEASON.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_season.html",
        {"results": parent_seasons, "query": query},
    )


@require_GET
def history_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the history page for a media item."""
    user_medias = BasicMedia.objects.filter_media(
        request.user,
        media_id,
        media_type,
        source,
        season_number=season_number,
        episode_number=episode_number,
    )

    total_medias = user_medias.count()
    timeline_entries = []
    for index, media in enumerate(user_medias, start=1):
        if history := media.history.all():
            media_entry_number = total_medias - index + 1
            timeline_entries.extend(
                history_processor.process_history_entries(
                    history,
                    media_type,
                    media_entry_number,
                    request.user,
                ),
            )
    return render(
        request,
        "app/components/fill_history.html",
        {
            "media_type": media_type,
            "timeline": timeline_entries,
            "total_medias": total_medias,
            "return_url": request.GET["return_url"],
        },
    )


@require_http_methods(["DELETE"])
def delete_history_record(request, media_type, history_id):
    """Delete a specific history record."""
    try:
        historical_model = apps.get_model(
            app_label="app",
            model_name=f"historical{media_type.lower()}",
        )

        historical_model.objects.get(
            history_id=history_id,
            history_user=request.user,
        ).delete()

        logger.info(
            "Deleted history record %s",
            str(history_id),
        )

        # Return empty 200 response - the element will be removed by HTMX
        return HttpResponse()

    except historical_model.DoesNotExist:
        logger.exception(
            "History record %s not found for user %s",
            str(history_id),
            str(request.user),
        )
        return HttpResponse("Record not found", status=404)


@require_GET
def statistics(request):
    """Return the statistics page."""
    start_date, end_date = stats.parse_activity_date_range(request)

    # Get all user media data in a single operation
    user_media, media_count = stats.get_user_media(
        request.user,
        start_date,
        end_date,
    )

    # Calculate all statistics from the retrieved data
    media_type_distribution = stats.get_media_type_distribution(
        media_count,
    )
    score_distribution, top_rated = stats.get_score_distribution(user_media)
    status_distribution = stats.get_status_distribution(user_media)
    status_pie_chart_data = stats.get_status_pie_chart_data(
        status_distribution,
    )
    consumption_stats = stats.get_consumption_stats(user_media, media_count)

    total = media_count["total"]
    in_progress_count = stats.get_status_total(
        status_distribution,
        Status.IN_PROGRESS.value,
    )
    rated_percent = (
        round(score_distribution["total_scored"] / total * 100) if total else None
    )

    context = {
        "start_date": start_date,
        "end_date": end_date,
        "media_count": media_count,
        "media_type_distribution": media_type_distribution,
        "score_distribution": score_distribution,
        "top_rated": top_rated,
        "status_distribution": status_distribution,
        "status_pie_chart_data": status_pie_chart_data,
        "consumption_stats": consumption_stats,
        "in_progress_count": in_progress_count,
        "rated_percent": rated_percent,
        "date_format_values": DateFormatChoices.values,
    }

    return render(request, "app/statistics.html", context)


@require_GET
def journal(request):
    """Return the journal page: a global feed of the user's tracking activity."""
    start_date, end_date = stats.parse_activity_date_range(request)

    items_per_page = 20
    # Keyset pagination: the cursor points just past the previous page's last
    # row, so each request reads at most one page per media type regardless of
    # scroll depth (never re-scanning everything above the current page).
    cursor = history_processor.parse_journal_cursor(request)
    page_rows, has_next = history_processor.get_journal_page(
        request.user,
        start_date,
        end_date,
        limit=items_per_page,
        cursor=cursor,
    )
    entries = history_processor.build_journal_entries(page_rows, request.user)
    journal_days = history_processor.build_journal_days(entries, request.user)

    # Preserve the active date range when the feed paginates via HTMX.
    date_params = {
        key: request.GET[key]
        for key in ("start-date", "end-date")
        if key in request.GET
    }

    # Cursor for the next page: the last row rendered on this one.
    next_params = dict(date_params)
    if page_rows:
        last_date, last_type, last_id = page_rows[-1]
        next_params["cursor_date"] = last_date.isoformat()
        next_params["cursor_type"] = last_type
        next_params["cursor_id"] = last_id

    prev_day = request.GET.get("last_day", "")

    context = {
        "entries": entries,
        "journal_days": journal_days,
        # The previous page's last day, so a day split across pages isn't
        # relabelled; the last day on this page, forwarded to the next page.
        # Falls back to prev_day when this page rendered no days, so a day that
        # spans an all-filtered page isn't shown twice.
        "prev_day": prev_day,
        "last_day": journal_days[-1]["day_iso"] if journal_days else prev_day,
        "has_next": has_next,
        "next_query": urlencode(next_params),
        "filter_query": urlencode(date_params),
        "start_date": start_date,
        "end_date": end_date,
    }

    # The activity dashboard only appears on the full page, so skip its queries
    # on the HTMX partial requests that load additional feed pages. Soft
    # navigations (body swaps) still need the full page.
    if request.headers.get("HX-Request") and not request.headers.get(
        "X-Soft-Navigation"
    ):
        return render(request, "app/components/journal_items.html", context)

    context.update(
        {
            "activity_data": stats.get_activity_data(
                request.user,
                start_date,
                end_date,
            ),
            "activity_total": history_processor.get_journal_count(
                request.user,
                start_date,
                end_date,
            ),
            "date_format_values": DateFormatChoices.values,
        },
    )
    return render(request, "app/journal.html", context)


@require_GET
def service_worker():
    """Serve the service worker file."""
    sw_path = Path(settings.STATICFILES_DIRS[0]) / "js" / "serviceworker.js"
    with sw_path.open() as f:
        response = HttpResponse(f.read(), content_type="application/javascript")
        response["Service-Worker-Allowed"] = "/"
        return response
