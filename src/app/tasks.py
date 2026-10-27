import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from app import item_details
from app.models import UserMessage

logger = logging.getLogger(__name__)


@shared_task(name="Cleanup user messages")
def cleanup_user_messages():
    """Delete shown user messages older than the configured retention window."""
    cutoff = timezone.now() - timedelta(days=settings.USER_MESSAGE_RETENTION_DAYS)
    deleted_count, _ = UserMessage.objects.filter(
        shown_at__isnull=False,
        shown_at__lt=cutoff,
    ).delete()

    logger.info("Deleted %s old shown user messages.", deleted_count)

    return deleted_count


@shared_task(name="Refresh library details")
def refresh_library_details():
    """Refresh the stored details of library items, up to the batch size."""
    refreshed, failed = item_details.refresh_library_details(
        settings.LIBRARY_DETAILS_BATCH,
    )
    logger.info("Refreshed %s library items, %s failed.", refreshed, failed)
    return refreshed
