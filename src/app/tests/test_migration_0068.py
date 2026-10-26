import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Book, Item, MediaTypes, Sources, Status

migration_module = importlib.import_module(
    "app.migrations.0068_neutral_repeat_statuses",
)


class NeutralRepeatStatusesTests(TestCase):
    """Data migration 0068 gives the repeat statuses their neutral values."""

    def test_old_values_and_their_history_are_renamed(self):
        """Entries and their history move to the neutral values, and back."""
        user = get_user_model().objects.create_user(username="test")
        item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Book",
        )
        book = Book.objects.create(item=item, user=user, status=Status.COMPLETED.value)
        # The values the first version of the statuses stored
        Book.objects.filter(pk=book.pk).update(status="Rewatching")
        book.history.update(status="Planning rewatch")

        migration_module.forwards(apps, None)

        book.refresh_from_db()
        self.assertEqual(book.status, Status.REPEATING.value)
        self.assertEqual(
            set(book.history.values_list("status", flat=True)),
            {Status.PLANNING_REPEAT.value},
        )

        migration_module.backwards(apps, None)

        book.refresh_from_db()
        self.assertEqual(book.status, "Rewatching")
