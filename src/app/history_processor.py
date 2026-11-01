from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlencode

from django.apps import apps
from django.db.models import Max, Min, Q
from django.template.defaultfilters import pluralize
from django.utils import formats, timezone
from django.utils.dateparse import parse_datetime

from app import config, helpers
from app.models import (
    REPEAT_STATUSES,
    WATCH_STATUS,
    BasicMedia,
    Episode,
    MediaTypes,
    Season,
    Status,
)
from app.templatetags import app_tags

# Rapid edits of one field fold into one entry when each is this close to the last.
COLLAPSE_WINDOW = timedelta(minutes=10)


def _journal_queryset(model, user, start_date, end_date, object_ids=None):
    """Build the base non-deletion history queryset for a media type.

    ``object_ids`` limits it to the history of those objects.
    """
    queryset = model.objects.filter(history_user_id=user).exclude(history_type="-")
    if object_ids is not None:
        queryset = queryset.filter(id__in=object_ids)
    if start_date:
        queryset = queryset.filter(history_date__gte=start_date)
    if end_date:
        queryset = queryset.filter(history_date__lte=end_date)
    return queryset


def get_journal_page(
    user,
    start_date=None,
    end_date=None,
    limit=20,
    cursor=None,
    scope=None,
):
    """Return one keyset page of the user's non-deletion history, newest first.

    Returns ``(rows, has_next)`` where each row is a
    ``(history_date, media_type, history_id)`` tuple, merged across the separate
    ``historical<media_type>`` tables. Rows follow a *total* order —
    ``(history_date, history_id, media_type)`` descending — so records that share
    a ``history_date`` (e.g. a bulk import stamps a whole batch with one
    timestamp) never reshuffle between pages.

    ``cursor`` is the last row of the previous page (or ``None`` for the first);
    only rows strictly older than it are returned. This is keyset pagination:
    each request reads at most ``limit + 1`` rows per media type regardless of
    how deep the feed is scrolled, instead of re-scanning everything above the
    current page. ``start_date``/``end_date`` are optional aware datetimes.

    ``scope`` (see :func:`activity_scope`) limits the feed to those objects'
    history and reads only their tables.
    """
    index = []
    for media_type, queryset in _journal_querysets(user, start_date, end_date, scope):
        if cursor is not None:
            cur_date, cur_type, cur_id = cursor
            cond = Q(history_date__lt=cur_date) | Q(
                history_date=cur_date,
                history_id__lt=cur_id,
            )
            # media_type is constant per table, so the third-key tiebreak is an
            # all-or-nothing condition on this table's rows sharing the cursor's
            # (date, id).
            if media_type < cur_type:
                cond |= Q(history_date=cur_date, history_id=cur_id)
            queryset = queryset.filter(cond)  # noqa: PLW2901

        rows = queryset.order_by("-history_date", "-history_id").values_list(
            "history_date",
            "history_id",
        )[: limit + 1]
        for history_date, history_id in rows:
            index.append((history_date, media_type, history_id))

    # Sort key mirrors the keyset order: (history_date, history_id, media_type).
    index.sort(key=lambda entry: (entry[0], entry[2], entry[1]), reverse=True)
    return index[:limit], len(index) > limit


def get_journal_count(user, start_date=None, end_date=None):
    """Count the user's non-deletion history rows across all media types."""
    return sum(
        queryset.count()
        for _, queryset in _journal_querysets(user, start_date, end_date, None)
    )


def _journal_querysets(user, start_date, end_date, scope):
    """Yield ``(media_type, queryset)`` for each history table a feed reads.

    Without a scope that is every media type; with one, the scope's non-empty
    types, limited to their objects.
    """
    for media_type in MediaTypes.values:
        if scope is not None and not scope.get(media_type):
            continue
        model = apps.get_model(app_label="app", model_name=f"historical{media_type}")
        yield (
            media_type,
            _journal_queryset(
                model,
                user,
                start_date,
                end_date,
                None if scope is None else scope[media_type],
            ),
        )


def activity_scope(
    user,
    media_type,
    media_id,
    source,
    season_number=None,
    episode_number=None,
):
    """Return the objects whose history one media's feed shows.

    The result maps a media type to a set of object ids, for the ``scope`` of
    :func:`get_journal_page`. It holds the user's entries of the item (every
    run), and for a season its episodes' entries, for a show its seasons' and
    those seasons' episodes'. Only the user's own entries are found.
    """
    entries = BasicMedia.objects.filter_media(
        user,
        media_id,
        media_type,
        source,
        season_number=season_number,
        episode_number=episode_number,
    )
    scope = {media_type: set(entries.values_list("id", flat=True))}
    if media_type == MediaTypes.TV.value:
        scope[MediaTypes.SEASON.value] = set(
            Season.objects.filter(related_tv_id__in=scope[media_type]).values_list(
                "id",
                flat=True,
            ),
        )
    if media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value):
        scope[MediaTypes.EPISODE.value] = set(
            Episode.objects.filter(
                related_season_id__in=scope[MediaTypes.SEASON.value],
            ).values_list("id", flat=True),
        )
    return scope


def activity_summary(user, scope, entries):
    """Summarise a media's activity: first and last activity and times finished.

    ``entries`` are the user's entries of the media (as
    ``filter_media_prefetch`` returns them, so a show's plays read prefetched
    seasons). The dates come from aggregates over the scope, not a page. A
    show's or season's finishes are its plays, as the details page counts them,
    else the completed entries; ``finished`` is 0 when none.
    """
    first = last = None
    for _, queryset in _journal_querysets(user, None, None, scope):
        bounds = queryset.aggregate(first=Min("history_date"), last=Max("history_date"))
        if bounds["first"] is not None:
            first = min(first or bounds["first"], bounds["first"])
            last = max(last or bounds["last"], bounds["last"])

    finished = app_tags.play_count(entries[0], entries) if entries else 0
    return {"first": first, "last": last, "finished": finished}


def parse_journal_cursor(request):
    """Parse the keyset ``cursor_*`` params into a ``(date, media_type, id)``.

    Returns ``None`` (first page) when any part is missing or malformed.
    """
    cursor_date = parse_datetime(request.GET.get("cursor_date") or "")
    cursor_type = request.GET.get("cursor_type")
    try:
        cursor_id = int(request.GET["cursor_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if cursor_date is None or cursor_type not in MediaTypes.values:
        return None
    return (cursor_date, cursor_type, cursor_id)


def build_feed_page(request, user, limit, extra_params=None, **feed):
    """Read, build and group one page of a feed for a view's context.

    ``feed`` goes to :func:`get_journal_page` (dates, ``scope``) and
    ``scope_type`` to :func:`build_journal_entries`. The result has the
    context keys the feed templates read: ``journal_days``, ``has_next``,
    ``next_query`` (the cursor of the page's last row, after ``extra_params``)
    and the day bookkeeping, ``prev_day`` (the last page's final day, so a day
    split across pages isn't relabelled) and ``last_day`` (this page's, falling
    back to ``prev_day`` when it rendered no days, so a day isn't shown twice).
    """
    scope_type = feed.pop("scope_type", None)
    page_rows, has_next = get_journal_page(
        user,
        limit=limit,
        cursor=parse_journal_cursor(request),
        **feed,
    )
    entries = collapse_entries(
        build_journal_entries(page_rows, user, scope_type=scope_type),
        user,
    )
    days = build_journal_days(entries, user)

    next_params = dict(extra_params or {})
    if page_rows:
        last_date, last_type, last_id = page_rows[-1]
        next_params["cursor_date"] = last_date.isoformat()
        next_params["cursor_type"] = last_type
        next_params["cursor_id"] = last_id

    prev_day = request.GET.get("last_day", "")
    return {
        "entries": entries,
        "journal_days": days,
        "prev_day": prev_day,
        "last_day": days[-1]["day_iso"] if days else prev_day,
        "has_next": has_next,
        "next_query": urlencode(next_params),
    }


def build_journal_days(entries, user):
    """Group consecutive same-day entries so the feed can show day separators."""
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    days = []
    for entry in entries:
        day = timezone.localdate(entry["date"])
        if not days or days[-1]["day"] != day:
            if day == today:
                label = "Today"
            elif day == yesterday:
                label = "Yesterday"
            else:
                label = formats.date_format(day, user.date_format)
            days.append(
                {
                    "day": day,
                    "day_iso": day.isoformat(),
                    "label": label,
                    "entries": [],
                },
            )
        days[-1]["entries"].append(entry)
    return days


def build_journal_entries(index_page, user, scope_type=None):
    """Turn a page of the journal index into renderable activity entries.

    ``index_page`` is a page from :func:`get_journal_page`. Historical records,
    their display items and their predecessors are fetched in batches per media
    type, then each record is rendered into human-readable changes.

    Each entry is a dict with ``id`` (history id), ``object_id``, ``date``,
    ``media_type``, ``item``, ``changes``, ``accent`` and ``context``. A change
    has ``description`` (plain text), ``field`` and, where there is one, ``old``
    and ``new``. ``scope_type`` is the media type of the feed's own media: its
    seasons and episodes get a short ``context`` ("S2", "S2E3", "E3"), else it
    is empty.
    """
    records = _fetch_history_records(index_page)
    display = _resolve_display_items(records)
    prev_records = _fetch_prev_records(records)

    entries = []
    for _history_date, media_type, history_id in index_page:
        record = records.get((media_type, history_id))
        if record is None:
            continue
        info = display.get((media_type, record.id))
        if info is None:
            continue

        # A creation ("+") has no previous record; otherwise use the
        # batched predecessor instead of a per-row ``prev_record`` query.
        prev_record = (
            None
            if record.history_type == "+"
            else prev_records.get((media_type, record.history_id))
        )
        if media_type == MediaTypes.EPISODE.value:
            changes = _episode_changes(record, prev_record, info)
        else:
            processed = process_history_entry(
                (record, prev_record),
                media_type,
                user,
            )
            changes = processed["changes"]

        if not changes:
            continue

        entries.append(
            {
                "id": record.history_id,
                "object_id": record.id,
                "date": record.history_date,
                "media_type": media_type,
                "item": info["item"],
                "changes": changes,
                "accent": _entry_accent(changes),
                "context": _context_label(scope_type, media_type, info),
            },
        )
    return entries


def _context_label(scope_type, media_type, info):
    """Name where a record sits under the feed's own media, or ``""``."""
    if scope_type == MediaTypes.TV.value:
        if media_type == MediaTypes.SEASON.value:
            return f"S{info['item'].season_number}"
        if media_type == MediaTypes.EPISODE.value:
            return f"S{info['item'].season_number}E{info['episode_number']}"
    if scope_type == MediaTypes.SEASON.value and media_type == MediaTypes.EPISODE.value:
        return f"E{info['episode_number']}"
    return ""


def collapse_entries(entries, user):
    """Fold rapid edits of one field into single entries.

    ``entries`` are from :func:`build_journal_entries`, newest first. Adjacent
    entries of the same object whose only change is the same field, each within
    ``COLLAPSE_WINDOW`` of the one before, become one entry: the newest, with the
    net change (the oldest ``old`` to the newest ``new``), ``count`` and
    ``members`` (the originals, newest first). Episodes don't fold, their
    wording names the episode. Runs fold within the given page only.
    """
    folded = []
    run = []
    for entry in [*entries, None]:
        if run and not (entry is not None and _continues_run(run[-1], entry)):
            folded.append(run[0] if len(run) == 1 else _fold_run(run, user))
            run = []
        if entry is not None:
            run.append(entry)
    return folded


def _continues_run(previous, entry):
    """Say whether ``entry`` (older) extends a run ending at ``previous``."""
    return (
        entry["media_type"] != MediaTypes.EPISODE.value
        and (entry["media_type"], entry["object_id"])
        == (previous["media_type"], previous["object_id"])
        and len(entry["changes"]) == 1
        and len(previous["changes"]) == 1
        and entry["changes"][0]["field"] == previous["changes"][0]["field"]
        and previous["date"] - entry["date"] <= COLLAPSE_WINDOW
    )


def _fold_run(run, user):
    """Fold a run of entries (newest first) into one with their net change."""
    field = run[0]["changes"][0]["field"]
    old = run[-1]["changes"][0].get("old")
    new = run[0]["changes"][0].get("new")
    media_type = run[0]["media_type"]

    if old == new:
        description = (
            f"Changed {_field_label(field)} {len(run)} times, "
            f"back to {_value_text(field, new, media_type, user)}"
        )
    else:
        description = format_description(field, old, new, media_type, user)
    change = {"description": description, "field": field, "old": old, "new": new}

    return {
        **run[0],
        "changes": [change],
        "accent": _entry_accent([change]),
        "count": len(run),
        "members": run,
    }


def _field_label(field):
    """Return a field's name as words."""
    return "rating" if field == "score" else field.replace("_", " ")


def _value_text(field, value, media_type, user):
    """Return a field value as words, for "back to ..."."""
    if field == "score":
        return "no rating" if value is None else f"{_score_text(value)}/10"
    if field == "status":
        return _status_label(value, media_type)
    if field in ("start_date", "end_date"):
        return app_tags.datetime_format(value, user)
    return str(value)


def _score_text(value):
    """Return a score without a trailing ".0", e.g. 8.0 as "8" and 8.5 as "8.5"."""
    return f"{float(value):g}"


def _entry_accent(changes):
    """Classify an entry by its primary change for iconography and colour.

    Returns a :class:`~app.models.Status` value for status-driven entries, or
    ``"score"``/``"default"`` for the remaining cases (see
    :data:`app.config.JOURNAL_ACCENT_EXTRA`).
    """
    primary = changes[0]
    field = primary.get("field")

    if field == "status":
        return primary.get("new")
    if field == "score":
        return "score"
    if field in ("progress", "start_date"):
        return Status.IN_PROGRESS.value
    if field == "end_date":
        return Status.COMPLETED.value
    return "default"


def _fetch_history_records(index_page):
    """Fetch historical records for a page, keyed by ``(media_type, history_id)``."""
    ids_by_type = defaultdict(list)
    for _history_date, media_type, history_id in index_page:
        ids_by_type[media_type].append(history_id)

    records = {}
    for media_type, history_ids in ids_by_type.items():
        model = apps.get_model(app_label="app", model_name=f"historical{media_type}")
        for record in model.objects.filter(history_id__in=history_ids):
            records[(media_type, record.history_id)] = record
    return records


def _resolve_display_items(records):
    """Resolve the item shown per record, keyed by ``(media_type, object_id)``.

    Episodes are displayed with their parent season's item (poster and link),
    since episodes have no standalone detail page.
    """
    ids_by_type = defaultdict(set)
    for (media_type, _history_id), record in records.items():
        ids_by_type[media_type].add(record.id)

    display = {}
    for media_type, object_ids in ids_by_type.items():
        model = apps.get_model(app_label="app", model_name=media_type)
        if media_type == MediaTypes.EPISODE.value:
            queryset = model.objects.filter(id__in=object_ids).select_related(
                "item",
                "related_season__item",
            )
            for obj in queryset:
                display[(media_type, obj.id)] = {
                    "item": obj.related_season.item,
                    "episode_number": obj.item.episode_number,
                }
        else:
            queryset = model.objects.filter(id__in=object_ids).select_related("item")
            for obj in queryset:
                display[(media_type, obj.id)] = {"item": obj.item}
    return display


def _fetch_prev_records(records):
    """Map each record to its predecessor, batched per media type.

    Mirrors django-simple-history's ``prev_record`` (the newest row for the same
    object with a strictly earlier ``history_date``) but fetches every involved
    object's history in one query per media type instead of one query per row.
    """
    ids_by_type = defaultdict(set)
    for (media_type, _history_id), record in records.items():
        ids_by_type[media_type].add(record.id)

    prev = {}
    for media_type, object_ids in ids_by_type.items():
        model = apps.get_model(app_label="app", model_name=f"historical{media_type}")
        by_object = defaultdict(list)
        history = model.objects.filter(id__in=object_ids).order_by(
            "history_date",
            "history_id",
        )
        for row in history:
            by_object[row.id].append(row)

        for rows in by_object.values():
            # rows ascend by (history_date, history_id); a row's predecessor is
            # the newest earlier row whose history_date is strictly less.
            for i, row in enumerate(rows):
                for candidate in reversed(rows[:i]):
                    if candidate.history_date < row.history_date:
                        prev[(media_type, row.history_id)] = candidate
                        break
    return prev


def _episode_changes(record, prev_record, info):
    """Build a change description for an episode history record.

    An entry created or moved to a status reads as what it now is, e.g.
    "Watched episode 4" or "Planned episode 4". A watch, created as one or
    turned into one from a plan that has no history of its own, is a completion
    of that episode, so it is tagged ``end_date`` to earn the completed accent;
    the others carry their status's. Other changes are treated as progress.
    Notes added with a new entry follow as their own change, like they do for
    other media.
    """
    episode_number = info["episode_number"]
    if prev_record is not None and prev_record.status == record.status:
        description = f"Updated episode {episode_number}"
        return [{"description": description, "field": "progress"}]

    verb = config.get_verb(MediaTypes.EPISODE.value, past_tense=False)
    if record.status == WATCH_STATUS:
        changes = [
            {"description": f"Watched episode {episode_number}", "field": "end_date"},
        ]
    else:
        wording = {
            Status.PLANNING.value: "Planned episode",
            Status.IN_PROGRESS.value: "Started episode",
            Status.PAUSED.value: "Paused episode",
            Status.DROPPED.value: "Dropped episode",
            Status.PLANNING_REPEAT.value: f"Planned re{verb} of episode",
            Status.REPEATING.value: f"Re{verb}ing episode",
        }
        changes = [
            {
                "description": f"{wording[record.status]} {episode_number}",
                "field": "status",
                "new": record.status,
            },
        ]
    if prev_record is None and record.notes:
        changes.append(
            {
                "description": format_description("notes", None, record.notes),
                "field": "notes",
            },
        )
    return changes


def process_history_entry(entry, media_type, user):
    """Process a single history entry to organize and format changes."""
    new_record, old_record = entry
    processed_entry = {
        "id": new_record.history_id,
        "date": new_record.history_date,
        "changes": [],
    }

    if old_record is not None:
        return process_changed_entry(
            new_record,
            old_record,
            media_type,
            processed_entry,
            user,
        )
    return process_creation_entry(new_record, media_type, processed_entry, user)


def process_changed_entry(new_record, old_record, media_type, processed_entry, user):
    """Process an entry representing a change to existing media."""
    delta = new_record.diff_against(old_record)
    changes = organize_changes(delta.changes, media_type, user)
    apply_date_status_integration(changes, user)
    build_changes_list(changes, processed_entry)
    return processed_entry


def process_creation_entry(new_record, media_type, processed_entry, user):
    """Process an entry representing media creation."""
    history_model = apps.get_model(
        app_label="app",
        model_name=f"historical{media_type}",
    )

    changes = collect_creation_changes(new_record, history_model, media_type, user)
    apply_date_status_integration(changes, user)
    build_changes_list(changes, processed_entry)
    return processed_entry


def organize_changes(changes, media_type, user):
    """Organize changes into categories."""
    organized = {
        "date_changes": {"start_date": None, "end_date": None},
        "status_change": None,
        "other_changes": [],
    }

    end_date_change = None

    for change in changes:
        if change.field == "progress" and media_type == MediaTypes.MOVIE.value:
            continue

        change_data = {
            "description": format_description(
                change.field,
                change.old,
                change.new,
                media_type,
                user,
            ),
            "field": change.field,
            "old": change.old,
            "new": change.new,
        }

        if change.field == "status":
            organized["status_change"] = change_data
        elif change.field == "end_date":
            end_date_change = change_data
        elif change.field in organized["date_changes"]:
            organized["date_changes"][change.field] = change_data
        else:
            organized["other_changes"].append(change_data)

    if end_date_change:
        organized["date_changes"]["end_date"] = end_date_change

    return organized


def collect_creation_changes(new_record, history_model, media_type, user):
    """Collect changes for a creation entry."""
    organized = {
        "date_changes": {"start_date": None, "end_date": None},
        "status_change": None,
        "other_changes": [],
    }

    for field in history_model._meta.get_fields():
        if (
            field.name.startswith("history_")
            or field.name == "id"
            or not hasattr(new_record, field.attname)
            or (field.name == "progress" and media_type == MediaTypes.MOVIE.value)
        ):
            continue

        value = getattr(new_record, field.attname, None)
        if not value and not (
            media_type == MediaTypes.EPISODE.value and field.name == "end_date"
        ):
            continue

        change_data = {
            "field": field.name,
            "old": None,
            "new": value,
            "description": format_description(
                field.name,
                None,
                value,
                media_type,
                user,
            ),
        }

        if field.name == "status":
            organized["status_change"] = change_data
        elif field.name in organized["date_changes"]:
            organized["date_changes"][field.name] = change_data
        elif field.name not in ["item", "user", "related_tv"]:
            organized["other_changes"].append(change_data)

    return organized


def apply_date_status_integration(changes, user):
    """Integrate status changes with date changes where appropriate."""
    date_changes = changes["date_changes"]
    status_change = changes["status_change"]

    # Process start date with status
    if (
        date_changes["start_date"]
        and status_change
        and status_change["new"] == Status.IN_PROGRESS.value
    ):
        date_changes["start_date"]["description"] = (
            f"Started on "
            f"{app_tags.datetime_format(date_changes['start_date']['new'], user)}"
        )
        changes["status_change"] = None

    # Process end date with status
    if (
        date_changes["end_date"]
        and status_change
        and status_change["new"] == Status.COMPLETED.value
    ):
        date_changes["end_date"]["description"] = (
            f"Finished on "
            f"{app_tags.datetime_format(date_changes['end_date']['new'], user)}"
        )
        changes["status_change"] = None


def build_changes_list(changes, processed_entry):
    """Build the final changes list in the desired order."""
    # Add date changes
    if changes["date_changes"]["start_date"]:
        processed_entry["changes"].append(changes["date_changes"]["start_date"])
    if changes["date_changes"]["end_date"]:
        processed_entry["changes"].append(changes["date_changes"]["end_date"])

    # Add status if not integrated with dates
    if changes["status_change"]:
        processed_entry["changes"].append(changes["status_change"])

    # Add other changes
    processed_entry["changes"].extend(changes["other_changes"])


def format_description(field_name, old_value, new_value, media_type=None, user=None):  # noqa: C901, PLR0911, PLR0912
    """Format change description in a human-readable way.

    Provides natural language descriptions for various types of changes,
    taking into account the media type and status transitions.
    """
    if field_name in {"start_date", "end_date"}:
        new_value = app_tags.datetime_format(new_value, user)
        old_value = app_tags.datetime_format(old_value, user)

    # If old_value is None, treat it as an initial setting
    if old_value is None:
        if field_name == "status":
            verb = config.get_verb(media_type, past_tense=False)
            action = "Marked as"
            if new_value == Status.IN_PROGRESS.value:
                return f"{action} currently {verb}ing"
            if new_value == Status.COMPLETED.value:
                return f"{action} finished {verb}ing"
            if new_value == Status.PLANNING.value:
                return f"Added to {verb}ing list"
            if new_value == Status.DROPPED.value:
                return f"{action} dropped"
            if new_value == Status.PAUSED.value:
                return f"{action} paused {verb}ing"
            if new_value in REPEAT_STATUSES:
                return f"{action} {_status_label(new_value, media_type).lower()}"

        if field_name == "score":
            return f"Rated {_score_text(new_value)}/10"

        if field_name == "progress" and media_type:
            verb = config.get_verb(media_type, past_tense=True).title()
            if media_type == MediaTypes.GAME.value:
                return f"{verb} for {helpers.minutes_to_hhmm(new_value)}"
            unit = config.get_unit(media_type, short=False).lower()
            return f"{verb} up to {unit} {new_value}"

        if field_name in ["start_date", "end_date"]:
            field_display = "Started" if field_name == "start_date" else "Finished"
            if new_value:
                return f"{field_display} on {new_value}"
            return f"{field_display} without date"

        if field_name == "notes":
            return "Added notes"

        return f"Set {field_name.replace('_', ' ').lower()} to {new_value}"

    # Regular change (old_value to new_value)
    if field_name == "status":
        verb = config.get_verb(media_type, past_tense=False)
        # Status transitions
        transitions = {
            (
                Status.PLANNING.value,
                Status.IN_PROGRESS.value,
            ): f"Currently {verb}ing",
            (
                Status.IN_PROGRESS.value,
                Status.COMPLETED.value,
            ): f"Finished {verb}ing",
            (
                Status.IN_PROGRESS.value,
                Status.PAUSED.value,
            ): f"Paused {verb}ing",
            (
                Status.PAUSED.value,
                Status.IN_PROGRESS.value,
            ): f"Resumed {verb}ing",
            (
                Status.IN_PROGRESS.value,
                Status.DROPPED.value,
            ): f"Stopped {verb}ing",
        }
        return transitions.get(
            (old_value, new_value),
            f"Changed status from {_status_label(old_value, media_type)} "
            f"to {_status_label(new_value, media_type)}",
        )

    if field_name == "score":
        if new_value is None:
            return "Removed rating"
        if old_value == 0:
            return f"Rated {_score_text(new_value)}/10"
        return (
            f"Changed rating from {_score_text(old_value)} to {_score_text(new_value)}"
        )

    if field_name == "progress":
        diff = new_value - old_value
        diff_abs = abs(diff)

        if media_type == MediaTypes.GAME.value:
            if diff > 0:
                return f"Added {helpers.minutes_to_hhmm(diff_abs)} of playtime"
            return f"Removed {helpers.minutes_to_hhmm(diff_abs)} of playtime"

        unit = (
            f"{config.get_unit(media_type, short=False).lower()}{pluralize(new_value)}"
        )

        return f"Progress set to {new_value} {unit}"

    if field_name in ["start_date", "end_date"]:
        field_display = "Start" if field_name == "start_date" else "End"
        if not new_value:
            return f"Removed {field_display.lower()} date"
        if not old_value:
            return f"{field_display}ed on {new_value}"
        return f"{field_display} date changed to {new_value}"

    if field_name == "notes":
        if not old_value:
            return "Added notes"
        if not new_value:
            return "Removed notes"
        return "Updated notes"

    field_label = field_name.replace("_", " ").lower()
    return f"Updated {field_label} from {old_value} to {new_value}"


def _status_label(status, media_type):
    """Return a status's label in a media type's words, or an unknown one as is."""
    if status in Status.values:
        return config.get_status_label(status, media_type)
    return status
