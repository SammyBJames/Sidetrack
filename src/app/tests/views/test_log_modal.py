import json
import re
from html import unescape
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Book, Game, Item, MediaTypes, Movie, Sources, Status


class LogModalViewTests(TestCase):
    """Test the log dialog and the buttons that open it."""

    def setUp(self):
        """Create a user, log in and add an in progress book and game."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        book_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Test Book",
            image="http://example.com/book.jpg",
            length=300,
        )
        game_item = Item.objects.create(
            media_id="2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/game.jpg",
        )
        with patch(
            "app.providers.services.get_media_metadata",
            return_value={"max_progress": None},
        ):
            self.book = Book.objects.create(
                item=book_item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=120,
            )
            self.game = Game.objects.create(
                item=game_item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=750,
            )

    def modal(self, media, **query):
        """Fetch an entry's log dialog."""
        url = reverse("log_modal", args=[media.item.media_type, media.id])
        return self.client.get(url, query)

    def test_book_dialog_starts_at_the_current_page(self):
        """A book's dialog is filled with its page, its total and the way it ends."""
        response = self.modal(self.book, card="grid")

        self.assertTemplateUsed(response, "app/components/fill_log.html")
        self.assertContains(response, ">Test Book</h2>")
        self.assertContains(response, "I'm on page")
        self.assertContains(response, 'name="page"')
        self.assertContains(response, 'value="120"')
        self.assertContains(response, "of 300")
        self.assertNotContains(response, "marks it")
        self.assertContains(
            response,
            reverse("log_progress", args=["book", self.book.id]),
        )

    def test_game_dialog_shows_the_total_so_far(self):
        """A game's dialog asks for the session and shows the time so far."""
        response = self.modal(self.game)

        self.assertContains(response, ">Test Game</h2>")
        self.assertContains(response, 'name="played"')
        self.assertContains(response, "Total so far: 12h 30min")

    def test_dialog_opens_its_host_once_it_has_arrived(self):
        """The dialog opens itself, so the previous one never shows while it loads."""
        response = self.modal(self.book)

        self.assertContains(response, "x-init=\"$dispatch('dialog-ready')\"")
        self.assertContains(response, 'hx-disabled-elt="find [type=submit]"')

    def test_dialog_carries_the_cards_display_state(self):
        """What the response card needs is posted back with the log."""
        response = self.modal(
            self.book,
            card="row",
            status="In progress",
            sort="title",
            title="Shown title",
            bogus="x",
        )

        self.assertContains(response, 'name="card" value="row"')
        self.assertContains(response, 'name="status" value="In progress"')
        self.assertContains(response, 'name="sort" value="title"')
        self.assertContains(response, 'name="title" value="Shown title"')
        self.assertNotContains(response, "bogus")

    def test_home_tile_log_leaves_its_section_once_finished(self):
        """A home tile's section goes through the dialog, so finishing removes it."""
        home = self.client.get(reverse("home")).content.decode()
        modal_url = reverse("log_modal", args=["book", self.book.id])
        chip = re.search(rf'hx-get="{modal_url}" hx-vals=\'([^\']*)\'', home)
        vals = json.loads(unescape(chip.group(1)))
        self.assertEqual(vals["home_status"], Status.IN_PROGRESS.value)

        dialog = self.client.get(modal_url, vals).content.decode()
        posted = dict(re.findall(r'type="hidden" name="(\w+)" value="([^"]*)"', dialog))
        posted.pop("csrfmiddlewaretoken")
        response = self.client.post(
            reverse("log_progress", args=["book", self.book.id]),
            {**posted, "page": "300"},
        )

        self.assertEqual(response["HX-Reswap"], "delete")

    def test_entry_not_in_progress_is_stale(self):
        """An entry that is not in progress has no dialog."""
        Book.objects.filter(pk=self.book.pk).update(status=Status.COMPLETED.value)

        response = self.modal(self.book)

        self.assertEqual(response["HX-Reswap"], "none")
        self.assertTemplateNotUsed(response, "app/components/fill_log.html")

    def test_other_types_and_other_users_are_refused(self):
        """Only the user's own books and games have the dialog."""
        item = Item.objects.create(
            media_id="3",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="http://example.com/m.jpg",
        )
        movie = Movie.objects.create(item=item, user=self.user)
        self.assertEqual(self.modal(movie).status_code, 400)

        other = {"username": "other", "password": "12345"}
        get_user_model().objects.create_user(**other)
        self.client.login(**other)
        self.assertEqual(self.modal(self.book).status_code, 404)

    def test_book_chip_opens_the_dialog_instead_of_posting(self):
        """In a library, a book in progress's chip fetches the dialog."""
        response = self.client.get(
            reverse("medialist", args=["test", MediaTypes.BOOK.value]),
        )

        content = response.content.decode()
        self.assertIn(reverse("log_modal", args=["book", self.book.id]), content)
        self.assertIn('hx-target="#log-dialog"', content)
        self.assertNotIn('"action": "log"', content)

        # Both layouts send the library's filters with the fetch, so the log's
        # response knows whether the card still belongs in the list
        modal_url = reverse("log_modal", args=["book", self.book.id])
        for layout in ("grid", "table"):
            with self.subTest(layout=layout):
                content = self.client.get(
                    reverse("medialist", args=["test", MediaTypes.BOOK.value]),
                    {"layout": layout},
                ).content.decode()
                chip = re.search(rf'<button[^>]*hx-get="{modal_url}"[^>]*>', content)
                self.assertIn('hx-include="#filter-form"', chip.group(0))

    def test_details_page_has_the_log_and_finish_buttons(self):
        """A book in progress's page offers logging, and marking it read."""
        response = self.client.get(
            reverse(
                "media_details",
                args=[Sources.MANUAL.value, MediaTypes.BOOK.value, "1", "test-book"],
            ),
        )

        content = response.content.decode()
        self.assertIn(reverse("log_modal", args=["book", self.book.id]), content)
        self.assertIn('aria-label="Mark read"', content)
        self.assertIn('id="log-dialog"', content)
