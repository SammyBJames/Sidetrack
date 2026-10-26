from datetime import UTC, date, datetime

from django.apps import apps
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.urls import reverse

from app import library_fields
from app.models import (
    TV,
    Episode,
    Item,
    MediaManager,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from users.models import MediaStatusChoices

MOVIE = MediaTypes.MOVIE.value


class LibraryFiltersTests(TestCase):
    """Test filtering a library by the fields of the registry."""

    def setUp(self):
        """Create a user."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.manager = MediaManager()
        self.count = 0

    def entry(self, media_type, title, item_fields=None, **entry_fields):
        """Create an entry of a media type with its item's details set."""
        self.count += 1
        model = apps.get_model("app", media_type)
        item = Item.objects.create(
            media_id=f"{media_type}{self.count}",
            source=Sources.MANUAL.value,
            media_type=media_type,
            title=title,
            image="https://example.com/image.jpg",
            **(item_fields or {}),
        )
        entry_fields.setdefault("status", Status.COMPLETED.value)
        return model.objects.create(item=item, user=self.user, **entry_fields)

    def titles(self, query, media_type=MOVIE, **kwargs):
        """Return the titles a media type's list shows under a query's filters."""
        kwargs.setdefault("sort_filter", "title")
        return [
            media.item.title
            for media in self.manager.get_media_list(
                user=self.user,
                media_type=media_type,
                status_filter=MediaStatusChoices.ALL,
                filters=library_fields.parse_filters(media_type, QueryDict(query)),
                **kwargs,
            )
        ]

    def test_number_range(self):
        """A number filter keeps media within its bounds, inclusive."""
        for title, score in (
            ("Low", "5.0"),
            ("Mid", "7.5"),
            ("High", "9.0"),
            ("None", None),
        ):
            self.entry(MOVIE, title, {"imdb_score": score})

        self.assertEqual(self.titles("imdb_min=7"), ["High", "Mid"])
        self.assertEqual(self.titles("imdb_max=7.5"), ["Low", "Mid"])
        self.assertEqual(self.titles("imdb_min=5&imdb_max=7.5"), ["Low", "Mid"])
        self.assertEqual(self.titles("imdb_min=9&imdb_max=9"), ["High"])
        # A minimum over the maximum leaves nothing
        self.assertEqual(self.titles("imdb_min=9&imdb_max=5"), [])

    def test_year_range_spans_whole_years(self):
        """A year range runs from the first day of its first year to the last."""
        for title, day in (
            ("Before", date(1999, 12, 31)),
            ("Start", date(2000, 1, 1)),
            ("End", date(2010, 12, 31)),
            ("After", date(2011, 1, 1)),
        ):
            self.entry(MOVIE, title, {"release_date": day})
        self.entry(MOVIE, "Unknown")

        self.assertEqual(
            self.titles("release_min=2000&release_max=2010"), ["End", "Start"]
        )
        self.assertEqual(self.titles("release_min=2011"), ["After"])
        self.assertEqual(self.titles("release_max=1999"), ["Before"])

    def test_date_range_takes_whole_days(self):
        """A date range includes media dated at any time of its last day."""
        for title, day, hour in (
            ("Early", 4, 23),
            ("Inside", 5, 15),
            ("Late", 6, 23),
            ("Next", 7, 0),
        ):
            self.entry(
                MOVIE,
                title,
                start_date=datetime(2024, 1, day, hour, tzinfo=UTC),
            )

        self.assertEqual(
            self.titles("start_date_min=2024-01-05&start_date_max=2024-01-06"),
            ["Inside", "Late"],
        )
        self.assertEqual(self.titles("start_date_min=2024-01-07"), ["Next"])

    def test_entry_filters_go_by_the_latest_entry(self):
        """A rewatch without a start date isn't matched by its first watch's."""
        first = datetime(2024, 1, 1, tzinfo=UTC)
        item = self.entry(
            MOVIE,
            "Rewatching",
            start_date=datetime(2024, 1, 5, tzinfo=UTC),
        ).item
        Movie = apps.get_model("app", MOVIE)
        newer = Movie.objects.create(
            item=item, user=self.user, status=Status.REPEATING.value
        )
        Movie.objects.filter(pk=newer.pk).update(
            created_at=datetime(2024, 2, 1, tzinfo=UTC)
        )
        Movie.objects.exclude(pk=newer.pk).update(created_at=first)
        self.entry(MOVIE, "Single", start_date=datetime(2024, 1, 5, tzinfo=UTC))

        self.assertEqual(self.titles("start_date_min=2024-01-01"), ["Single"])

    def test_rating_range_and_unrated(self):
        """Your rating takes a range, unrated media, or both."""
        for title, score in (("Low", "4"), ("High", "9"), ("Unrated", None)):
            self.entry(MOVIE, title, score=score)

        self.assertEqual(self.titles("score_min=7"), ["High"])
        self.assertEqual(self.titles("score=unrated"), ["Unrated"])
        self.assertEqual(self.titles("score_min=7&score=unrated"), ["High", "Unrated"])

    def test_times_finished_range(self):
        """Times finished filters by the completed entries, rewatched from two."""
        once = self.entry(MOVIE, "Once")
        twice = self.entry(MOVIE, "Twice")
        self.entry(MOVIE, "Never", status=Status.PLANNING.value)
        apps.get_model("app", MOVIE).objects.create(
            item=twice.item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        self.assertEqual(self.titles("plays_min=2"), ["Twice"])
        self.assertEqual(self.titles("plays_max=0"), ["Never"])
        self.assertEqual(self.titles("plays_min=1&plays_max=1"), [once.item.title])

    def test_choice_takes_any_of_its_values(self):
        """Airing statuses are filtered to any of those asked for."""
        anime = MediaTypes.ANIME.value
        for title, status in (
            ("Airing", "airing"),
            ("Ended", "ended"),
            ("Upcoming", "upcoming"),
        ):
            self.entry(anime, title, {"airing_status": status})
        self.entry(anime, "Unknown")

        self.assertEqual(self.titles("airing_status=airing", anime), ["Airing"])
        self.assertEqual(
            self.titles("airing_status=airing&airing_status=ended", anime),
            ["Airing", "Ended"],
        )
        # Unknown choices are ignored, so nothing is filtered
        self.assertEqual(len(self.titles("airing_status=bogus", anime)), 4)
        self.assertEqual(
            self.titles("airing_status=bogus&airing_status=ended", anime),
            ["Ended"],
        )

    def test_genre_matches_whole_names_ignoring_case(self):
        """Genres match whole names of the item's list, in any case."""
        self.entry(MOVIE, "Both", {"genres": "|Drama|Comedy|"})
        self.entry(MOVIE, "Drama", {"genres": "|Drama|"})
        self.entry(MOVIE, "Horror", {"genres": "|Horror|"})
        self.entry(MOVIE, "Melodrama", {"genres": "|Melodrama|"})
        self.entry(MOVIE, "None")

        self.assertEqual(self.titles("genre=Drama"), ["Both", "Drama"])
        self.assertEqual(self.titles("genre=drama"), ["Both", "Drama"])
        self.assertEqual(
            self.titles("genre=Drama&genre=Horror"), ["Both", "Drama", "Horror"]
        )
        self.assertEqual(self.titles("genre=Comedy&genre=Horror"), ["Both", "Horror"])
        self.assertEqual(self.titles("genre=Dram"), [])

    def test_platform_of_games(self):
        """Games are filtered by their platforms the same way."""
        game = MediaTypes.GAME.value
        self.entry(game, "Switch", {"platforms": "|Nintendo Switch|PC|"})
        self.entry(game, "Playstation", {"platforms": "|PlayStation 5|"})

        self.assertEqual(self.titles("platform=pc", game), ["Switch"])
        self.assertEqual(
            self.titles("platform=PC&platform=PlayStation 5", game),
            ["Playstation", "Switch"],
        )

    def test_players_must_fit_the_count(self):
        """Board games are kept if they play with a count in the range."""
        board = MediaTypes.BOARDGAME.value
        self.entry(board, "Pair", {"min_players": 2, "max_players": 2})
        self.entry(board, "Party", {"min_players": 4, "max_players": 10})
        self.entry(board, "Unknown")

        self.assertEqual(self.titles("players_min=3&players_max=3", board), [])
        self.assertEqual(self.titles("players_min=2&players_max=2", board), ["Pair"])
        self.assertEqual(self.titles("players_min=5&players_max=5", board), ["Party"])
        self.assertEqual(
            self.titles("players_min=2&players_max=4", board), ["Pair", "Party"]
        )

    def watched_show(self, title, watches, season_number=1):
        """Create a show with an episode watched once per date of `watches`."""
        self.count += 1
        show_item = Item.objects.create(
            media_id=f"tv{self.count}",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title=title,
            image="https://example.com/image.jpg",
        )
        show = TV.objects.create(
            item=show_item, user=self.user, status=Status.COMPLETED.value
        )
        season = Season.objects.create(
            item=Item.objects.create(
                media_id=show_item.media_id,
                source=Sources.MANUAL.value,
                media_type=MediaTypes.SEASON.value,
                title=title,
                image="https://example.com/image.jpg",
                season_number=season_number,
            ),
            related_tv=show,
            user=self.user,
        )
        for number, day in enumerate(watches, start=1):
            episode = Item.objects.create(
                media_id=show_item.media_id,
                source=Sources.MANUAL.value,
                media_type=MediaTypes.EPISODE.value,
                title=title,
                image="https://example.com/image.jpg",
                season_number=season_number,
                episode_number=number,
            )
            Episode.objects.create(
                item=episode,
                related_season=season,
                end_date=datetime(2024, 1, day, tzinfo=UTC),
            )
        return show

    def test_shows_filter_by_their_episodes(self):
        """A show's progress, dates and times finished are its episodes'."""
        self.watched_show("Few", [10])
        self.watched_show("Many", [1, 2, 3])
        self.entry(MediaTypes.TV.value, "None")
        tv = MediaTypes.TV.value

        self.assertEqual(self.titles("progress_min=2", tv), ["Many"])
        self.assertEqual(self.titles("progress_max=1", tv), ["Few", "None"])
        self.assertEqual(self.titles("end_date_min=2024-01-05", tv), ["Few"])
        self.assertEqual(self.titles("start_date_max=2024-01-01", tv), ["Many"])
        self.assertEqual(self.titles("plays_min=1", tv), ["Few", "Many"])
        self.assertEqual(self.titles("plays_max=0", tv), ["None"])
        self.assertEqual(self.titles("plays_min=2", tv), [])

    def test_bad_params_are_ignored(self):
        """Invalid values, unknown params and fields the type lacks filter nothing."""
        self.entry(
            MOVIE, "One", {"imdb_score": "5.0", "release_date": date(2000, 1, 1)}
        )
        self.entry(MOVIE, "Two", {"imdb_score": "9.0"})

        for query in (
            "imdb_min=abc",
            "imdb_min=nan",
            "imdb_max=inf",
            "imdb_min=1e999",
            "imdb_min=",
            "release_min=2000-01",
            "release_min=0",
            "release_max=99999",
            "release_min=2000.5",
            "start_date_min=yesterday",
            "start_date_max=9999-12-31",
            "start_date_min=0001-01-01",
            "added_max=9999-12-31",
            "recent_min=0001-01-01",
            "start_date_min=2024-13-45",
            "genre=",
            "genre=%7C",
            "airing_status=ended",
            "time_to_beat_min=10",
            "seasons_min=1",
            "progress_min=1",
            "title=One",
            "status=Completed",
            "bogus_min=1",
            "score=rated",
            "rated=rated",
            "rewatched=1",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.titles(query), ["One", "Two"])

        self.assertEqual(self.titles("imdb_min=abc&imdb_max=6"), ["One"])

    @override_settings(TIME_ZONE="Asia/Tokyo")
    def test_extreme_dates_are_ignored_east_of_utc(self):
        """The first day of the first year doesn't overflow in a zone ahead of UTC."""
        self.entry(MOVIE, "One", start_date=datetime(2024, 1, 5, tzinfo=UTC))

        for query in ("start_date_min=0001-01-01", "added_min=0001-01-01"):
            with self.subTest(query=query):
                self.assertEqual(self.titles(query), ["One"])

    def test_same_field_filtered_and_sorted_for_shows(self):
        """A show list filtered and sorted by one episode field still loads."""
        self.watched_show("Few", [10])
        self.watched_show("Many", [1, 2, 3])
        self.entry(MediaTypes.TV.value, "None")
        tv, season = MediaTypes.TV.value, MediaTypes.SEASON.value

        for sort, query, order, shows in (
            ("progress", "progress_min=1", "desc", ["Many", "Few"]),
            ("start_date", "start_date_max=2024-01-10", "asc", ["Many", "Few"]),
            ("end_date", "end_date_min=2024-01-03", "asc", ["Many", "Few"]),
            ("recent", "recent_min=2024-01-01", "desc", ["None", "Few", "Many"]),
        ):
            for media_type in (tv, season):
                expected = [t for t in shows if media_type == tv or t != "None"]
                with self.subTest(sort=sort, media_type=media_type):
                    self.assertEqual(
                        self.titles(
                            query,
                            media_type,
                            sort_filter=sort,
                            sort_order=order,
                        ),
                        expected,
                    )

    def test_fractional_bounds_of_whole_numbers(self):
        """A minimum rounds up and a maximum down on whole number columns."""
        for title, value in (("A", 98), ("B", 99), ("C", 100)):
            self.entry(MOVIE, title, {"rotten_tomatoes": value, "imdb_score": "8.5"})

        self.assertEqual(self.titles("rotten_tomatoes_min=98.5"), ["B", "C"])
        self.assertEqual(self.titles("rotten_tomatoes_max=98.5"), ["A"])
        self.assertEqual(
            self.titles("rotten_tomatoes_min=98.2&rotten_tomatoes_max=98.8"), []
        )
        # A decimal column keeps its fractions
        self.assertEqual(self.titles("imdb_min=8.5"), ["A", "B", "C"])
        self.assertEqual(self.titles("imdb_min=8.6"), [])
        self.assertEqual(self.titles("imdb_max=8.4"), [])

    def test_counts_honour_the_filters(self):
        """Status counts are of the media the filters leave."""
        self.entry(MOVIE, "Good", {"imdb_score": "8.0"}, status=Status.COMPLETED.value)
        self.entry(MOVIE, "Bad", {"imdb_score": "3.0"}, status=Status.COMPLETED.value)
        self.entry(MOVIE, "Next", {"imdb_score": "9.0"}, status=Status.PLANNING.value)

        counts = self.manager.get_status_counts(
            self.user,
            MOVIE,
            filters=library_fields.parse_filters(MOVIE, QueryDict("imdb_min=7")),
        )

        self.assertEqual(counts[MediaStatusChoices.ALL], 2)
        self.assertEqual(counts[Status.COMPLETED.value], 1)
        self.assertEqual(counts[Status.PLANNING.value], 1)

    def test_active_filters_are_labelled(self):
        """Each filter in use has a label and the params that remove it."""
        filters = library_fields.parse_filters(
            MOVIE,
            QueryDict(
                "imdb_min=7&release_min=2000&release_max=2010&runtime_min=125"
                "&genre=Drama&genre=drama&genre=Comedy&score=unrated"
                "&start_date_min=2024-03-05&plays_max=1",
            ),
        )

        pills = {
            (pill["params"][0], pill["value"]): pill
            for pill in library_fields.describe_filters(filters)
        }

        self.assertEqual(pills[("imdb_min", None)]["label"], "IMDb rating ≥ 7")
        self.assertEqual(pills[("imdb_min", None)]["params"], ["imdb_min", "imdb_max"])
        self.assertEqual(
            pills[("release_min", None)]["label"], "Released 2000\u20132010"
        )
        self.assertEqual(pills[("runtime_min", None)]["label"], "Runtime ≥ 2h 5m")
        self.assertEqual(pills[("score_min", None)]["label"], "Unrated")
        self.assertEqual(
            pills[("score_min", None)]["params"],
            ["score_min", "score_max", "score"],
        )
        self.assertEqual(
            pills[("plays_min", None)]["params"],
            ["plays_min", "plays_max"],
        )
        self.assertEqual(
            pills[("start_date_min", None)]["label"], "Start date from Mar 5, 2024"
        )
        self.assertEqual(pills[("plays_min", None)]["label"], "Times watched ≤ 1")
        self.assertEqual(pills[("genre", "Drama")]["label"], "Drama")
        self.assertEqual(pills[("genre", "Comedy")]["params"], ["genre"])
        self.assertNotIn(("genre", "drama"), pills)

    def test_genres_and_platforms_present_with_counts(self):
        """The choices are the library's names, counted by title, most first."""
        rewatched = self.entry(MOVIE, "A", {"genres": "|Drama|Comedy|"})
        # A second entry of the same title doesn't count it twice
        Movie.objects.create(
            item=rewatched.item, user=self.user, status=Status.COMPLETED.value
        )
        self.entry(MOVIE, "B", {"genres": "|drama|"})
        self.entry(MOVIE, "C", {"genres": "|Horror|"})
        self.entry(MOVIE, "D")
        # Another user's library has its own genres
        other = get_user_model().objects.create_user(username="other")
        Movie.objects.create(
            item=Item.objects.create(
                media_id="other",
                source=Sources.MANUAL.value,
                media_type=MOVIE,
                title="Other",
                image="https://example.com/image.jpg",
                genres="|Western|",
            ),
            user=other,
            status=Status.COMPLETED.value,
        )

        self.assertEqual(
            library_fields.present_choices(self.user, MOVIE),
            {
                "genre": [
                    {"value": "Drama", "count": 2},
                    {"value": "Comedy", "count": 1},
                    {"value": "Horror", "count": 1},
                ],
            },
        )
        self.assertEqual(self.titles("genre=Comedy"), ["A"])
        self.entry(MediaTypes.GAME.value, "G", {"platforms": "|PC|", "genres": "|RPG|"})
        game = library_fields.present_choices(self.user, MediaTypes.GAME.value)
        self.assertEqual(game["platform"], [{"value": "PC", "count": 1}])

    def test_view_takes_the_filter_params(self):
        """The list takes filter params, and shows when nothing matches them."""
        self.client.login(**self.credentials)
        self.entry(MOVIE, "Good", {"imdb_score": "8.0", "genres": "|Drama|"}, score="9")
        self.entry(MOVIE, "Bad", {"imdb_score": "3.0"})
        url = reverse("medialist", args=["test", MOVIE])

        response = self.client.get(url, {"imdb_min": "7"})
        self.assertEqual(
            [m.item.title for m in response.context["media_list"]], ["Good"]
        )
        self.assertEqual(
            [pill["label"] for pill in response.context["active_filters"]],
            ["IMDb rating ≥ 7"],
        )
        genre = next(
            group
            for group in response.context["filter_groups"]
            if group["title"] == "Genre"
        )
        self.assertEqual(
            [(o["value"], o["count"]) for o in genre["choices"][0]["options"]],
            [("Drama", 1)],
        )

        response = self.client.get(url, {"plays_min": "2"})
        self.assertEqual(list(response.context["media_list"]), [])
        self.assertContains(response, "Nothing matches these filters")

        counts = self.client.get(url, {"part": "counts", "imdb_min": "7"}).context
        self.assertEqual(counts["status_choices"][0]["count"], 1)
