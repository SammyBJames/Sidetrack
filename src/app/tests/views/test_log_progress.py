from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from app import quick_actions
from app.models import Book, Game, Item, MediaTypes, Sources, Status


class LogProgressViewTests(TestCase):
    """Test logging the page of a book or the time played of a game."""

    def setUp(self):
        """Create a user, log in and add a book and a game."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.book_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Test Book",
            image="http://example.com/book.jpg",
            length=300,
        )
        self.game_item = Item.objects.create(
            media_id="2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/game.jpg",
        )
        metadata = patch(
            "app.providers.services.get_media_metadata",
            return_value={"max_progress": None},
        )
        self.metadata = metadata.start()
        self.addCleanup(metadata.stop)

    def book(self, status=Status.IN_PROGRESS.value, **extra):
        """Return a tracked book."""
        return Book.objects.create(
            item=self.book_item,
            user=self.user,
            status=status,
            **extra,
        )

    def game(self, status=Status.IN_PROGRESS.value, **extra):
        """Return a tracked game."""
        return Game.objects.create(
            item=self.game_item,
            user=self.user,
            status=status,
            **extra,
        )

    def log(self, media, **data):
        """Post a log for an entry."""
        url = reverse(
            "log_progress",
            args=[media.item.media_type, media.id],
        )
        return self.client.post(url, data)

    def test_page_below_the_total_only_sets_the_progress(self):
        """Logging a page before the end moves the progress, not the status."""
        book = self.book(progress=10)

        response = self.log(book, page="120")

        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (120, Status.IN_PROGRESS.value))
        self.assertIsNone(book.end_date)
        self.assertContains(response, "media-card")
        self.assertContains(response, "Test Book is now on page 120.")

    def test_page_can_go_back(self):
        """The page is absolute, so it may be lower than before."""
        book = self.book(progress=200)

        self.log(book, page="50")

        book.refresh_from_db()
        self.assertEqual(book.progress, 50)

    def test_last_page_completes_the_book(self):
        """Reaching the total marks it read with an end date, and clamps."""
        book = self.book(progress=10)

        response = self.log(book, page="350")

        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (300, Status.COMPLETED.value))
        self.assertIsNotNone(book.end_date)
        self.assertContains(response, "Test Book marked as read.")

    def test_exact_total_completes_the_book(self):
        """The last page itself finishes the book."""
        book = self.book(progress=10)

        self.log(book, page="300")

        book.refresh_from_db()
        self.assertEqual(book.status, Status.COMPLETED.value)
        self.assertEqual(book.progress, 300)

    def test_completing_keeps_an_existing_end_date(self):
        """An end date that's already set isn't replaced."""
        book = self.book(progress=10, end_date="2020-01-02T00:00:00Z")

        self.log(book, page="300")

        book.refresh_from_db()
        self.assertEqual(book.end_date.year, 2020)

    def test_last_page_of_a_reread_completes_the_play(self):
        """Finishing a repeat is the repeat's completion, like marking it reread."""
        book = self.book(Status.REPEATING.value, progress=10)

        response = self.log(book, page="300")

        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (300, Status.COMPLETED.value))
        self.assertContains(response, "Test Book marked as reread.")

    def test_provider_total_is_the_fallback(self):
        """Without a page count on the item the provider's maximum is the total."""
        self.book_item.length = None
        self.book_item.save()
        self.metadata.return_value = {"max_progress": 250}
        book = self.book(progress=10)

        self.log(book, page="100")
        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (100, Status.IN_PROGRESS.value))

        self.log(book, page="900")
        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (250, Status.COMPLETED.value))

    def test_provider_total_that_is_not_a_count_is_unknown(self):
        """A provider total that isn't a positive whole number is no total."""
        self.book_item.length = None
        self.book_item.save()
        book = self.book()

        self.metadata.return_value = {"max_progress": "250"}
        self.assertEqual(quick_actions.book_total(book), 250)
        self.metadata.return_value = {"max_progress": "1); alert(1"}
        self.assertIsNone(quick_actions.book_total(book))

    def test_unknown_total_only_sets_the_progress(self):
        """With no total anywhere, any page is logged and nothing completes."""
        self.book_item.length = None
        self.book_item.save()
        book = self.book(progress=10)

        self.log(book, page="5000")

        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (5000, Status.IN_PROGRESS.value))

    def test_game_session_is_added_to_the_progress(self):
        """A session's time is added to the minutes played so far."""
        game = self.game(progress=60)

        response = self.log(game, played="1:30")

        game.refresh_from_db()
        self.assertEqual((game.progress, game.status), (150, Status.IN_PROGRESS.value))
        self.assertContains(response, "Logged 1h 30min of Test Game.")

    def test_game_session_formats(self):
        """Sessions take the tracking form's duration formats."""
        game = self.game()
        for played, minutes in (
            ("1.5", 90),
            ("2h 15min", 135),
            ("45min", 45),
            ("2", 120),
        ):
            game.progress = 0
            game.save()
            self.log(game, played=played)
            game.refresh_from_db()
            self.assertEqual(game.progress, minutes, played)

    def test_game_in_a_replay_keeps_its_status(self):
        """Time logged while replaying stays in the replay."""
        game = self.game(Status.REPEATING.value, progress=10)

        self.log(game, played="30min")

        game.refresh_from_db()
        self.assertEqual((game.progress, game.status), (40, Status.REPEATING.value))

    def test_invalid_input_is_rejected(self):
        """Bad pages and durations get a 422 with their errors and change nothing."""
        book = self.book(progress=10)
        game = self.game(progress=10)
        cases = [
            (book, "page", ""),
            (book, "page", "abc"),
            (book, "page", "-1"),
            (book, "page", "1.5"),
            (book, "page", "99999999999"),
            (game, "played", ""),
            (game, "played", "0"),
            (game, "played", "abc"),
            (game, "played", "-30min"),
            (game, "played", "1:75"),
            (game, "played", "25h"),
            (game, "played", "99999999999h"),
        ]
        for media, field, value in cases:
            response = self.log(media, **{field: value})
            self.assertEqual(response.status_code, 422, (field, value))
            self.assertIn(field, response.json()["errors"])
        missing = self.log(book)
        self.assertEqual(missing.status_code, 422)

        book.refresh_from_db()
        game.refresh_from_db()
        self.assertEqual((book.progress, game.progress), (10, 10))

    def test_other_users_entry_is_not_found(self):
        """Entries of another user cannot be logged on."""
        other = get_user_model().objects.create_user(
            username="other", password=self.credentials["password"]
        )
        book = Book.objects.create(
            item=self.book_item,
            user=other,
            status=Status.IN_PROGRESS.value,
            progress=10,
        )

        response = self.log(book, page="20")

        self.assertEqual(response.status_code, 404)
        book.refresh_from_db()
        self.assertEqual(book.progress, 10)

    def test_other_media_types_cannot_log(self):
        """Only books and games log progress."""
        url = reverse("log_progress", args=[MediaTypes.MOVIE.value, 1])
        self.assertEqual(self.client.post(url, {"page": "1"}).status_code, 400)

    def test_entry_not_in_progress_changes_nothing(self):
        """A stale dialog on an entry that's planned or finished changes nothing."""
        for status in (Status.PLANNING.value, Status.COMPLETED.value):
            book = self.book(status, progress=10)

            response = self.log(book, page="20")

            self.assertEqual(response["HX-Reswap"], "none")
            book.refresh_from_db()
            self.assertEqual(book.progress, 10)
            book.delete()

    def test_get_is_not_allowed(self):
        """Logging only takes posts."""
        book = self.book()
        url = reverse("log_progress", args=[MediaTypes.BOOK.value, book.id])
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_details_page_reloads(self):
        """On a details page the log reloads the page, keeping its message."""
        book = self.book(progress=10)

        url = reverse("log_progress", args=[MediaTypes.BOOK.value, book.id])
        response = self.client.post(
            f"{url}?next=/details/page",
            {"page": "20", "card": "details"},
            headers={"HX-Request": "true"},
        )

        self.assertNotIn("HX-Refresh", response)
        self.assertIn('"/details/page"', response["HX-Location"])
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Test Book is now on page 20."],
        )

    def test_row_is_returned_as_a_row(self):
        """A list row gets its updated row back."""
        book = self.book(progress=10)

        response = self.log(book, page="20", card="row")

        self.assertContains(response, "media-row")

    def test_card_leaves_a_list_filtered_to_its_old_status(self):
        """Finishing a book removes its card from the in progress list."""
        book = self.book(progress=10)

        response = self.log(book, page="300", status=Status.IN_PROGRESS.value)

        self.assertEqual(response["HX-Reswap"], "delete")
        self.assertEqual(response["HX-Trigger"], "libraryChanged")

    def test_card_stays_in_its_filtered_list(self):
        """A log that keeps the status keeps the card, and refreshes the counts."""
        book = self.book(progress=10)

        response = self.log(book, page="20", status=Status.IN_PROGRESS.value)

        self.assertNotIn("HX-Reswap", response)
        self.assertContains(response, "media-card")
        self.assertEqual(response["HX-Trigger"], "libraryChanged")

    def test_home_card_is_updated_in_its_section(self):
        """A home card that stays in its section is rendered again."""
        game = self.game(progress=10)

        response = self.log(
            game,
            played="30min",
            card="home",
            home_status=Status.IN_PROGRESS.value,
        )

        self.assertNotIn("HX-Reswap", response)
        self.assertContains(response, f"home-media-game-{game.id}")
        self.assertContains(response, "Logged 30min of Test Game.")

    def test_home_card_is_removed_when_the_book_is_finished(self):
        """A home card leaves its section when the log finishes the book."""
        book = self.book(progress=10)

        response = self.log(
            book,
            page="300",
            card="home",
            home_status=Status.IN_PROGRESS.value,
        )

        self.assertEqual(response["HX-Reswap"], "delete")
        self.assertEqual(response["HX-Retarget"], f"#home-media-book-{book.id}")
        self.assertEqual(response["HX-Trigger"], "mediaChanged")

    def test_log_chip_does_not_apply_as_a_quick_action(self):
        """Posting the log chip to the one-click action changes nothing."""
        book = self.book(progress=10)

        for card in ("grid", "home"):
            response = self.client.post(
                reverse("quick_action"),
                {
                    "media_id": book.item.media_id,
                    "source": book.item.source,
                    "media_type": book.item.media_type,
                    "instance_id": book.id,
                    "action": quick_actions.LOG,
                    "card": card,
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["HX-Reswap"], "none")
        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (10, Status.IN_PROGRESS.value))

    def test_finish_action_completes_a_book_or_game(self):
        """The secondary finish button marks a book or game done as a quick action."""
        book = self.book(progress=10)
        game = self.game(Status.REPEATING.value, progress=10)

        for media, key in (
            (book, quick_actions.COMPLETE),
            (game, quick_actions.REPEAT),
        ):
            self.client.post(
                reverse("quick_action"),
                {
                    "media_id": media.item.media_id,
                    "source": media.item.source,
                    "media_type": media.item.media_type,
                    "instance_id": media.id,
                    "action": key,
                    "finish": "1",
                    "card": "details",
                },
            )
            media.refresh_from_db()
            self.assertEqual(media.status, Status.COMPLETED.value)
            self.assertIsNotNone(media.end_date)
