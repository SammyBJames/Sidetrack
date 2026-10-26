from datetime import UTC, datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app import library_fields, quick_actions
from app.models import (
    TV,
    BoardGame,
    Book,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.templatetags import app_tags
from app.views import SORTS_WITH_COLUMNS


class LibraryPanelViewTests(TestCase):
    """Test the sort and filter panel, its pills and the list's sorted value."""

    def setUp(self):
        """Create a user with a movie and a book, and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.movie = self.create(
            Movie,
            MediaTypes.MOVIE,
            "Alpha",
            imdb_score="8.4",
            runtime=125,
            genres="|Drama|",
        )
        self.book = self.create(Book, MediaTypes.BOOK, "Pages", length=412)
        self.url = reverse("medialist", args=["test", MediaTypes.MOVIE.value])

    def create(self, model, media_type, title, **item_fields):
        """Create a completed entry of a media type with its item's details."""
        item = Item.objects.create(
            media_id=title,
            source=Sources.MANUAL.value,
            media_type=media_type.value,
            title=title,
            image="https://example.com/image.jpg",
            **item_fields,
        )
        return model.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

    def test_active_filters_are_pills_with_their_params(self):
        """Each filter is a pill removing its params, a choice's just its value."""
        response = self.client.get(
            self.url,
            {"imdb_min": "7", "genre": ["Drama", "Comedy"]},
        )

        self.assertContains(response, 'data-params="imdb_min,imdb_max"')
        self.assertContains(response, 'data-params="genre"', count=2)
        self.assertContains(response, 'data-value="Drama"')
        self.assertContains(response, 'data-value="Comedy"')
        self.assertContains(response, "IMDb rating ≥ 7")
        self.assertContains(response, 'data-filter-count="2"')

    def test_no_pills_without_filters(self):
        """The pills and their Clear all are left out when nothing is filtered."""
        response = self.client.get(self.url)

        self.assertNotContains(response, "Remove filter")
        self.assertContains(response, 'data-count="1"')
        self.assertContains(response, 'data-filter-count="0"')

    def test_inputs_are_filled_from_the_address(self):
        """The panel's inputs hold the filters asked for, even an unlisted genre."""
        response = self.client.get(
            self.url,
            {"imdb_min": "7", "release_max": "2000", "genre": ["Drama", "Horror"]},
        )

        html = response.content.decode()
        self.assertRegex(html, r'name="imdb_min"\s+value="7"')
        self.assertRegex(html, r'name="release_max"\s+value="2000"')
        self.assertRegex(html, r'name="genre"\s+value="Drama"\s+checked')
        self.assertRegex(html, r'name="genre"\s+value="Horror"\s+checked')
        self.assertNotRegex(html, r'name="imdb_max"\s+value="[^"]')

    def test_legacy_params_fill_the_panel(self):
        """An old rated link shows as the rating range in the panel."""
        response = self.client.get(self.url, {"rated": "unrated"})

        self.assertRegex(
            response.content.decode(),
            r'name="score"\s+value="unrated"\s+checked',
        )

    def test_rows_show_the_sorted_value(self):
        """A row shows the sorted field's value with its unit."""
        for sort, text in (("imdb", "8.4"), ("runtime", "2h 5m")):
            with self.subTest(sort=sort):
                response = self.client.get(self.url, {"sort": sort, "layout": "table"})

                self.assertEqual(response.context["media_list"][0].sorted_value, text)
                self.assertEqual(response.context["sorted_field"].key, sort)
                self.assertContains(response, text)

        book_url = reverse("medialist", args=["test", MediaTypes.BOOK.value])
        response = self.client.get(book_url, {"sort": "length", "layout": "table"})
        self.assertEqual(response.context["media_list"][0].sorted_value, "412 pages")

    def full_details(self, **extra):
        """Return item details for every field a list can sort by."""
        return {
            "release_date": datetime(2001, 5, 6, tzinfo=UTC).date(),
            "provider_score": "7.5",
            "imdb_score": "8.4",
            "rotten_tomatoes": 91,
            "metacritic": 70,
            "runtime": 125,
            "season_count": 3,
            "length": 12,
            "airing_status": "airing",
            "min_players": 2,
            "max_players": 4,
            **extra,
        }

    def show(self, plays):
        """Create a show whose one episode was watched `plays` times."""
        item = Item.objects.create(
            media_id="show",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Show",
            image="https://example.com/image.jpg",
            **self.full_details(),
        )
        show = TV.objects.create(
            item=item, user=self.user, status=Status.COMPLETED.value
        )
        season_item = Item.objects.create(
            media_id="show",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Show",
            image="https://example.com/image.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item, related_tv=show, user=self.user
        )
        episode_item = Item.objects.create(
            media_id="show",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            title="Show",
            image="https://example.com/image.jpg",
            season_number=1,
            episode_number=1,
        )
        for day in range(1, plays + 1):
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=datetime(2024, 3, day, tzinfo=UTC),
            )
        return show

    def test_every_sort_has_a_value_to_show(self):
        """Whatever a list is sorted by, its rows show a value."""
        Movie.objects.all().delete()
        for model, media_type in (
            (Movie, MediaTypes.MOVIE),
            (BoardGame, MediaTypes.BOARDGAME),
        ):
            entry = self.create(model, media_type, "Seen", **self.full_details())
            model.objects.filter(pk=entry.pk).update(
                start_date=datetime(2024, 1, 1, tzinfo=UTC),
                end_date=datetime(2024, 1, 2, tzinfo=UTC),
            )
        self.show(plays=2)

        for media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.BOARDGAME.value,
        ):
            url = reverse("medialist", args=["test", media_type])
            for field in library_fields.get_sortable_fields(media_type):
                if field.key in SORTS_WITH_COLUMNS:
                    continue
                with self.subTest(media_type=media_type, sort=field.key):
                    response = self.client.get(
                        url,
                        {"sort": field.key, "layout": "table"},
                    )

                    self.assertEqual(response.context["sorted_field"].key, field.key)
                    values = [
                        media.sorted_value for media in response.context["media_list"]
                    ]
                    self.assertTrue(all(values), values)

    def day(self, year, month, day):
        """Return a day as the user's date format shows it."""
        return app_tags.date_format(datetime(year, month, day, tzinfo=UTC), self.user)

    def sorted_values(self, media_type, sort):
        """Return the sorted values a media type's list shows, by title."""
        response = self.client.get(
            reverse("medialist", args=["test", media_type]),
            {"sort": sort, "layout": "table"},
        )
        return {
            media.item.title: media.sorted_value
            for media in response.context["media_list"]
        }

    def test_exact_sorted_values(self):
        """Dates, times finished and players read as they should."""
        self.show(plays=2)
        self.create(Movie, MediaTypes.MOVIE, "Added")
        movie = self.create(
            Movie,
            MediaTypes.MOVIE,
            "Twice",
        )
        Movie.objects.create(
            item=movie.item, user=self.user, status=Status.COMPLETED.value
        )
        self.create(
            BoardGame,
            MediaTypes.BOARDGAME,
            "Game",
            min_players=2,
            max_players=4,
        )

        self.assertEqual(self.sorted_values("tv", "plays"), {"Show": "2"})
        self.assertEqual(self.sorted_values("movie", "plays")["Twice"], "2")
        self.assertEqual(self.sorted_values("movie", "plays")["Added"], "1")
        self.assertEqual(
            self.sorted_values("boardgame", "players"),
            {"Game": "2\u20134 players"},
        )
        # The last of the episodes' dates, for a show gone through
        shown = self.sorted_values("tv", "end_date")["Show"]
        self.assertEqual(
            shown, app_tags.date_format(datetime(2024, 3, 2, tzinfo=UTC), self.user)
        )
        shown = self.sorted_values("tv", "start_date")["Show"]
        self.assertEqual(
            shown, app_tags.date_format(datetime(2024, 3, 1, tzinfo=UTC), self.user)
        )
        recent = self.sorted_values("tv", "recent")["Show"]
        self.assertEqual(
            recent, app_tags.date_format(datetime(2024, 3, 2, tzinfo=UTC), self.user)
        )

    def test_sorts_without_a_column_show_the_last_activity(self):
        """The title and rating have columns, so the last one is the last activity."""
        for sort in ("title", "score"):
            with self.subTest(sort=sort):
                response = self.client.get(self.url, {"sort": sort, "layout": "table"})

                self.assertEqual(response.context["sorted_field"].key, "recent")
                self.assertTrue(response.context["media_list"][0].sorted_value)

    def quick_row(self, sort):
        """Plan the movie, then complete it from a list row sorted by `sort`."""
        planned = Movie.objects.get(pk=self.movie.pk)
        planned.status = Status.PLANNING.value
        planned.save()

        return self.client.post(
            reverse("quick_action"),
            {
                "media_id": self.movie.item.media_id,
                "source": self.movie.item.source,
                "media_type": self.movie.item.media_type,
                "instance_id": self.movie.pk,
                "action": quick_actions.COMPLETE,
                "card": "row",
                "sort": sort,
                "status": "All",
            },
        )

    def test_quick_action_row_shows_the_sorted_value(self):
        """A row after its quick action is of the list's sort."""
        response = self.quick_row("imdb")

        self.assertEqual(response.context["media"].sorted_value, "8.4")
        self.assertContains(response, "8.4")

    def test_quick_action_row_counts_the_times_finished(self):
        """The row's times finished count the completed entries."""
        response = self.quick_row("plays")

        self.assertEqual(response.context["media"].sorted_value, "1")
