from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase
from django.utils import timezone

from app import library_fields
from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    MediaManager,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from events.models import Event
from users.models import LibraryStatusChoices, MediaSortChoices, MediaStatusChoices


class MediaListFilterTests(TestCase):
    """Test sorting and filtering a media type's list."""

    def setUp(self):
        """Create a user."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.manager = MediaManager()

    def item(self, title, media_type=MediaTypes.MOVIE.value, **fields):
        """Create an item."""
        return Item.objects.create(
            media_id=title,
            source=Sources.TMDB.value,
            media_type=media_type,
            title=title,
            image="https://example.com/image.jpg",
            **fields,
        )

    def movie(self, title, *entries):
        """Create a movie's entries from (status, score, added day) tuples."""
        item = self.item(title)
        for status, score, day in entries:
            movie = Movie(item=item, user=self.user, status=status, score=score)
            Movie.objects.bulk_create([movie])
            Movie.objects.filter(pk=movie.pk).update(
                created_at=datetime(2024, 1, day, tzinfo=UTC),
            )
        return item

    def titles(self, media_type=MediaTypes.MOVIE.value, query="", **kwargs):
        """Return the titles a list shows, in its order, under a query's filters."""
        kwargs["filters"] = library_fields.parse_filters(media_type, QueryDict(query))
        kwargs.setdefault("status_filter", MediaStatusChoices.ALL)
        kwargs.setdefault("sort_filter", MediaSortChoices.TITLE)
        return [
            media.item.title
            for media in self.manager.get_media_list(
                user=self.user,
                media_type=media_type,
                **kwargs,
            )
        ]

    def test_date_added_goes_by_the_first_entry(self):
        """A rewatch added last still sorts by when the movie was first added."""
        self.movie(
            "Old", (Status.COMPLETED.value, None, 1), (Status.REPEATING.value, None, 20)
        )
        self.movie("Middle", (Status.COMPLETED.value, None, 10))
        self.movie("New", (Status.PLANNING.value, None, 15))

        self.assertEqual(
            self.titles(sort_filter=MediaSortChoices.ADDED),
            ["New", "Middle", "Old"],
        )
        self.assertEqual(
            self.titles(sort_filter=MediaSortChoices.ADDED, sort_order="asc"),
            ["Old", "Middle", "New"],
        )

    def test_sorts_go_both_ways_with_missing_values_last(self):
        """Unrated media comes last whichever way the ratings go."""
        self.movie("Low", (Status.COMPLETED.value, Decimal(4), 1))
        self.movie("High", (Status.COMPLETED.value, Decimal(9), 2))
        self.movie("Unrated", (Status.COMPLETED.value, None, 3))

        self.assertEqual(
            self.titles(sort_filter=MediaSortChoices.SCORE),
            ["High", "Low", "Unrated"],
        )
        self.assertEqual(
            self.titles(sort_filter=MediaSortChoices.SCORE, sort_order="asc"),
            ["Low", "High", "Unrated"],
        )

    def test_title_starts_from_a(self):
        """Titles sort from A unless reversed."""
        self.movie("Beta", (Status.COMPLETED.value, None, 1))
        self.movie("alpha", (Status.COMPLETED.value, None, 2))

        self.assertEqual(self.titles(), ["alpha", "Beta"])
        self.assertEqual(self.titles(sort_order="desc"), ["Beta", "alpha"])

    def test_status_goes_from_in_progress_to_dropped(self):
        """Statuses sort from what's being gone through, by the latest entry."""
        self.movie("Dropped", (Status.DROPPED.value, None, 1))
        self.movie("Done", (Status.COMPLETED.value, None, 2))
        self.movie(
            "Rewatching",
            (Status.COMPLETED.value, None, 3),
            (Status.REPEATING.value, None, 4),
        )
        self.movie("Watching", (Status.IN_PROGRESS.value, None, 5))

        self.assertEqual(
            self.titles(sort_filter=MediaSortChoices.STATUS),
            ["Watching", "Rewatching", "Done", "Dropped"],
        )
        self.assertEqual(
            self.titles(sort_filter=MediaSortChoices.STATUS, sort_order="desc"),
            ["Dropped", "Done", "Rewatching", "Watching"],
        )

    def test_rated_goes_by_the_latest_entry(self):
        """A rewatch not rated yet is unrated, even if the first watch was."""
        self.movie(
            "Rewatching",
            (Status.COMPLETED.value, Decimal(8), 1),
            (Status.REPEATING.value, None, 2),
        )
        self.movie("Rated", (Status.COMPLETED.value, Decimal(7), 3))

        self.assertEqual(self.titles(query="score_min=0"), ["Rated"])
        self.assertEqual(self.titles(query="score=unrated"), ["Rewatching"])
        self.assertEqual(self.titles(query="score=anything"), ["Rated", "Rewatching"])

    def test_rewatched_is_finished_more_than_once(self):
        """Only media finished at least twice is rewatched."""
        self.movie(
            "Twice",
            (Status.COMPLETED.value, None, 1),
            (Status.COMPLETED.value, None, 2),
        )
        self.movie(
            "Once",
            (Status.COMPLETED.value, None, 3),
            (Status.REPEATING.value, None, 4),
        )

        self.assertEqual(self.titles(query="plays_min=2"), ["Twice"])

    def test_rewatched_shows_go_by_their_episodes(self):
        """A show is rewatched once each of its watched episodes is watched twice."""
        for title, watches in (("Twice", 2), ("Once", 1)):
            tv = TV(item=self.item(title, MediaTypes.TV.value), user=self.user)
            TV.objects.bulk_create([tv])
            season = Season(
                item=self.item(title, MediaTypes.SEASON.value, season_number=1),
                user=self.user,
                related_tv=tv,
                status=Status.COMPLETED.value,
            )
            Season.objects.bulk_create([season])
            episode = self.item(
                title,
                MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=1,
            )
            Episode.objects.bulk_create(
                [Episode(item=episode, related_season=season) for _ in range(watches)],
            )

        self.assertEqual(
            self.titles(MediaTypes.TV.value, query="plays_min=2"), ["Twice"]
        )

    def create_caught_up_anime(self):
        """Create anime in progress up to its latest episode, the next to come."""
        item = self.item("Airing", MediaTypes.ANIME.value)
        anime = Anime(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=2,
        )
        Anime.objects.bulk_create([anime])
        now = timezone.now()
        for number, when in ((1, -2), (2, -1), (3, 1)):
            Event.objects.create(
                item=item,
                content_number=number,
                datetime=now + timedelta(days=when),
            )

    def test_caught_up_lists_what_waits_for_its_next_release(self):
        """Caught up media is listed as caught up instead of in progress."""
        self.create_caught_up_anime()
        behind = Anime(
            item=self.item("Behind", MediaTypes.ANIME.value),
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        Anime.objects.bulk_create([behind])

        self.assertEqual(
            self.titles(
                MediaTypes.ANIME.value,
                status_filter=LibraryStatusChoices.CAUGHT_UP,
            ),
            ["Airing"],
        )
        self.assertEqual(
            self.titles(MediaTypes.ANIME.value, status_filter=Status.IN_PROGRESS.value),
            ["Behind"],
        )

    def test_status_counts(self):
        """Each status counts its media, caught up apart from in progress."""
        self.create_caught_up_anime()
        for title, status in (
            ("Done", Status.COMPLETED.value),
            ("Later", Status.PLANNING.value),
            ("Now", Status.IN_PROGRESS.value),
        ):
            Anime.objects.bulk_create(
                [
                    Anime(
                        item=self.item(title, MediaTypes.ANIME.value),
                        user=self.user,
                        status=status,
                    ),
                ],
            )

        counts = self.manager.get_status_counts(self.user, MediaTypes.ANIME.value)

        self.assertEqual(counts[MediaStatusChoices.ALL], 4)
        self.assertEqual(counts[Status.IN_PROGRESS.value], 1)
        self.assertEqual(counts[LibraryStatusChoices.CAUGHT_UP], 1)
        self.assertEqual(counts[Status.COMPLETED.value], 1)
        self.assertEqual(counts[Status.PLANNING.value], 1)
        self.assertEqual(counts[Status.DROPPED.value], 0)

    def test_status_counts_take_the_filters(self):
        """The counts are of the media the search and filters leave."""
        self.movie("Rated", (Status.COMPLETED.value, Decimal(7), 1))
        self.movie("Unrated", (Status.PLANNING.value, None, 2))

        counts = self.manager.get_status_counts(
            self.user,
            MediaTypes.MOVIE.value,
            "rat",
            filters=library_fields.parse_filters(
                MediaTypes.MOVIE.value,
                QueryDict("score_min=0"),
            ),
        )

        self.assertEqual(counts[MediaStatusChoices.ALL], 1)
        self.assertEqual(counts[Status.COMPLETED.value], 1)
        self.assertEqual(counts[Status.PLANNING.value], 0)

    def test_caught_up_counts_take_the_filters(self):
        """Caught up media the filters leave out isn't moved out of in progress."""
        self.create_caught_up_anime()  # "Airing", with no provider score
        behind = Anime(
            item=self.item("Behind", MediaTypes.ANIME.value),
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        Anime.objects.bulk_create([behind])
        Item.objects.filter(pk=behind.item_id).update(provider_score=Decimal(8))

        counts = self.manager.get_status_counts(
            self.user,
            MediaTypes.ANIME.value,
            filters=library_fields.parse_filters(
                MediaTypes.ANIME.value,
                QueryDict("provider_score_min=7"),
            ),
        )

        self.assertEqual(counts[MediaStatusChoices.ALL], 1)
        self.assertEqual(counts[Status.IN_PROGRESS.value], 1)
        self.assertEqual(counts[LibraryStatusChoices.CAUGHT_UP], 0)
