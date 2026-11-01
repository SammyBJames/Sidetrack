import datetime
from decimal import Decimal

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from app import config, history_processor
from app.history_processor import format_description, process_history_entries
from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)


class HistoryProcessorTests(TestCase):
    """Test the history processor functions."""

    def test_get_verb_covers_all_media_types(self):
        """Test that get_verb covers all media types defined in MediaTypes."""
        # Get all media types from the MediaTypes enum
        for media_type in MediaTypes:
            # Ensure both present and past tense verbs are defined
            try:
                config.get_verb(media_type.value, past_tense=False)
                config.get_verb(media_type.value, past_tense=True)
            except KeyError:
                self.fail(f"Media type {media_type.name} not defined in get_verb")

    def test_format_description_status_initial(self):
        """Test format_description for initial status changes."""
        # Test initial status settings
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.IN_PROGRESS.value,
                MediaTypes.TV.value,
            ),
            "Marked as currently watching",
        )
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.COMPLETED.value,
                MediaTypes.MANGA.value,
            ),
            "Marked as finished reading",
        )
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.PLANNING.value,
                MediaTypes.GAME.value,
            ),
            "Added to playing list",
        )
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.DROPPED.value,
                MediaTypes.BOOK.value,
            ),
            "Marked as dropped",
        )
        self.assertEqual(
            format_description(
                "status",
                None,
                Status.PAUSED.value,
                MediaTypes.ANIME.value,
            ),
            "Marked as paused watching",
        )

    def test_format_description_status_transitions(self):
        """Test format_description for status transitions."""
        # Test status transitions
        self.assertEqual(
            format_description(
                "status",
                Status.PLANNING.value,
                Status.IN_PROGRESS.value,
                MediaTypes.TV.value,
            ),
            "Currently watching",
        )
        self.assertEqual(
            format_description(
                "status",
                Status.IN_PROGRESS.value,
                Status.COMPLETED.value,
                MediaTypes.MANGA.value,
            ),
            "Finished reading",
        )
        self.assertEqual(
            format_description(
                "status",
                Status.IN_PROGRESS.value,
                Status.PAUSED.value,
                MediaTypes.GAME.value,
            ),
            "Paused playing",
        )
        self.assertEqual(
            format_description(
                "status",
                Status.PAUSED.value,
                Status.IN_PROGRESS.value,
                MediaTypes.BOOK.value,
            ),
            "Resumed reading",
        )
        self.assertEqual(
            format_description(
                "status",
                Status.IN_PROGRESS.value,
                Status.DROPPED.value,
                MediaTypes.ANIME.value,
            ),
            "Stopped watching",
        )
        self.assertEqual(
            format_description("status", "Custom1", "Custom2", MediaTypes.TV.value),
            "Changed status from Custom1 to Custom2",
        )

    def test_format_description_score(self):
        """Test format_description for score changes."""
        # Initial score
        self.assertEqual(
            format_description("score", None, 8.5, MediaTypes.TV.value),
            "Rated 8.5/10",
        )
        self.assertEqual(
            format_description("score", 0, 7.0, MediaTypes.ANIME.value),
            "Rated 7/10",
        )
        # Score change
        self.assertEqual(
            format_description("score", 6.5, 8.0, MediaTypes.MOVIE.value),
            "Changed rating from 6.5 to 8",
        )
        self.assertEqual(
            format_description("score", 4, None, MediaTypes.MOVIE.value),
            "Removed rating",
        )
        self.assertEqual(
            format_description("score", Decimal("4.0"), Decimal("9.0")),
            "Changed rating from 4 to 9",
        )

    def test_format_description_progress(self):
        """Test format_description for progress changes."""
        # Initial progress
        self.assertEqual(
            format_description("progress", None, 120, MediaTypes.GAME.value),
            "Played for 2h 00min",
        )
        self.assertEqual(
            format_description("progress", None, 5, MediaTypes.BOOK.value),
            "Read up to page 5",
        )
        self.assertEqual(
            format_description("progress", None, 10, MediaTypes.MANGA.value),
            "Read up to chapter 10",
        )

        # Progress change
        self.assertEqual(
            format_description("progress", 60, 90, MediaTypes.GAME.value),
            "Added 30min of playtime",
        )
        self.assertEqual(
            format_description("progress", 90, 60, MediaTypes.GAME.value),
            "Removed 30min of playtime",
        )
        self.assertEqual(
            format_description("progress", 10, 15, MediaTypes.BOOK.value),
            "Progress set to 15 pages",
        )
        self.assertEqual(
            format_description("progress", 5, 10, MediaTypes.MANGA.value),
            "Progress set to 10 chapters",
        )

    def test_format_description_notes(self):
        """Test format_description for notes changes."""
        # Initial notes
        self.assertEqual(
            format_description("notes", None, "Test notes"),
            "Added notes",
        )

        # Update notes
        self.assertEqual(
            format_description("notes", "Old notes", "New notes"),
            "Updated notes",
        )

        # Remove notes
        self.assertEqual(
            format_description("notes", "Old notes", ""),
            "Removed notes",
        )

    def test_format_description_generic(self):
        """Test format_description for generic field changes."""
        self.assertEqual(
            format_description("custom_field", "old", "new"),
            "Updated custom field from old to new",
        )

    def test_repeats_read_in_their_media_types_words(self):
        """A rewatch's history names its statuses as the UI does."""
        user = get_user_model().objects.create_user(username="test")
        item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
        )
        movie = Movie.objects.create(
            item=item,
            user=user,
            status=Status.PLANNING_REPEAT.value,
        )
        movie.status = Status.REPEATING.value
        movie.save()

        descriptions = [
            change["description"]
            for entry in process_history_entries(
                movie.history.all(),
                MediaTypes.MOVIE.value,
                1,
                user,
            )
            for change in entry["changes"]
        ]

        self.assertEqual(
            descriptions,
            [
                "Changed status from Planning Rewatch to Rewatching",
                "Marked as planning rewatch",
            ],
        )


T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)


class ActivityFeedTests(TestCase):
    """Test the scoped feed, change structure, collapsing and summary."""

    def setUp(self):
        """Create a user and a movie."""
        self.user = get_user_model().objects.create_user(username="test")
        self.movie = self._movie("1")
        self._stamp(self.movie.history.first(), -60)

    def _movie(self, media_id, user=None):
        """Create a planning movie for ``user`` (default: the test user) at T0-60min."""
        user = user or self.user
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title=f"Movie {media_id}",
        )
        movie = Movie(item=item, user=user, status=Status.PLANNING.value)
        movie._history_user = user
        movie.save()
        self._stamp(movie.history.first(), -60)
        return movie

    def _edit(self, obj, minutes, **fields):
        """Save ``fields`` on ``obj`` and stamp its record ``minutes`` after T0."""
        for name, value in fields.items():
            setattr(obj, name, value)
        obj._history_user = self.user
        obj.save()
        self._stamp(obj.history.first(), minutes)

    def _stamp(self, record, minutes):
        """Set a history record's date to ``minutes`` after T0."""
        record.__class__.objects.filter(history_id=record.history_id).update(
            history_date=T0 + datetime.timedelta(minutes=minutes),
        )

    def _entries(self, scope=None, scope_type=None, limit=100):
        """Return the built, uncollapsed feed entries for ``scope``, newest first."""
        rows, _ = history_processor.get_journal_page(
            self.user,
            limit=limit,
            scope=scope,
        )
        return history_processor.build_journal_entries(rows, self.user, scope_type)

    def _show(self, user=None):
        """Create a show of two seasons with one watched episode in each."""
        user = user or self.user
        tv_item = Item.objects.create(
            media_id="9",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
        )
        tv = TV(item=tv_item, user=user, status=Status.PLANNING.value)
        tv._history_user = user
        tv.save()
        seasons, episodes = [], []
        for number in (1, 2):
            season_item = Item.objects.create(
                media_id="9",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Show",
                season_number=number,
            )
            season = Season(
                item=season_item,
                user=user,
                related_tv=tv,
                status=Status.IN_PROGRESS.value,
            )
            season._history_user = user
            season.save()
            episode_item = Item.objects.create(
                media_id="9",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Show",
                season_number=number,
                episode_number=number + 1,
            )
            episode = Episode(
                item=episode_item,
                related_season=season,
                status=Status.COMPLETED.value,
                end_date=T0,
            )
            episode._history_user = user
            Episode.save_base(episode)
            seasons.append(season)
            episodes.append(episode)
        for obj in (tv, *seasons, *episodes):
            for record in obj.history.all():
                self._stamp(record, -60)
        return tv, seasons, episodes

    def _scope(self, media_type, media_id, season_number=None, user=None):
        """Return the activity scope of the TMDB media made by ``_show``."""
        return history_processor.activity_scope(
            user or self.user,
            media_type,
            media_id,
            Sources.TMDB.value,
            season_number=season_number,
        )

    def test_scope_limits_page_and_keeps_keyset_order(self):
        """A scoped feed reads only those objects and pages without repeats."""
        other = self._movie("2")
        for minutes in range(1, 6):
            self._edit(self.movie, minutes, progress=minutes)
            self._edit(other, minutes, progress=minutes)
        scope = {MediaTypes.MOVIE.value: {self.movie.id}}

        seen, cursor = [], None
        while True:
            rows, has_next = history_processor.get_journal_page(
                self.user,
                limit=4,
                cursor=cursor,
                scope=scope,
            )
            seen.extend(rows)
            if not has_next:
                break
            cursor = rows[-1]

        records = apps.get_model("app", "historicalmovie").objects.filter(
            history_id__in=[row[2] for row in seen],
        )
        self.assertEqual({r.id for r in records}, {self.movie.id})
        self.assertEqual(len(seen), 6)
        self.assertEqual(seen, sorted(seen, key=lambda r: (r[0], r[2]), reverse=True))
        self.assertEqual(
            history_processor.get_journal_count(self.user, scope=scope),
            6,
        )

    def test_scope_of_season_and_show_hold_their_children(self):
        """A season's scope holds its episodes, a show's seasons and episodes."""
        tv, seasons, episodes = self._show()
        show = self._scope(MediaTypes.TV.value, "9")
        season = self._scope(MediaTypes.SEASON.value, "9", season_number=2)

        self.assertEqual(show[MediaTypes.TV.value], {tv.id})
        self.assertEqual(show[MediaTypes.SEASON.value], {s.id for s in seasons})
        self.assertEqual(show[MediaTypes.EPISODE.value], {e.id for e in episodes})
        self.assertEqual(season[MediaTypes.SEASON.value], {seasons[1].id})
        self.assertEqual(season[MediaTypes.EPISODE.value], {episodes[1].id})

    def test_scope_never_holds_another_users_entries(self):
        """Another user's show gives an empty scope and no activity."""
        other = get_user_model().objects.create_user(username="other")
        self._show(user=other)

        scope = self._scope(MediaTypes.TV.value, "9")

        self.assertEqual({k: v for k, v in scope.items() if v}, {})
        self.assertEqual(self._entries(scope), [])

    def test_context_label(self):
        """Seasons and episodes are labelled by where they sit under the media."""
        _tv, _seasons, _episodes = self._show()
        show = self._scope(MediaTypes.TV.value, "9")
        season = self._scope(MediaTypes.SEASON.value, "9", season_number=2)

        def labels(scope, scope_type):
            return sorted(
                (e["media_type"], e["context"])
                for e in self._entries(scope, scope_type)
            )

        self.assertEqual(
            sorted(set(labels(show, MediaTypes.TV.value))),
            [
                ("episode", "S1E2"),
                ("episode", "S2E3"),
                ("season", "S1"),
                ("season", "S2"),
                ("tv", ""),
            ],
        )
        self.assertEqual(
            labels(season, MediaTypes.SEASON.value),
            [("episode", "E3"), ("season", "")],
        )
        self.assertEqual({e["context"] for e in self._entries()}, {""})

    def test_status_and_score_changes_are_structured(self):
        """Status keeps old and new, scores read without ".0" and when cleared."""
        self._edit(self.movie, 1, status=Status.COMPLETED.value)
        self._edit(self.movie, 2, score=9)
        self._edit(self.movie, 3, score=4)
        self._edit(self.movie, 4, score=None)
        newest_first = [e["changes"] for e in self._entries()]

        removed, to_four, rated, finished, created = newest_first
        self.assertEqual(removed[0]["description"], "Removed rating")
        self.assertEqual(removed[0]["label"], "Removed rating")
        self.assertEqual(to_four[0]["description"], "Changed rating from 9 to 4")
        self.assertEqual(to_four[0]["label"], "9 → 4")
        self.assertEqual(rated[0]["description"], "Rated 9/10")
        self.assertEqual(rated[0]["label"], "Rated 9/10")
        self.assertEqual(
            (finished[0]["old"], finished[0]["new"]),
            (Status.PLANNING.value, Status.COMPLETED.value),
        )
        self.assertEqual(created[0]["new"], Status.PLANNING.value)
        self.assertIsNone(created[0]["old"])

    def _ratings(self, *edits):
        """Rate the movie at ``(minutes, score)`` and return the collapsed feed."""
        for minutes, score in edits:
            self._edit(self.movie, minutes, score=score)
        return history_processor.collapse_entries(self._entries(), self.user)

    def test_rapid_edits_fold_into_the_net_change(self):
        """Eight rating edits minutes apart show as one with the net change."""
        entries = self._ratings(*[(m, m) for m in range(1, 9)])

        folded = entries[0]
        self.assertEqual(len(entries), 2)  # the fold and the creation
        self.assertEqual(folded["count"], 8)
        self.assertEqual(len(folded["members"]), 8)
        self.assertEqual(folded["changes"][0]["label"], "Rated 8/10")
        self.assertEqual(folded["changes"][0]["description"], "Rated 8/10")
        self.assertEqual(folded["accent"], "score")

    def test_fold_net_change_between_two_scores_and_cleared(self):
        """The net change runs from the first old score to the last new one."""
        self._edit(self.movie, -30, score=2)
        entries = self._ratings((1, 5), (2, 7))
        self.assertEqual(entries[0]["changes"][0]["label"], "2 → 7")

        self._edit(self.movie, 3, score=None)
        entries = history_processor.collapse_entries(self._entries(), self.user)
        self.assertEqual(entries[0]["changes"][0]["label"], "Removed rating")
        self.assertEqual(entries[0]["count"], 3)

    def test_fold_with_no_net_change_says_so(self):
        """A run that ends where it started still shows, with its count."""
        self._edit(self.movie, -30, score=4)
        entries = self._ratings((1, 8), (2, 4))

        self.assertEqual(entries[0]["count"], 2)
        self.assertEqual(
            entries[0]["changes"][0]["description"],
            "Changed rating 2 times, back to 4/10",
        )

    def test_gap_other_field_or_object_breaks_the_run(self):
        """A gap over ten minutes, another field or another object doesn't fold."""
        other = self._movie("2")
        self._edit(self.movie, 1, score=3)
        self._edit(self.movie, 2, score=4)
        self._edit(self.movie, 13, score=5)  # 11 minutes after the last
        self._edit(self.movie, 14, notes="x")  # another field
        self._edit(self.movie, 15, score=6)
        self._edit(other, 16, score=1)  # another object between
        self._edit(self.movie, 17, score=7)
        self._edit(self.movie, 18, score=8)

        entries = history_processor.collapse_entries(self._entries(), self.user)

        shape = [
            (e["object_id"], e["changes"][0]["field"], e.get("count", 1))
            for e in entries
            if e["changes"][0]["field"] in ("score", "notes")
        ]
        movie, second = self.movie.id, other.id
        self.assertEqual(
            shape,
            [
                (movie, "score", 2),  # 17 and 18
                (second, "score", 1),
                (movie, "score", 1),  # 15
                (movie, "notes", 1),
                (movie, "score", 1),  # 13
                (movie, "score", 2),  # 1 and 2
            ],
        )

    def test_summary_for_a_movie(self):
        """A movie's summary has its first and last activity and its finishes."""
        self._edit(self.movie, 5, status=Status.COMPLETED.value)
        self._edit(self.movie, 30, score=8)
        self._stamp(self.movie.history.last(), 0)

        scope = {MediaTypes.MOVIE.value: {self.movie.id}}
        summary = history_processor.activity_summary(
            self.user,
            MediaTypes.MOVIE.value,
            scope,
            [self.movie],
        )

        self.assertEqual(summary["first"], T0)
        self.assertEqual(summary["last"], T0 + datetime.timedelta(minutes=30))
        self.assertEqual(summary["finished"], 1)

    def test_summary_for_a_season_and_a_show_counts_plays_and_spans_children(self):
        """Season and show summaries span their children and count plays."""
        tv, seasons, episodes = self._show()
        delta = datetime.timedelta
        for record in tv.history.all():
            self._stamp(record, -1000)
        for record in seasons[1].history.all():
            self._stamp(record, -500)
        for record in episodes[1].history.all():
            self._stamp(record, 1000)

        def summary(media_type, entries, scope):
            return history_processor.activity_summary(
                self.user,
                media_type,
                scope,
                entries,
            )

        show = summary(
            MediaTypes.TV.value,
            [tv],
            self._scope(MediaTypes.TV.value, "9"),
        )
        season = summary(
            MediaTypes.SEASON.value,
            [seasons[1]],
            self._scope(MediaTypes.SEASON.value, "9", season_number=2),
        )

        self.assertEqual(show["first"], T0 - delta(minutes=1000))
        self.assertEqual(show["last"], T0 + delta(minutes=1000))
        self.assertEqual(season["first"], T0 - delta(minutes=500))
        self.assertEqual(season["last"], T0 + delta(minutes=1000))
        self.assertEqual(show["finished"], tv.plays)
        self.assertEqual(season["finished"], seasons[1].plays)
        self.assertEqual(tv.plays, 1)

        unwatched = self._movie("3")
        self.assertEqual(
            summary(
                MediaTypes.MOVIE.value,
                [unwatched],
                {MediaTypes.MOVIE.value: {unwatched.id}},
            )["finished"],
            0,
        )

    def test_a_shows_page_takes_a_constant_number_of_queries(self):
        """A page of 30 show entries costs the queries of a page of 5."""
        _tv, seasons, _episodes = self._show()
        for number in range(10, 40):
            item = Item.objects.create(
                media_id="9",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Show",
                season_number=1,
                episode_number=number,
            )
            episode = Episode(
                item=item,
                related_season=seasons[0],
                status=Status.COMPLETED.value,
                end_date=T0,
            )
            episode._history_user = self.user
            Episode.save_base(episode)
            # An update record each, so every entry looks up its predecessor.
            episode.notes = "x"
            episode._history_user = self.user
            episode.save_base()

        def run(limit):
            rows, _ = history_processor.get_journal_page(
                self.user,
                limit=limit,
                scope=self._scope(MediaTypes.TV.value, "9"),
            )
            entries = history_processor.collapse_entries(
                history_processor.build_journal_entries(
                    rows,
                    self.user,
                    MediaTypes.TV.value,
                ),
                self.user,
            )
            return len(entries)

        with CaptureQueriesContext(connection) as small:
            small_count = run(5)
        with self.assertNumQueries(len(small)):
            large_count = run(30)
        self.assertEqual((small_count, large_count), (5, 30))

    def test_exactly_ten_minutes_apart_still_folds(self):
        """The window is inclusive: edits exactly 10 minutes apart fold."""
        entries = self._ratings((1, 3), (11, 4))
        self.assertEqual(entries[0]["count"], 2)

    def test_episode_edits_never_fold(self):
        """Two edits of one episode within the window stay two entries."""
        _tv, _seasons, episodes = self._show()
        episode = episodes[0]
        for minutes in (1, 2):
            episode.notes = f"n{minutes}"
            episode._history_user = self.user
            episode.save_base()
            self._stamp(episode.history.first(), minutes)

        entries = history_processor.collapse_entries(
            self._entries({MediaTypes.EPISODE.value: {episode.id}}),
            self.user,
        )
        mine = [e for e in entries if e["object_id"] == episode.id]
        self.assertEqual(len(mine), 3)
        self.assertNotIn("members", mine[0])

    def test_episode_status_change_carries_old(self):
        """An episode moved from one status to another keeps the old status."""
        _tv, _seasons, episodes = self._show()
        episode = episodes[0]
        episode.status = Status.PAUSED.value
        episode._history_user = self.user
        episode.save_base()
        self._stamp(episode.history.first(), 5)

        entry = self._entries(
            {MediaTypes.EPISODE.value: {episode.id}},
        )[0]
        self.assertEqual(
            (entry["changes"][0]["old"], entry["changes"][0]["new"]),
            (Status.COMPLETED.value, Status.PAUSED.value),
        )

    def test_status_fold_takes_the_newest_accent(self):
        """A run of status edits reads as its last status."""
        self._edit(self.movie, 1, status=Status.IN_PROGRESS.value)
        self._edit(self.movie, 2, status=Status.COMPLETED.value)

        entry = history_processor.collapse_entries(self._entries(), self.user)[0]

        self.assertEqual(entry["accent"], Status.COMPLETED.value)

    def test_fold_back_to_a_date_uses_the_users_format(self):
        """A "back to" names a date as the rest of the feed does."""
        d1 = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=datetime.UTC)
        d2 = datetime.datetime(2026, 9, 2, 8, 0, tzinfo=datetime.UTC)
        self._edit(self.movie, -30, end_date=d1)
        self._edit(self.movie, 1, end_date=d2)
        self._edit(self.movie, 2, end_date=d1)

        description = history_processor.collapse_entries(self._entries(), self.user)[0][
            "changes"
        ][0]["description"]

        self.assertTrue(description.startswith("Changed end date 2 times, back to "))
        self.assertNotIn("+00:00", description)
