from unittest.mock import patch
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    Anime,
    Book,
    Episode,
    Game,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from events.models import Event


class HomeViewTests(TestCase):
    """Test the home view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.metadata_patcher = patch("app.providers.services.get_media_metadata")
        self.mock_get_media_metadata = self.metadata_patcher.start()
        self.addCleanup(self.metadata_patcher.stop)

        def mock_get_media_metadata(
            media_type,
            _media_id,
            _source,
            season_numbers=None,
            _episode_number=None,
        ):
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "details": {"seasons": 1},
                    "related": {
                        "seasons": [
                            {
                                "season_number": 1,
                                "image": "http://example.com/image.jpg",
                            },
                        ],
                    },
                }

            if media_type == "tv_with_seasons":
                season_number = season_numbers[0]
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "details": {"seasons": 1},
                    f"season/{season_number}": {
                        "episodes": [{"id": i} for i in range(1, 11)],
                    },
                    "related": {
                        "seasons": [
                            {
                                "season_number": season_number,
                                "image": "http://example.com/image.jpg",
                            },
                        ],
                    },
                }

            if media_type == MediaTypes.SEASON.value:
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 10,
                    "season/1": {
                        "episodes": [{"id": i} for i in range(1, 11)],
                    },
                }

            if media_type == MediaTypes.ANIME.value:
                return {
                    "title": "Test Anime",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 24,
                }

            if media_type == MediaTypes.MOVIE.value:
                return {
                    "title": "Planned Movie",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 1,
                }

            return {"max_progress": None}

        self.mock_get_media_metadata.side_effect = mock_get_media_metadata

        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        base_watched_at = timezone.now()
        for i in range(1, 6):  # Create 5 episodes
            episode_item = Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Test TV Show",
                image="http://example.com/image.jpg",
                season_number=1,
                episode_number=i,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=base_watched_at - timezone.timedelta(days=6 - i),
            )

        anime_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )
        Anime.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=10,
        )
        Event.objects.create(
            item=anime_item,
            content_number=16,
            datetime=timezone.now() - timezone.timedelta(days=1),
        )
        Event.objects.create(
            item=anime_item,
            content_number=17,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

        movie_item = Item.objects.create(
            media_id="10",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Planned Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        # Episode 6 is out to watch, and episode 7 comes out tomorrow
        Event.objects.create(
            item=season_item,
            content_number=6,
            datetime=timezone.now() - timezone.timedelta(days=1),
        )
        Event.objects.create(
            item=season_item,
            content_number=7,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

    def rows_by_key(self, response):
        """Return the home rows of a response by status and media type."""
        return {
            ("caught-up" if row["caught_up"] else row["status"], row["media_type"]): row
            for row in response.context["home_rows"]
        }

    def test_home_view(self):
        """The home page has a row per status and media type with entries."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/home.html")

        rows = self.rows_by_key(response)
        self.assertEqual(
            list(rows),
            [
                (Status.IN_PROGRESS.value, MediaTypes.SEASON.value),
                (Status.IN_PROGRESS.value, MediaTypes.ANIME.value),
                (Status.PLANNING.value, MediaTypes.MOVIE.value),
            ],
        )

        season_row = rows[(Status.IN_PROGRESS.value, MediaTypes.SEASON.value)]
        self.assertEqual(season_row["total"], 1)
        self.assertEqual(season_row["items"][0].progress, 5)

    def test_row_headers_open_their_list_in_the_same_order(self):
        """Each row's header opens its media list filtered and ordered like the row.

        Seasons have no list of their own, so theirs opens their shows' list.
        """
        response = self.client.get(reverse("home"))

        for row in response.context["home_rows"]:
            is_season = row["media_type"] == MediaTypes.SEASON.value
            list_type = MediaTypes.TV.value if is_season else row["media_type"]
            link = (
                reverse("medialist", args=[self.user.username, list_type])
                + f"?status={quote(row['status'])}&sort=recent"
            )
            self.assertContains(response, f'href="{link}"')

            list_response = self.client.get(link)

            self.assertEqual(list_response.status_code, 200, link)
            if is_season:
                continue
            listed = [media.id for media in list_response.context["media_list"]]
            self.assertEqual(listed[: len(row["items"])], [m.id for m in row["items"]])

    def test_tv_list_can_be_sorted_by_recent(self):
        """Shows, whose last activity comes from their episodes, sort by Recent."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.TV.value]),
            {"sort": "recent"},
        )

        self.assertEqual(response.status_code, 200)

    def test_rows_list_recent_activity_first(self):
        """A row lists the most recently updated entries first."""
        quiet = Item.objects.create(
            media_id="quiet",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Zero Episodes Anime",
            image="http://example.com/image.jpg",
        )
        Anime.objects.create(
            item=quiet, user=self.user, status=Status.IN_PROGRESS.value
        )

        def first_anime():
            response = self.client.get(reverse("home"))
            row = self.rows_by_key(response)[
                (Status.IN_PROGRESS.value, MediaTypes.ANIME.value)
            ]
            return row["items"][0].item.media_id

        # The newest entry has no releases or progress, so only Recent puts it first
        self.assertEqual(first_anime(), "quiet")

        busy = Anime.objects.get(item__media_id="1", user=self.user)
        busy.progress = 11
        busy.save()

        self.assertEqual(first_anime(), "1")

    def test_rewatches_have_their_own_rows(self):
        """Rewatches get rows after their first watch counterparts, in their words."""
        for media_id, status in (
            ("rewatching", Status.REPEATING.value),
            ("reread", Status.PLANNING_REPEAT.value),
        ):
            item = Item.objects.create(
                media_id=media_id,
                source=Sources.MANUAL.value,
                media_type=MediaTypes.BOOK.value,
                title=media_id,
                image="http://example.com/image.jpg",
            )
            Book.objects.create(item=item, user=self.user, status=status)

        response = self.client.get(reverse("home"))

        self.assertEqual(
            list(self.rows_by_key(response)),
            [
                (Status.IN_PROGRESS.value, MediaTypes.SEASON.value),
                (Status.IN_PROGRESS.value, MediaTypes.ANIME.value),
                (Status.REPEATING.value, MediaTypes.BOOK.value),
                (Status.PLANNING.value, MediaTypes.MOVIE.value),
                (Status.PLANNING_REPEAT.value, MediaTypes.BOOK.value),
            ],
        )
        self.assertContains(response, "Rereading")
        self.assertContains(response, "Planning Reread")

    def test_caught_up_media_has_its_own_row(self):
        """Media watched up to its latest release waits in Caught Up, after Planning."""
        Anime.objects.filter(user=self.user, item__media_id="1").update(progress=16)

        response = self.client.get(reverse("home"))

        rows = self.rows_by_key(response)
        self.assertEqual(
            list(rows),
            [
                (Status.IN_PROGRESS.value, MediaTypes.SEASON.value),
                (Status.PLANNING.value, MediaTypes.MOVIE.value),
                ("caught-up", MediaTypes.ANIME.value),
            ],
        )
        self.assertContains(response, f'href="{reverse("calendar")}"')
        self.assertRegex(
            response.content.decode(), r"Caught Up\s*<span[^>]*>•</span>\s*Anime"
        )

    def test_caught_up_card_has_a_release_button_and_no_progress_bar(self):
        """A caught up card offers its next release, and a full bar says nothing."""
        anime = Anime.objects.get(user=self.user, item__media_id="1")
        anime.progress = 16
        anime.save()

        response = self.client.get(reverse("home"))

        card = response.content.decode().split(f'id="home-media-anime-{anime.id}"')[1]
        card = card.split('id="home-media-')[0]
        event = Event.objects.get(item=anime.item, content_number=17)
        self.assertIn(reverse("download_event", args=[event.id]), card)
        self.assertNotIn("card-chip flex min-w-0", card)
        self.assertNotIn("h-1 bg-", card)

    def track_show(self, media_id, seasons):
        """Track a show by its seasons' numbers, statuses and next release days."""
        tv_item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=media_id,
            image="http://example.com/image.jpg",
        )
        tv = TV(item=tv_item, user=self.user, status=Status.PLANNING.value)
        TV.save_base(tv)
        for number, status, release_in_days in seasons:
            item = Item.objects.create(
                media_id=media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title=media_id,
                image="http://example.com/image.jpg",
                season_number=number,
            )
            Season.save_base(
                Season(item=item, user=self.user, related_tv=tv, status=status),
            )
            if release_in_days is not None:
                Event.objects.create(
                    item=item,
                    content_number=1,
                    datetime=timezone.now() + timezone.timedelta(days=release_in_days),
                )

    def test_renewed_shows_wait_in_caught_up_by_next_release(self):
        """A finished show's unaired season is caught up, soonest first."""
        # Tracked first, so most recent first would list it last
        self.track_show(
            "renewed-sooner",
            [(1, Status.COMPLETED.value, None), (2, Status.PLANNING.value, 3)],
        )
        self.track_show(
            "renewed-later",
            [(1, Status.COMPLETED.value, None), (2, Status.PLANNING.value, 30)],
        )
        # Never started, so it's still only planned
        self.track_show("premiere", [(1, Status.PLANNING.value, 10)])

        rows = self.rows_by_key(self.client.get(reverse("home")))

        caught_up = rows[("caught-up", MediaTypes.SEASON.value)]
        self.assertEqual(
            [season.item.media_id for season in caught_up["items"]],
            ["renewed-sooner", "renewed-later"],
        )
        # Its card counts its episodes rather than showing no progress
        self.assertContains(self.client.get(reverse("home")), "1 Episode")
        planned = rows[(Status.PLANNING.value, MediaTypes.SEASON.value)]
        self.assertEqual(
            [season.item.media_id for season in planned["items"]],
            ["premiere"],
        )

    def test_caught_up_seasons_and_repeats_leave_their_rows(self):
        """Seasons airing weekly and repeats caught up wait in Caught Up instead."""
        # Every released episode of the season is watched, the next airs tomorrow
        Event.objects.filter(
            item__media_type=MediaTypes.SEASON.value,
            content_number=6,
        ).update(content_number=5)
        Anime.objects.filter(user=self.user, item__media_id="1").update(
            status=Status.REPEATING.value,
            progress=16,
        )

        rows = self.rows_by_key(self.client.get(reverse("home")))

        self.assertEqual(
            list(rows),
            [
                (Status.PLANNING.value, MediaTypes.MOVIE.value),
                ("caught-up", MediaTypes.SEASON.value),
                ("caught-up", MediaTypes.ANIME.value),
            ],
        )

    def test_planned_rewatches_list_each_shows_first_season(self):
        """A show planned to be rewatched has one card, its first season's."""
        self.track_show(
            "rewatch",
            [
                (2, Status.PLANNING_REPEAT.value, None),
                (1, Status.PLANNING_REPEAT.value, None),
            ],
        )

        rows = self.rows_by_key(self.client.get(reverse("home")))

        row = rows[(Status.PLANNING_REPEAT.value, MediaTypes.SEASON.value)]
        self.assertEqual(
            [season.item.season_number for season in row["items"]],
            [1],
        )

    def test_planned_shows_list_their_first_season(self):
        """A planned show's first planned season stands for it, with its episodes."""
        tv_item = Item.objects.create(
            media_id="planned-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Planned Show",
            image="http://example.com/image.jpg",
        )
        tv = TV(item=tv_item, user=self.user, status=Status.PLANNING.value)
        TV.save_base(tv)
        for number in (3, 2):
            item = Item.objects.create(
                media_id="planned-show",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Planned Show",
                image="http://example.com/image.jpg",
                season_number=number,
            )
            Season.save_base(
                Season(
                    item=item,
                    user=self.user,
                    related_tv=tv,
                    status=Status.PLANNING.value,
                ),
            )
            Event.objects.create(
                item=item,
                content_number=8,
                datetime=timezone.now() - timezone.timedelta(days=10),
            )

        response = self.client.get(reverse("home"))

        row = self.rows_by_key(response)[
            (Status.PLANNING.value, MediaTypes.SEASON.value)
        ]
        self.assertEqual(
            [season.item.season_number for season in row["items"]],
            [2],
        )
        self.assertEqual(row["total"], 1)
        self.assertContains(response, "8 Episodes")
        # Seasons' rows are named for TV
        self.assertNotContains(response, "TV Seasons")

    def test_paused_entries_have_no_row(self):
        """Only in progress and planned entries get rows."""
        paused = Item.objects.create(
            media_id="paused",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Paused Manga",
            image="http://example.com/image.jpg",
        )
        Manga.objects.create(item=paused, user=self.user, status=Status.PAUSED.value)

        response = self.client.get(reverse("home"))

        self.assertNotIn(
            MediaTypes.MANGA.value,
            [row["media_type"] for row in response.context["home_rows"]],
        )

    def test_empty_home_page(self):
        """Someone tracking nothing gets an empty state instead of rows."""
        new = {"username": "new", "password": self.credentials["password"]}
        get_user_model().objects.create_user(**new)
        self.client.login(**new)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.context["home_rows"], [])
        self.assertContains(response, "Nothing in progress or planned yet")

    def test_rows_hold_twenty_entries(self):
        """A row shows its first 20 entries and counts them all."""
        for i in range(25):
            item = Item.objects.create(
                media_id=f"many-{i}",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.ANIME.value,
                title=f"Anime {i}",
                image="http://example.com/image.jpg",
            )
            Anime.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

        response = self.client.get(reverse("home"))

        anime_row = self.rows_by_key(response)[
            (Status.IN_PROGRESS.value, MediaTypes.ANIME.value)
        ]
        self.assertEqual(len(anime_row["items"]), 20)
        self.assertEqual(anime_row["total"], 26)

    def test_unreleased_media_is_shown_with_the_old_hide_setting(self):
        """Unreleased media stays on the home page now that it can't be hidden."""
        self.user.home_hide_unreleased = True
        self.user.save(update_fields=["home_hide_unreleased"])
        upcoming_item = Item.objects.create(
            media_id="planning-with-release",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Upcoming Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=upcoming_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Event.objects.create(
            item=upcoming_item,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

        response = self.client.get(reverse("home"))

        movies = self.rows_by_key(response)[
            (Status.PLANNING.value, MediaTypes.MOVIE.value)
        ]
        self.assertIn(
            "planning-with-release",
            [media.item.media_id for media in movies["items"]],
        )

    def test_unreleased_card_offers_release_instead_of_action(self):
        """An unreleased planned movie shows its release date, not a status action."""
        upcoming_item = Item.objects.create(
            media_id="unreleased-card",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Unreleased Movie",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=upcoming_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        release = Event.objects.create(
            item=upcoming_item,
            datetime=timezone.now() + timezone.timedelta(days=30),
        )

        response = self.client.get(reverse("home"))

        card = response.content.decode().split(f'id="home-media-movie-{movie.id}"')[1]
        card = card.split('id="home-media-')[0]
        self.assertIn(reverse("download_event", args=[release.id]), card)
        self.assertNotIn("Mark watched", card)

    def test_game_card_shows_no_time_played(self):
        """A game card on the home page leaves out its time played."""
        item = Item.objects.create(
            media_id="game-card",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Played Game",
            image="http://example.com/image.jpg",
        )
        game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=150,
        )

        response = self.client.get(reverse("home"))

        card = response.content.decode().split(f'id="home-media-game-{game.id}"')[1]
        card = card.split('id="home-media-')[0]
        self.assertIn("Played Game", card)
        self.assertNotIn("30min", card)

    def test_cards_cost_no_query_each(self):
        """Home costs the same number of queries however many cards it shows."""

        def add(media_type, model, number, **fields):
            """Create a tracked item of the model for the user."""
            item = Item.objects.create(
                media_id=f"cost-{number}",
                source=Sources.MANUAL.value,
                media_type=media_type,
                title=f"Cost {number}",
                image="http://example.com/image.jpg",
            )
            model.objects.create(item=item, user=self.user, **fields)

        def home_queries():
            """Return how many queries one home request runs."""
            with CaptureQueriesContext(connection) as queries:
                self.client.get(reverse("home"))
            return len(queries)

        add(
            MediaTypes.GAME.value, Game, 0, status=Status.IN_PROGRESS.value, progress=60
        )
        add(
            MediaTypes.ANIME.value,
            Anime,
            0,
            status=Status.IN_PROGRESS.value,
            progress=2,
        )
        add(MediaTypes.MOVIE.value, Movie, 0, status=Status.PLANNING.value)
        few = home_queries()

        for number in range(1, 4):
            add(
                MediaTypes.GAME.value,
                Game,
                number,
                status=Status.IN_PROGRESS.value,
                progress=60,
            )
            add(
                MediaTypes.ANIME.value,
                Anime,
                number,
                status=Status.IN_PROGRESS.value,
                progress=2,
            )
            add(MediaTypes.MOVIE.value, Movie, number, status=Status.PLANNING.value)

        self.assertEqual(home_queries(), few)

    def test_movie_card_shows_its_release_year(self):
        """A movie card on the home page shows the year it came out."""
        released_item = Item.objects.create(
            media_id="released-card",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Released Movie",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=released_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Event.objects.create(
            item=released_item,
            datetime=timezone.now().replace(year=1999),
        )

        response = self.client.get(reverse("home"))

        card = response.content.decode().split(f'id="home-media-movie-{movie.id}"')[1]
        card = card.split('id="home-media-')[0]
        self.assertIn("<span>1999</span>", card)

    def test_card_keeps_its_id_chip_and_hover_action(self):
        """A released card with more on the way has its id, next release and action."""
        item = Item.objects.create(
            media_id="chip-card",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Chip Anime",
            image="http://example.com/image.jpg",
        )
        anime = Anime.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=1,
        )
        Event.objects.create(
            item=item,
            content_number=2,
            datetime=timezone.now() - timezone.timedelta(days=1),
        )
        Event.objects.create(
            item=item,
            content_number=3,
            datetime=timezone.now() + timezone.timedelta(days=30),
        )

        response = self.client.get(reverse("home"))

        card = response.content.decode().split(f'id="home-media-anime-{anime.id}"')[1]
        card = card.split('id="home-media-')[0]
        self.assertIn("card-chip flex min-w-0", card)
        self.assertIn("text-indigo-300", card)
        self.assertIn('<div class="hover-reveal">', card)
        self.assertIn(reverse("progress_edit", args=["anime", anime.id]), card)
        self.assertNotIn("download_event", card)

    def test_home_and_library_cards_share_their_caption(self):
        """The same entry gets the same title and details on home and elsewhere."""
        item = Item.objects.create(
            media_id="caption-card",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Caption Anime",
            image="http://example.com/image.jpg",
        )
        anime = Anime.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=3,
        )
        anime.max_progress = 12

        def caption(**context):
            """Return the caption markup of a card rendered with the context."""
            html = render_to_string(
                "app/components/media_card.html",
                {
                    "item": item,
                    "media": anime,
                    "title": anime,
                    "user": self.user,
                    "home_status": Status.IN_PROGRESS.value,
                }
                | context,
            )
            html = html.split("<a href=", 2)[2]
            return html.split("</a>", 1)[1].split("</div>", 1)[0]

        home = caption(variant="home")
        self.assertIn("3 / 12", home)
        self.assertEqual(home, caption())
