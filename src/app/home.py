from django.utils.text import slugify

from app.models import BasicMedia, Status, is_caught_up
from users.models import HomeSortChoices

# The home page's rows, in the order they are shown: a status, or the media in
# progress or repeated that's caught up to its latest release
CAUGHT_UP = "caught-up"
HOME_ROWS = (
    Status.IN_PROGRESS.value,
    Status.REPEATING.value,
    Status.PLANNING.value,
    CAUGHT_UP,
    Status.PLANNING_REPEAT.value,
)


def build_home_rows(user, items_limit):
    """Return the home page's rows, one per status and media type with entries.

    Each row lists the most recently active entries first, at most `items_limit`,
    except the caught up row, which lists the next releases first. Its cards act
    as in progress ones.
    """
    rows = []
    loaded_lists = {}
    for row in HOME_ROWS:
        caught_up = row == CAUGHT_UP
        status = Status.IN_PROGRESS.value if caught_up else row
        media_types = BasicMedia.objects.get_home_status(
            user=user,
            status=status,
            sort_by=HomeSortChoices.UPCOMING if caught_up else HomeSortChoices.RECENT,
            items_limit=items_limit,
            caught_up=caught_up,
            loaded_lists=loaded_lists,
        )
        for media_type, media_list in media_types.items():
            rows.append(
                {
                    "id": f"{slugify(row)}-{media_type}",
                    "status": status,
                    "caught_up": caught_up,
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
