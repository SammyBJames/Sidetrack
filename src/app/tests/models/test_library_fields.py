from datetime import UTC, date, datetime
from decimal import Decimal

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from app import library_fields
from app.models import (
    TV,
    Episode,
    Item,
    MediaManager,
    MediaTypes,
    Season,
    Sources,
    Status,
)
from users.models import MediaSortChoices, MediaStatusChoices

SORTS = MediaSortChoices

# The item column of each sort that reads one, with a low and a high value
ITEM_VALUES = {
    SORTS.RELEASE: ("release_date", date(1999, 5, 1), date(2021, 3, 9)),
    SORTS.PROVIDER_SCORE: ("provider_score", Decimal("3.5"), Decimal("8.1")),
    SORTS.IMDB: ("imdb_score", Decimal("4.2"), Decimal("9.0")),
    SORTS.ROTTEN_TOMATOES: ("rotten_tomatoes", 20, 95),
    SORTS.METACRITIC: ("metacritic", 35, 88),
    SORTS.RUNTIME: ("runtime", 24, 140),
    SORTS.SEASONS: ("season_count", 1, 9),
    SORTS.LENGTH: ("length", 12, 900),
    # Airing comes first when ascending
    SORTS.AIRING_STATUS: ("airing_status", "airing", "ended"),
    SORTS.TIME_TO_BEAT: ("time_to_beat", 300, 4200),
    SORTS.PLAYERS: ("max_players", 2, 8),
}

# The entry columns of each sort that reads one, with a low and a high value
ENTRY_VALUES = {
    SORTS.SCORE: ("score", Decimal("4.0"), Decimal("9.5")),
    SORTS.START_DATE: (
        "start_date",
        datetime(2023, 1, 5, tzinfo=UTC),
        datetime(2024, 6, 1, tzinfo=UTC),
    ),
    SORTS.END_DATE: (
        "end_date",
        datetime(2023, 2, 5, tzinfo=UTC),
        datetime(2024, 7, 1, tzinfo=UTC),
    ),
}

EPISODE_DERIVED = {SORTS.START_DATE, SORTS.END_DATE}


class LibraryFieldsTests(TestCase):
    """Test every sort of the field registry for every media type."""

    def setUp(self):
        """Create a user."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.manager = MediaManager()
        self.count = 0

    def entry(self, media_type, title, item_fields=None, **entry_fields):
        """Create an entry of a media type with its item's details set."""
        self.count += 1
        item_kwargs = {}
        entry_kwargs = {}
        if media_type == MediaTypes.SEASON.value:
            item_kwargs["season_number"] = 1
            item_kwargs["media_id"] = f"tv{self.count}"
            tv_item = Item.objects.create(
                media_id=f"tv{self.count}",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.TV.value,
                title=title,
                image="https://example.com/image.jpg",
            )
            entry_kwargs["related_tv"] = TV.objects.create(
                item=tv_item,
                user=self.user,
                status=Status.COMPLETED.value,
            )
        item = Item.objects.create(
            media_id=item_kwargs.pop("media_id", f"{media_type}{self.count}"),
            source=Sources.MANUAL.value,
            media_type=media_type,
            title=title,
            image="https://example.com/image.jpg",
            **item_kwargs,
            **(item_fields or {}),
        )
        model = apps.get_model("app", media_type)
        return model.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            **entry_kwargs,
            **entry_fields,
        )

    def titles(self, media_type, sort, order):
        """Return the titles a media type's list shows sorted, in a direction."""
        return [
            media.item.title
            for media in self.manager.get_media_list(
                user=self.user,
                media_type=media_type,
                status_filter=MediaStatusChoices.ALL,
                sort_filter=sort,
                sort_order=order,
            )
        ]

    def test_every_sortable_field_is_covered_by_the_sort_tests(self):
        """Each sort of the registry has values in a table or a test of its own."""
        covered = {
            *ITEM_VALUES,
            *ENTRY_VALUES,
            SORTS.PROGRESS,
            SORTS.PLAYS,
            SORTS.RECENT,
            SORTS.ADDED,
            SORTS.TITLE,
            SORTS.STATUS,
        }
        sortable = {
            field.key
            for media_type in library_fields.ALL_TYPES
            for field in library_fields.get_sortable_fields(media_type)
        }
        self.assertEqual(sortable - covered, set())

    def test_item_details_sort_both_ways_with_missing_last(self):
        """Each sort by a stored detail orders its media types both ways."""
        for media_type in library_fields.ALL_TYPES:
            for field in library_fields.get_sortable_fields(media_type):
                if field.key not in ITEM_VALUES:
                    continue
                column, low, high = ITEM_VALUES[field.key]
                with self.subTest(media_type=media_type, sort=field.key):
                    model = apps.get_model("app", media_type)
                    model.objects.filter(user=self.user).delete()
                    # Titles in the order that would put the missing one first
                    self.entry(media_type, "Missing")
                    self.entry(media_type, "High", {column: high})
                    self.entry(media_type, "Low", {column: low})

                    self.assertEqual(
                        self.titles(media_type, field.key, "asc"),
                        ["Low", "High", "Missing"],
                    )
                    self.assertEqual(
                        self.titles(media_type, field.key, "desc"),
                        ["High", "Low", "Missing"],
                    )

    def test_entry_values_sort_both_ways_with_missing_last(self):
        """Each sort by the entry's own value orders its media types both ways."""
        for media_type in library_fields.ALL_TYPES:
            if media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value):
                # Their dates come from their episodes, as tested with them
                skipped = EPISODE_DERIVED
            else:
                skipped = set()
            for field in library_fields.get_sortable_fields(media_type):
                if field.key not in ENTRY_VALUES or field.key in skipped:
                    continue
                column, low, high = ENTRY_VALUES[field.key]
                with self.subTest(media_type=media_type, sort=field.key):
                    model = apps.get_model("app", media_type)
                    model.objects.filter(user=self.user).delete()
                    self.entry(media_type, "Missing")
                    self.entry(media_type, "High", **{column: high})
                    self.entry(media_type, "Low", **{column: low})

                    self.assertEqual(
                        self.titles(media_type, field.key, "asc"),
                        ["Low", "High", "Missing"],
                    )
                    self.assertEqual(
                        self.titles(media_type, field.key, "desc"),
                        ["High", "Low", "Missing"],
                    )

    def watched_show(self, title, watches):
        """Create a show with its watched episodes from (season, episode) pairs.

        Each pair is watched once for each time it's listed.
        """
        show = self.entry(MediaTypes.TV.value, title)
        seasons = {}
        for season_number, episode_number in watches:
            if season_number not in seasons:
                seasons[season_number] = Season.objects.create(
                    item=Item.objects.create(
                        media_id=show.item.media_id,
                        source=Sources.MANUAL.value,
                        media_type=MediaTypes.SEASON.value,
                        title=title,
                        image="https://example.com/image.jpg",
                        season_number=season_number,
                    ),
                    related_tv=show,
                    user=self.user,
                )
            episode_item, _ = Item.objects.get_or_create(
                media_id=show.item.media_id,
                source=Sources.MANUAL.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=season_number,
                episode_number=episode_number,
                defaults={"title": title, "image": "https://example.com/image.jpg"},
            )
            Episode.objects.create(
                item=episode_item,
                related_season=seasons[season_number],
                end_date=datetime(2024, 1, episode_number, tzinfo=UTC),
            )
        return show

    def test_progress_and_dates_of_shows(self):
        """Shows sort by their watched episodes' progress and dates."""
        self.watched_show("Few", [(1, 5)])
        self.watched_show("Many", [(1, 1), (1, 2), (1, 3)])
        self.entry(MediaTypes.TV.value, "None")

        self.assertEqual(
            self.titles(MediaTypes.TV.value, SORTS.PROGRESS, "desc"),
            ["Many", "Few", "None"],
        )
        for sort in (SORTS.START_DATE, SORTS.END_DATE):
            self.assertEqual(
                self.titles(MediaTypes.TV.value, sort, "asc"),
                ["Many", "Few", "None"],
                sort,
            )
            self.assertEqual(
                self.titles(MediaTypes.TV.value, sort, "desc"),
                ["Few", "Many", "None"],
                sort,
            )

    def test_airing_status_goes_from_airing_to_ended(self):
        """Upcoming sits between airing and ended, and unknown comes last."""
        anime = MediaTypes.ANIME.value
        self.entry(anime, "Unknown")
        for title, status in (
            ("Ended", "ended"),
            ("Upcoming", "upcoming"),
            ("Airing", "airing"),
        ):
            self.entry(anime, title, {"airing_status": status})

        self.assertEqual(
            self.titles(anime, SORTS.AIRING_STATUS, "asc"),
            ["Airing", "Upcoming", "Ended", "Unknown"],
        )
        self.assertEqual(
            self.titles(anime, SORTS.AIRING_STATUS, "desc"),
            ["Ended", "Upcoming", "Airing", "Unknown"],
        )

    def test_last_activity_of_an_unwatched_show_is_when_it_was_added(self):
        """A show with no watched episodes sorts by its date added instead."""
        self.watched_show("Watched", [(1, 5)])
        unwatched = self.entry(MediaTypes.TV.value, "Unwatched")
        TV.objects.filter(pk=unwatched.pk).update(
            created_at=datetime(2024, 1, 10, tzinfo=UTC),
        )

        self.assertEqual(
            self.titles(MediaTypes.TV.value, SORTS.RECENT, "desc"),
            ["Unwatched", "Watched"],
        )

    def test_times_finished_of_other_media_counts_completed_entries(self):
        """Media outside shows counts the entries it was completed in."""
        for media_type in library_fields.ALL_TYPES:
            if media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value):
                continue
            with self.subTest(media_type=media_type):
                once = self.entry(media_type, "Once")
                twice = self.entry(media_type, "Twice")
                model = apps.get_model("app", media_type)
                model.objects.create(
                    item=twice.item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                )
                model.objects.create(
                    item=once.item,
                    user=self.user,
                    status=Status.REPEATING.value,
                )
                never = self.entry(media_type, "Never")
                never.status = Status.PLANNING.value
                never.save()

                self.assertEqual(
                    self.titles(media_type, SORTS.PLAYS, "asc"),
                    ["Never", "Once", "Twice"],
                )
                self.assertEqual(
                    self.titles(media_type, SORTS.PLAYS, "desc"),
                    ["Twice", "Once", "Never"],
                )
                model.objects.filter(user=self.user).delete()

    def test_times_finished_of_shows_matches_their_plays(self):
        """Shows and seasons order by their least watched episode, as `plays`."""
        self.entry(MediaTypes.TV.value, "Zero")
        self.watched_show("Once", [(1, 1), (1, 2)])
        self.watched_show("Twice", [(1, 1), (1, 2)] * 2)
        self.watched_show("Uneven", [(1, 1)] * 3 + [(1, 2)])
        self.watched_show("Second season behind", [(1, 1)] * 2 + [(2, 1)])
        self.watched_show("Specials ignored", [(0, 1)] + [(1, 1)] * 2)

        for media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value):
            for order in ("asc", "desc"):
                with self.subTest(media_type=media_type, order=order):
                    media_list = list(
                        self.manager.get_media_list(
                            user=self.user,
                            media_type=media_type,
                            status_filter=MediaStatusChoices.ALL,
                            sort_filter=SORTS.PLAYS,
                            sort_order=order,
                        ),
                    )
                    counts = [media.plays for media in media_list]
                    self.assertTrue(any(counts))
                    self.assertEqual(counts, sorted(counts, reverse=order == "desc"))

        plays = {
            media.item.title: media.plays
            for media in self.manager.get_media_list(
                user=self.user,
                media_type=MediaTypes.TV.value,
                status_filter=MediaStatusChoices.ALL,
                sort_filter=SORTS.PLAYS,
            )
        }
        self.assertEqual(
            plays,
            {
                "Zero": 0,
                "Once": 1,
                "Twice": 2,
                "Uneven": 1,
                "Second season behind": 1,
                "Specials ignored": 2,
            },
        )

    def test_unknown_sort_falls_back_to_the_default(self):
        """A sort the media type doesn't have is sorted by the default."""
        self.entry(MediaTypes.MOVIE.value, "Low", score=Decimal(2))
        self.entry(MediaTypes.MOVIE.value, "High", score=Decimal(9))

        self.assertEqual(
            self.titles(MediaTypes.MOVIE.value, SORTS.PROGRESS, None), ["High", "Low"]
        )
        field = library_fields.get_sort_field(MediaTypes.MOVIE.value, SORTS.PROGRESS)
        self.assertEqual(field.key, library_fields.DEFAULT_SORT)

    def test_fields_are_named_in_the_media_types_words(self):
        """Labels use each media type's verbs, units and default source."""
        labels = {
            (media_type, key): field.label
            for media_type in library_fields.ALL_TYPES
            for key, field in library_fields.get_fields(media_type).items()
        }
        self.assertEqual(labels[("tv", "plays")], "Times watched")
        self.assertEqual(labels[("book", "plays")], "Times read")
        self.assertEqual(labels[("game", "plays")], "Times played")
        self.assertEqual(labels[("book", "length")], "Pages")
        self.assertEqual(labels[("manga", "length")], "Chapters")
        self.assertEqual(labels[("comic", "length")], "Issues")
        self.assertEqual(labels[("anime", "length")], "Episodes")
        self.assertEqual(labels[("movie", "provider_score")], "TMDB rating")
        self.assertEqual(labels[("anime", "provider_score")], "MyAnimeList rating")
        self.assertEqual(labels[("game", "progress")], "Time played")
        self.assertNotIn(("movie", "progress"), labels)
        self.assertNotIn(("boardgame", "progress"), labels)

    def test_unsupported_fields_are_left_out(self):
        """Comics have no provider score and seasons no airing status."""
        self.assertNotIn(SORTS.PROVIDER_SCORE, library_fields.get_fields("comic"))
        self.assertNotIn(SORTS.AIRING_STATUS, library_fields.get_fields("season"))
        self.assertEqual(library_fields.get_fields("movie")["title"].kind, "text")
        self.assertEqual(
            library_fields.get_fields("movie")["title"].format("a_b Title"),
            "a_b Title",
        )

    def test_genre_and_platform_are_not_sortable(self):
        """Genre and platform are only filtered by."""
        sortable = {field.key for field in library_fields.get_sortable_fields("game")}
        self.assertNotIn("genre", sortable)
        self.assertNotIn("platform", sortable)
        self.assertIn("genre", library_fields.get_fields("game"))
        self.assertIn("platform", library_fields.get_fields("game"))

    def test_every_sort_choice_is_a_sortable_field(self):
        """Each sort choice is sortable for some media type, and each field is one."""
        keys = {
            field.key
            for media_type in library_fields.ALL_TYPES
            for field in library_fields.get_sortable_fields(media_type)
        }
        self.assertEqual(keys, set(MediaSortChoices.values))

    def test_values_are_formatted_with_their_units(self):
        """Values read as a list shows them."""
        fields = library_fields.get_fields("movie")
        self.assertEqual(fields[SORTS.RUNTIME].format(125), "2h 5m")
        self.assertEqual(fields[SORTS.RUNTIME].format(45), "45m")
        self.assertEqual(fields[SORTS.IMDB].format(Decimal("8.4")), "8.4")
        self.assertEqual(fields[SORTS.ROTTEN_TOMATOES].format(87), "87%")
        self.assertEqual(fields[SORTS.RELEASE].format(date(2010, 7, 16)), "2010")
        self.assertEqual(
            library_fields.get_fields("book")[SORTS.LENGTH].format(412),
            "412 pages",
        )
        self.assertEqual(
            library_fields.get_fields("book")[SORTS.LENGTH].format(1),
            "1 page",
        )
        self.assertEqual(fields[SORTS.END_DATE].format(date(2024, 3, 5)), "Mar 5, 2024")
        self.assertEqual(fields[SORTS.IMDB].format(None), "")
