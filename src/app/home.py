from django.utils.text import slugify

from app.models import BasicMedia, Status, is_caught_up
from users.models import HomeSortChoices

# The statuses of the home page's rows, in the order they are shown
HOME_STATUSES = (
    Status.IN_PROGRESS.value,
    Status.REPEATING.value,
    Status.PLANNING.value,
    Status.PLANNING_REPEAT.value,
)


def build_home_rows(user, items_limit):
    """Return the home page's rows, one per status and media type with entries.

    Each row lists the most recently active entries first, at most `items_limit`.
    """
    rows = []
    for status in HOME_STATUSES:
        media_types = BasicMedia.objects.get_home_status(
            user=user,
            status=status,
            sort_by=HomeSortChoices.RECENT,
            items_limit=items_limit,
        )
        for media_type, media_list in media_types.items():
            rows.append(
                {
                    "id": f"{slugify(status)}-{media_type}",
                    "status": status,
                    "media_type": media_type,
                    **media_list,
                },
            )
    return rows


def is_unreleased_home_media(media, section_key):
    """Return True when a home card is waiting on its next release."""
    if section_key in (Status.IN_PROGRESS.value, Status.REPEATING.value):
        return is_caught_up(media)
    return bool(media.next_event and not media.next_event.is_max_datetime)
