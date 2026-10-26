from datetime import UTC, date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse

from app import library_fields
from app.models import (
    TV,
    BoardGame,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)

TODAY = date(2026, 10, 26)
DASH = "\u2013"


class LibraryControlsTests(TestCase):
    """Test the panel's sliders, date presets and player chips."""

    def setUp(self):
        """Create a user with two movies, and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.short = self.create(Movie, "Short", runtime=90, length=None)
        self.long = self.create(Movie, "Long", runtime=165, release_date=TODAY)
        self.url = reverse("medialist", args=["test", MediaTypes.MOVIE.value])

    def create(self, model, title, **fields):
        """Create a completed entry whose item has the given details."""
        item = Item.objects.create(
            media_id=title,
            source=Sources.MANUAL.value,
            media_type=model.__name__.lower(),
            title=title,
            image="https://example.com/image.jpg",
            **fields,
        )
        return model.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

    def controls(self, media_type=MediaTypes.MOVIE.value, **params):
        """Return the panel's controls by field key for a media type's list."""
        url = reverse("medialist", args=["test", media_type])
        response = self.client.get(url, params)
        return response, {
            control["key"]: control
            for group in response.context["filter_groups"]
            for control in group["ranges"]
        }

    def test_slider_is_bound_by_the_library(self):
        """A number slider runs from the lowest to the highest value held."""
        Item.objects.filter(pk=self.short.item.pk).update(release_date=date(1999, 5, 1))
        _, controls = self.controls()

        runtime = controls["runtime"]
        self.assertEqual(runtime["control"], "slider")
        self.assertEqual((runtime["floor"], runtime["ceiling"]), ("90", "165"))
        self.assertEqual((runtime["min"], runtime["max"]), ("90", "165"))
        self.assertEqual(runtime["format"], "minutes")
        release = controls["release"]
        self.assertEqual((release["floor"], release["ceiling"]), ("1999", "2026"))
        self.assertEqual(release["format"], "year")

    def test_fields_without_a_range_to_slide_have_no_slider(self):
        """A field the library holds no value, or only one value, for is left out."""
        _, controls = self.controls()

        # Both movies' runtimes differ, but only one has a release year
        self.assertNotIn("release", controls)
        book = reverse("medialist", args=["test", MediaTypes.BOOK.value])
        response = self.client.get(book)
        self.assertNotIn(
            "length",
            [c["key"] for g in response.context["filter_groups"] for c in g["ranges"]],
        )

    def test_rating_sliders_have_fixed_scales(self):
        """Ratings run over their own scale whatever the library holds."""
        _, controls = self.controls()

        for key, scale, step in (
            ("score", ("0", "10"), "0.5"),
            ("provider_score", ("0", "10"), "0.1"),
            ("imdb", ("0", "10"), "0.1"),
            ("rotten_tomatoes", ("0", "100"), "1"),
            ("metacritic", ("0", "100"), "1"),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    (controls[key]["floor"], controls[key]["ceiling"]),
                    scale,
                )
                self.assertEqual(controls[key]["step"], step)
        self.assertTrue(controls["score"]["unset"])

    def test_address_outside_the_bounds_widens_the_slider(self):
        """A value in the address stays on the slider, whatever the library holds."""
        _, controls = self.controls(runtime_min="30", runtime_max="400")

        runtime = controls["runtime"]
        self.assertEqual((runtime["floor"], runtime["ceiling"]), ("30", "400"))
        self.assertEqual((runtime["min"], runtime["max"]), ("30", "400"))

    def test_times_finished_start_at_zero(self):
        """Times finished slides from 0 to the most any title was finished."""
        Movie.objects.create(
            item=self.short.item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        _, controls = self.controls()

        self.assertEqual(
            (controls["plays"]["floor"], controls["plays"]["ceiling"]),
            ("0", "2"),
        )

    def test_full_range_sends_no_params(self):
        """Handles at the ends of the scale leave their params empty."""
        response, _ = self.controls()

        html = response.content.decode()
        for name in ("runtime_min", "runtime_max", "score_min", "score_max"):
            self.assertRegex(html, rf'name="{name}"\s+value=""')

    def test_slider_inputs_hold_the_range_asked_for(self):
        """Only the end of the scale that was moved has a value."""
        response, controls = self.controls(runtime_min="100")

        html = response.content.decode()
        self.assertRegex(html, r'name="runtime_min"\s+value="100"')
        self.assertRegex(html, r'name="runtime_max"\s+value=""')
        self.assertEqual(controls["runtime"]["min"], "100")

    def test_existing_range_params_still_filter(self):
        """Minimum and maximum params give the same results and pills."""
        response = self.client.get(
            self.url, {"runtime_min": "100", "runtime_max": "200"}
        )

        self.assertEqual(
            [media.item.title for media in response.context["media_list"]],
            ["Long"],
        )
        self.assertEqual(
            [pill["label"] for pill in response.context["active_filters"]],
            [f"Runtime 1h 40m{DASH}3h 20m"],
        )

    def parse(self, query):
        """Return the movie filters a query string asks for."""
        return library_fields.parse_filters(MediaTypes.MOVIE.value, QueryDict(query))

    def test_date_presets_count_back_from_today(self):
        """Each preset is a minimum date as of today."""
        for preset, start in (
            ("7d", date(2026, 10, 19)),
            ("30d", date(2026, 9, 26)),
            ("90d", date(2026, 7, 28)),
            ("365d", date(2025, 10, 26)),
            ("year", date(2026, 1, 1)),
        ):
            with (
                self.subTest(preset=preset),
                patch("app.library_fields.timezone.localdate", return_value=TODAY),
            ):
                flt = self.parse(f"added_within={preset}")["added"]

                self.assertEqual(flt.min, start)
                self.assertIsNone(flt.max)

    def test_unknown_preset_is_ignored(self):
        """A preset that isn't offered, or on a field that isn't a date, is dropped."""
        self.assertEqual(self.parse("added_within=forever&runtime_within=7d"), {})

    def test_preset_filters_the_list(self):
        """A preset keeps the media whose date is within it."""
        Movie.objects.filter(pk=self.short.pk).update(
            end_date=datetime(2026, 10, 20, tzinfo=UTC),
        )
        Movie.objects.filter(pk=self.long.pk).update(
            end_date=datetime(2026, 8, 1, tzinfo=UTC),
        )

        with patch("app.library_fields.timezone.localdate", return_value=TODAY):
            week = self.client.get(self.url, {"end_date_within": "7d"})
            year = self.client.get(self.url, {"end_date_within": "year"})
            ignored = self.client.get(self.url, {"end_date_within": "bad"})

        titles = [
            [media.item.title for media in response.context["media_list"]]
            for response in (week, year, ignored)
        ]
        self.assertEqual(titles[0], ["Short"])
        self.assertCountEqual(titles[1], ["Short", "Long"])
        self.assertCountEqual(titles[2], ["Short", "Long"])
        self.assertEqual(ignored.context["active_filters"], [])

    def test_preset_pills_say_what_was_done(self):
        """Each date field's pill reads in its own words and removes the preset."""
        for key, text in (
            ("added", "Added in the past month"),
            ("recent", "Active in the past month"),
            ("start_date", "Started in the past month"),
            ("end_date", "Ended in the past month"),
        ):
            with self.subTest(key=key):
                response = self.client.get(self.url, {f"{key}_within": "30d"})

                (pill,) = response.context["active_filters"]
                self.assertEqual(pill["label"], text)
                self.assertEqual(
                    pill["params"],
                    [f"{key}_min", f"{key}_max", f"{key}_within"],
                )
        response = self.client.get(self.url, {"added_within": "year"})
        self.assertEqual(
            response.context["active_filters"][0]["label"], "Added this year"
        )

    def test_preset_chip_is_chosen_from_the_address(self):
        """The date control opens on the preset or the custom dates asked for."""
        _, controls = self.controls(added_within="7d")
        self.assertEqual(controls["added"]["mode"], "7d")

        _, controls = self.controls(added_min="2024-01-01")
        self.assertEqual(controls["added"]["mode"], "custom")
        self.assertEqual(controls["added"]["inputs"][0]["value"], "2024-01-01")

        _, controls = self.controls()
        self.assertEqual(controls["added"]["mode"], "")
        self.assertEqual(
            [preset["label"] for preset in controls["added"]["presets"]],
            ["Past week", "Past month", "Past 3 months", "Past year", "This year"],
        )

    def test_players_chips_fit_a_count(self):
        """A chip is players_min and players_max both its count, 6+ only the minimum."""
        self.create(BoardGame, "Duo", min_players=2, max_players=2)
        self.create(BoardGame, "Crowd", min_players=4, max_players=8)
        url = reverse("medialist", args=["test", MediaTypes.BOARDGAME.value])

        def titles(**params):
            """Return the titles listed for the params, and the response."""
            response = self.client.get(url, params)
            return {m.item.title for m in response.context["media_list"]}, response

        self.assertEqual(titles(players_min="2", players_max="2")[0], {"Duo"})
        self.assertEqual(titles(players_min="4", players_max="4")[0], {"Crowd"})
        self.assertEqual(titles(players_min="6")[0], {"Crowd"})

    def test_players_pills(self):
        """Pills say how many players a game fits, whichever way it was asked."""
        url = reverse("medialist", args=["test", MediaTypes.BOARDGAME.value])

        def label(**params):
            """Return the label of the first filter pill for the params."""
            response = self.client.get(url, params)
            return response.context["active_filters"][0]["label"]

        self.assertEqual(label(players_min="4", players_max="4"), "Fits 4 players")
        self.assertEqual(label(players_min="1", players_max="1"), "Fits 1 player")
        self.assertEqual(label(players_min="6"), "Fits 6+ players")
        self.assertEqual(
            label(players_min="2", players_max="4"), f"Players 2 players{DASH}4 players"
        )
        self.assertEqual(label(players_min="2"), "Players ≥ 2 players")

    def test_players_chips_keep_other_ranges(self):
        """A range the chips can't show stays in the form."""
        url = reverse("medialist", args=["test", MediaTypes.BOARDGAME.value])
        response = self.client.get(url, {"players_min": "2", "players_max": "4"})

        html = response.content.decode()
        self.assertRegex(html, r'name="players_min"\s+value="2"')
        self.assertRegex(html, r'name="players_max"\s+value="4"')
        self.assertContains(response, "playerChips('2', '4')")
        self.assertContains(response, "6+")

    def test_many_genres_get_a_search(self):
        """The genre chips have a search box once there are more than twelve."""
        response = self.client.get(self.url)
        self.assertNotContains(response, "Search genre")

        self.create(
            Movie,
            "Varied",
            genres="|" + "|".join(f"Genre{n}" for n in range(13)) + "|",
        )
        response = self.client.get(self.url)
        self.assertContains(response, 'aria-label="Search genre"')

    def watch_show(self, user, watches):
        """Create a show for a user whose episodes were watched `watches` times each."""
        item = Item.objects.create(
            media_id=f"show{user.pk}",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Show",
            image="https://example.com/image.jpg",
        )
        show = TV.objects.create(item=item, user=user, status=Status.COMPLETED.value)
        season_item = Item.objects.create(
            media_id=f"show{user.pk}",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Show",
            image="https://example.com/image.jpg",
            season_number=1,
        )
        season = Season.objects.create(item=season_item, related_tv=show, user=user)
        for number, count in enumerate(watches, start=1):
            episode_item = Item.objects.create(
                media_id=f"show{user.pk}",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.EPISODE.value,
                title="Show",
                image="https://example.com/image.jpg",
                season_number=1,
                episode_number=number,
            )
            for day in range(1, count + 1):
                Episode.objects.create(
                    item=episode_item,
                    related_season=season,
                    end_date=datetime(2024, 3, day, tzinfo=UTC),
                )

    def test_show_times_finished_bound_is_the_most_a_show_was_finished(self):
        """The bound is of the least watched episodes, and of this user alone."""
        # Plays 2, though one episode was watched 5 times
        self.watch_show(self.user, [5, 2])
        other = get_user_model().objects.create_user(
            **{**self.credentials, "username": "other"}
        )
        self.watch_show(other, [9])

        _, controls = self.controls(MediaTypes.TV.value)

        plays = controls["plays"]
        self.assertEqual((plays["floor"], plays["ceiling"]), ("0", "2"))

    def test_bounds_asked_for_at_the_end_of_the_scale_are_kept(self):
        """A bound the address puts at an end of the scale stays in its input."""
        response, _ = self.controls(
            imdb_min="0",
            runtime_max="165",
        )

        html = response.content.decode()
        self.assertRegex(html, r'name="imdb_min"\s+value="0"')
        self.assertRegex(html, r'name="runtime_max"\s+value="165"')
        self.assertRegex(html, r'name="imdb_max"\s+value=""')
        self.assertContains(response, "data-keep-min", count=1)
        self.assertContains(response, "data-keep-max", count=1)

    def test_slider_handles_are_named_by_field(self):
        """Each handle reads as the field, then its end."""
        response, _ = self.controls()

        self.assertContains(response, 'aria-label="Runtime, minimum"')
        self.assertContains(response, 'aria-label="Your rating, maximum, exact"')
