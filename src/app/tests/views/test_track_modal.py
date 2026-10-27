import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.template import loader
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Book,
    Game,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)


class TrackModalViewTests(TestCase):
    """Test the track modal view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        self.movie = Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

    def test_track_modal_view_existing_media(self):
        """Test the track modal view for existing media."""
        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            )
            + "?return_url=/home",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")

        self.assertIn("form", response.context)
        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"], self.movie)
        self.assertEqual(response.context["return_url"], "/home")

    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_view_new_media(self, mock_get_metadata):
        """Test the track modal view for new media."""
        mock_get_metadata.return_value = {
            "media_id": "278",
            "title": "New Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
        }

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "278",
                },
            )
            + "?return_url=/home&title=New+Movie",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")

        self.assertIn("form", response.context)
        self.assertEqual(response.context["form"].initial["media_id"], "278")
        self.assertEqual(
            response.context["form"].initial["media_type"],
            MediaTypes.MOVIE.value,
        )


class TrackModalDialogTests(TestCase):
    """Test the controls the tracking dialog draws for each form."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def get_dialog(self, media_type, media_id="1", source=Sources.MAL.value, **query):
        """Fetch the dialog to add an entry of a type, or edit with `instance_id`."""
        query = {"return_url": "/home", "is_create": "1", **query}
        kwargs = {"source": source, "media_type": media_type, "media_id": media_id}
        if media_type == MediaTypes.SEASON.value:
            kwargs["season_number"] = 1
        with patch("app.providers.services.get_media_metadata") as metadata:
            metadata.return_value = {"title": "Title", "max_progress": 412}
            return self.client.get(reverse("track_modal", kwargs=kwargs), query)

    def test_every_form_has_status_chips_in_the_types_words(self):
        """Each form's status is a radio group, and its chips are named for the type."""
        for media_type in (
            MediaTypes.MOVIE,
            MediaTypes.TV,
            MediaTypes.SEASON,
            MediaTypes.ANIME,
            MediaTypes.MANGA,
            MediaTypes.BOOK,
            MediaTypes.COMIC,
            MediaTypes.BOARDGAME,
            MediaTypes.GAME,
        ):
            with self.subTest(media_type=media_type.value):
                response = self.get_dialog(media_type.value)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'type="radio"', count=7)
                self.assertContains(response, 'name="status"', count=7)
                self.assertContains(response, 'name="score"')
                self.assertContains(response, "$id('fold')")
                self.assertContains(response, ">Advanced<")
                self.assertContains(response, "dialog-sheet")

    def test_status_chips_follow_the_lifecycle(self):
        """Chips run Planning to Dropped, then the repeat statuses."""
        content = self.get_dialog(MediaTypes.BOOK.value).content.decode()
        values = re.findall(r'name="status"\s+value="([^"]+)"', content)

        self.assertEqual(
            values,
            [
                "Planning",
                "In progress",
                "Completed",
                "Paused",
                "Dropped",
                "Repeating",
                "Planning repeat",
            ],
        )

    def test_repeating_chip_uses_the_types_verb(self):
        """The repeat chip says Rereading for a book."""
        response = self.get_dialog(MediaTypes.BOOK.value)

        self.assertContains(response, "Rereading")
        self.assertNotContains(response, ">Repeating<")

    def test_field_sets_follow_the_form(self):
        """Progress and dates show only where the form has them."""
        expected = {
            MediaTypes.MOVIE.value: (False, True),
            MediaTypes.TV.value: (False, False),
            MediaTypes.SEASON.value: (False, False),
            MediaTypes.ANIME.value: (True, True),
        }
        for media_type, (has_progress, has_dates) in expected.items():
            with self.subTest(media_type=media_type):
                response = self.get_dialog(media_type)
                self.assertEqual(
                    'name="progress"' in response.content.decode(),
                    has_progress,
                )
                self.assertEqual(
                    'name="start_date"' in response.content.decode(),
                    has_dates,
                )
                self.assertContains(response, 'name="notes"')

    def test_new_entry_stepper_shows_the_total_from_metadata(self):
        """A new book's stepper names the pages it counts up to."""
        response = self.get_dialog(
            MediaTypes.BOOK.value, source=Sources.OPENLIBRARY.value
        )

        self.assertContains(response, "/ 412 pages")
        self.assertContains(response, "mediaForm(null, 412)")
        self.assertContains(response, "bump(1)")
        self.assertRegex(response.content.decode(), r">\s*Add\s*<")
        self.assertNotContains(response, "Delete")

    def test_edit_shows_total_from_the_item_length_and_the_score(self):
        """An existing book shows Item.length as its total and its score."""
        item = Item.objects.create(
            media_id="b1",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book",
            image="http://example.com/image.jpg",
            length=300,
        )
        with patch("app.providers.services.get_media_metadata") as metadata:
            metadata.return_value = {"max_progress": 300}
            book = Book.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=120,
                score=7.5,
            )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "b1",
                },
            ),
            {"return_url": "/home", "instance_id": book.id},
        )

        self.assertContains(response, "/ 300 pages")
        self.assertContains(response, "mediaForm(7.5, 300)")
        self.assertRegex(response.content.decode(), r'value="In progress"\s+checked')
        self.assertContains(response, 'value="120"')
        self.assertRegex(response.content.decode(), r">\s*Save\s*<")

    def test_edit_without_an_item_length_shows_the_provider_total(self):
        """An existing book with no page count has the same total as its log dialog."""
        item = Item.objects.create(
            media_id="b2",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book",
            image="http://example.com/image.jpg",
        )
        with patch("app.providers.services.get_media_metadata") as metadata:
            metadata.return_value = {"max_progress": 412}
            book = Book.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=120,
            )
            response = self.client.get(
                reverse(
                    "track_modal",
                    kwargs={
                        "source": Sources.OPENLIBRARY.value,
                        "media_type": MediaTypes.BOOK.value,
                        "media_id": "b2",
                    },
                ),
                {"return_url": "/home", "instance_id": book.id},
            )

        self.assertContains(response, "/ 412 pages")
        self.assertContains(response, "mediaForm(null, 412)")

    def test_provider_total_that_is_not_a_count_is_left_out(self):
        """Only a positive whole number from the provider reaches the stepper."""
        kwargs = {
            "source": Sources.OPENLIBRARY.value,
            "media_type": MediaTypes.BOOK.value,
            "media_id": "b3",
        }
        for total in ("1); alert(1", "0", None):
            with (
                self.subTest(total=total),
                patch("app.providers.services.get_media_metadata") as metadata,
            ):
                metadata.return_value = {"title": "Title", "max_progress": total}
                response = self.client.get(
                    reverse("track_modal", kwargs=kwargs),
                    {"return_url": "/home", "is_create": "1"},
                )

                self.assertContains(response, "mediaForm(null, null)")
                self.assertNotContains(response, "alert")

    def test_edit_can_delete_only_after_confirming(self):
        """The delete button of an existing entry asks first and never submits."""
        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            ),
            {"return_url": "/home", "instance_id": self.make_movie().id},
        )

        self.assertContains(response, "hx-confirm=")
        self.assertContains(response, 'aria-label="Delete entry"')
        self.assertContains(response, reverse("media_delete"))
        self.assertNotContains(response, "bump(")

    def make_movie(self):
        """Create a movie entry for the user."""
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="http://example.com/image.jpg",
        )
        return Movie.objects.create(item=item, user=self.user)

    def test_game_keeps_the_duration_text_input_with_half_hour_steps(self):
        """A game's progress is the duration text, stepped by 30 minutes both ways."""
        item = Item.objects.create(
            media_id="g1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Game",
            image="http://example.com/image.jpg",
        )
        with patch("app.providers.services.get_media_metadata") as metadata:
            metadata.return_value = {"max_progress": None}
            game = Game.objects.create(item=item, user=self.user, progress=307)

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "g1",
                },
            ),
            {"return_url": "/home", "instance_id": game.id},
        )

        self.assertContains(response, 'value="5h 07min"')
        self.assertContains(response, "addMinutes(30)")
        self.assertContains(response, "addMinutes(-30)")
        self.assertNotContains(response, "addMinutes(60)")
        self.assertNotContains(response, "Time Played")
        self.assertNotContains(response, "bump(")
        self.assertContains(response, "mediaForm(null, null)")


class DialogEventTests(TestCase):
    """Dialog sheets talk to their hosts through events, not the hosts' flags."""

    def test_sheets_name_no_host_flag(self):
        """The fetched sheets and the header close and open by events only."""
        for name in (
            "app/components/dialog_header.html",
            "app/components/fill_track.html",
            "app/components/fill_log.html",
            "app/components/fill_history.html",
            "lists/components/fill_lists.html",
        ):
            with self.subTest(name=name):
                source = loader.get_template(name).template.source
                self.assertNotRegex(source, r"\w+Open\b|showModal|is_create")

        header = loader.get_template("app/components/dialog_header.html")
        self.assertIn("$dispatch('dialog-dismiss')", header.template.source)
        for name in ("app/components/fill_track.html", "app/components/fill_log.html"):
            self.assertIn(
                "x-init=\"$dispatch('dialog-ready')\"",
                loader.get_template(name).template.source,
            )
