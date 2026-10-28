import re
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from app import config
from app.models import (
    TV,
    Anime,
    BasicMedia,
    Book,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.templatetags.app_tags import release_label
from events.models import Event

CHIP_POSITIONS = ("grid", "row", "home")


def icon_paths(icon):
    """Return the path data of an icon, which identifies it in rendered HTML."""
    return re.findall(r' d="([^"]+)"', render_to_string(icon, {"classes": ""}))


class ActionButtonTests(TestCase):
    """Test that every position draws the same action for the same entry."""

    def setUp(self):
        """Create a user to render the buttons for."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def item(self, media_type, media_id="1"):
        """Return a stored manual item."""
        return Item.objects.create(
            media_id=media_id,
            source=Sources.MANUAL.value,
            media_type=media_type,
            title=f"Test {media_type}",
            image="http://example.com/image.jpg",
            season_number=1 if media_type == MediaTypes.SEASON.value else None,
        )

    def render(self, size, card, ref, entry, **context):
        """Render the action button as a position includes it."""
        request = RequestFactory().get("/page?a=b")
        request.user = self.user
        return render_to_string(
            "app/components/action_button.html",
            {
                "size": size,
                "card": card,
                "ref": ref,
                "entry": entry,
                "user": self.user,
                "request": request,
                **context,
            },
        )

    def positions(self, item, entry, **large_context):
        """Return the rendered button of each position, by name."""
        rendered = {
            card: self.render("chip", card, item, entry) for card in CHIP_POSITIONS
        }
        rendered["details"] = self.render(
            "large", "details", item, entry, **large_context
        )
        return rendered

    def assertAction(self, html, action, icon):  # noqa: N802
        """Assert a button posts `action`, drawn as the status it leads to."""
        self.assertIn(f'"action": "{action.key}"', html)
        self.assertIn(f'aria-label="{action.label}"', html)
        self.assertIn(reverse("quick_action"), html)
        self.assertIn(config.get_status_button_color(action.target_status), html)
        for path in icon_paths(icon):
            self.assertIn(path, html)

    def test_planned_movie_marks_watched_the_same_everywhere(self):
        """A planned movie's chip, row, home card and details page complete it."""
        item = self.item(MediaTypes.MOVIE.value)
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        for html in self.positions(item, movie).values():
            self.assertAction(
                html,
                SimpleAction("complete", "Mark watched", Status.COMPLETED.value),
                "app/icons/states/completed.svg",
            )

    def test_watched_movie_plans_a_rewatch_the_same_everywhere(self):
        """A watched movie plans a rewatch, as planning repeat, in every position."""
        item = self.item(MediaTypes.MOVIE.value)
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        other = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        rendered = self.positions(item, movie, all_entries=[movie, other])

        for html in rendered.values():
            self.assertAction(
                html,
                SimpleAction(
                    "plan_repeat",
                    "Plan to rewatch",
                    Status.PLANNING_REPEAT.value,
                ),
                "app/icons/states/planning-repeat.svg",
            )
        # Resting, chips show it is watched; the large button says how often
        for card in CHIP_POSITIONS:
            self.assertIn(
                config.get_status_text_color(Status.COMPLETED.value), rendered[card]
            )
        self.assertIn("Watched 2 times", rendered["details"])
        self.assertIn(
            config.get_status_button_color(Status.COMPLETED.value), rendered["details"]
        )

    def test_logged_media_opens_the_log_the_same_everywhere(self):
        """A book in progress opens its log dialog in every position."""
        item = self.item(MediaTypes.BOOK.value)
        book = Book.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        for html in self.positions(item, book).values():
            self.assertIn(reverse("log_modal", args=["book", book.id]), html)
            self.assertIn('aria-label="Log pages"', html)
            self.assertIn("open-log", html)
            self.assertNotIn(reverse("quick_action"), html)

    def test_in_progress_with_a_release_and_no_total_waits_everywhere(self):
        """In progress media with no known total, caught up on a release, waits.

        Details pages show the release, not the action, as the cards do.
        """
        item = self.item(MediaTypes.ANIME.value)
        Anime.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=3,
        )
        release = Event.objects.create(
            item=item,
            content_number=4,
            datetime=timezone.now() + timedelta(days=3),
        )
        entry = BasicMedia.objects.filter_media_prefetch(
            self.user,
            item.media_id,
            item.media_type,
            item.source,
        )[0]
        self.assertIsNone(entry.max_progress)

        rendered = self.positions(item, entry)

        label = release_label(release, self.user)
        for card in CHIP_POSITIONS:
            self.assertIn(reverse("download_event", args=[release.id]), rendered[card])
            self.assertIn(label, rendered[card])
        self.assertIn(label, rendered["details"])
        for html in rendered.values():
            self.assertNotIn("+1 episode", html)
            self.assertNotIn(reverse("quick_action"), html)
        # Caught up, it waits in planning, whose colors its clock takes
        self.assertIn(
            config.get_status_button_color(Status.PLANNING.value),
            rendered["details"],
        )
        self.assertIn("text-sky-400", rendered["grid"])

    def test_unreleased_season_acts_like_any_other_until_planned(self):
        """An untracked season is planned; once planned, it waits for its release."""
        item = self.item(MediaTypes.SEASON.value)
        for position in self.positions(item, None).values():
            self.assertIn('aria-label="Watchlist"', position)

        tv_item = self.item(MediaTypes.TV.value)
        tv = TV(item=tv_item, user=self.user, status=Status.PLANNING.value)
        TV.save_base(tv)
        season = Season(
            item=item,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        Season.save_base(season)
        release = Event.objects.create(
            item=item,
            content_number=1,
            datetime=timezone.now() + timedelta(days=30),
        )
        entry = BasicMedia.objects.filter_media_prefetch(
            self.user,
            item.media_id,
            item.media_type,
            item.source,
            season_number=1,
        )[0]
        self.assertEqual(entry, season)

        rendered = self.positions(item, entry)

        for card in CHIP_POSITIONS:
            self.assertIn(reverse("download_event", args=[release.id]), rendered[card])
        self.assertIn(release_label(release, self.user), rendered["details"])
        for html in rendered.values():
            self.assertNotIn(reverse("quick_action"), html)

    def test_finish_button_is_only_on_details_pages(self):
        """The finish button comes with the details page's action, nowhere else."""
        item = self.item(MediaTypes.BOOK.value)
        book = Book.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        page = {"finish_ref": item, "finish_entry": book}

        details = self.render("large", "details", item, book, **page)
        without = self.render("large", "details", item, book)
        chips = [self.render("chip", card, item, book) for card in CHIP_POSITIONS]

        self.assertIn('"finish": "1"', details)
        self.assertIn('aria-label="Mark read"', details)
        self.assertNotIn('"finish": "1"', without)
        for html in chips:
            self.assertNotIn('"finish": "1"', html)

    def test_others_cards_show_their_status_without_an_action(self):
        """Someone else's entry shows its status, and posts nothing."""
        item = self.item(MediaTypes.MOVIE.value)
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        html = self.render("chip", "grid", item, movie, readonly=True)

        self.assertIn('title="Planning"', html)
        self.assertNotIn("hx-post", html)
        self.assertNotIn("quick_action", html)

    def test_untracked_media_offers_to_plan_it(self):
        """Untracked media shows a plus, which turns into planning it."""
        item = self.item(MediaTypes.MOVIE.value)

        for html in self.positions(item, None).values():
            self.assertAction(
                html,
                SimpleAction("plan", "Watchlist", Status.PLANNING.value),
                "app/icons/states/planning.svg",
            )

    def test_chip_taps_pass_through_to_clickable_cards(self):
        """A card that is a link on touch screens leaves its chip to the link."""
        item = self.item(MediaTypes.MOVIE.value)
        self.user.clickable_media_cards = True

        on_card = self.render("chip", "grid", item, None)
        off_card = self.render("chip", "details", item, None, on_card=False)

        self.assertIn("pointer-coarse:pointer-events-none", on_card)
        self.assertNotIn("pointer-coarse:pointer-events-none", off_card)

    def test_home_actions_wait_for_hover_and_others_do_not(self):
        """Only a home card's action is revealed on hover."""
        item = self.item(MediaTypes.MOVIE.value)

        self.assertIn("hover-reveal", self.render("chip", "home", item, None))
        self.assertNotIn("hover-reveal", self.render("chip", "grid", item, None))

    def test_rows_post_the_filter_form_along(self):
        """A list sorted by a filter has its form posted with the action."""
        item = self.item(MediaTypes.MOVIE.value)

        html = self.render("chip", "row", item, None, filtered=True)

        self.assertIn('hx-include="#filter-form"', html)
        self.assertIn("closest .media-row", html)

    def test_others_cards_through_the_card_template_have_no_action(self):
        """A card of someone else's entry, as the card template draws it."""
        item = self.item(MediaTypes.MOVIE.value)
        other = get_user_model().objects.create_user(username="other")
        movie = Movie.objects.create(
            item=item,
            user=other,
            status=Status.PLANNING.value,
        )
        request = RequestFactory().get("/")
        request.user = self.user

        html = render_to_string(
            "app/components/media_card.html",
            {
                "item": item,
                "title": item.title,
                "media": movie,
                "user": self.user,
                "target_user": other,
                "request": request,
            },
        )

        self.assertIn('title="Planning"', html)
        self.assertNotIn("quick_action", html)
        self.assertNotIn("hx-post", html)

    def form(self, item, entry):
        """Return what the large no-action button needs to open the tracking form."""
        return {
            "form_url": reverse(
                "track_modal",
                args=[item.source, item.media_type, item.media_id],
            ),
            "form_target": "#track-box",
            "form_entry": entry,
        }

    def test_dropped_media_shows_its_status_and_opens_the_form(self):
        """Dropped media shows its status, and its page opens the form."""
        item = self.item(MediaTypes.MOVIE.value)
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.DROPPED.value,
        )

        rendered = self.positions(item, movie, **self.form(item, movie))

        for card in CHIP_POSITIONS:
            self.assertIn('title="Dropped"', rendered[card])
            self.assertIn(
                config.get_status_text_color(Status.DROPPED.value),
                rendered[card],
            )
            self.assertNotIn("hx-post", rendered[card])
        large = rendered["details"]
        self.assertIn("Dropped", large)
        self.assertIn(config.get_status_button_color(Status.DROPPED.value), large)
        self.assertIn(f'"instance_id": "{movie.id}"', large)
        self.assertIn('hx-target="#track-box"', large)
        self.assertIn(self.form(item, movie)["form_url"], large)
        self.assertNotIn("quick_action", large)

    def test_paused_media_resumes_the_same_everywhere(self):
        """Paused media offers to resume it, in every position."""
        item = self.item(MediaTypes.MOVIE.value)
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.PAUSED.value,
        )

        for html in self.positions(item, movie).values():
            self.assertIn('"action": "resume"', html)
            self.assertIn('aria-label="Resume watching"', html)

    def test_untracked_media_with_no_action_opens_the_form_to_add(self):
        """The large button of nothing tracked and nothing to do invites tracking."""
        item = self.item(MediaTypes.MOVIE.value)

        html = self.render(
            "large",
            "details",
            item,
            None,
            readonly=True,
            **self.form(item, None),
        )

        self.assertIn("Add to tracker", html)
        self.assertIn('hx-target="#track-box"', html)

    def test_show_in_progress_acts_on_its_season_on_its_page(self):
        """Cards act on the show, and its page on the season whose episode is next."""
        tv_item = self.item(MediaTypes.TV.value, "tv")
        tv = TV(item=tv_item, user=self.user, status=Status.IN_PROGRESS.value)
        TV.save_base(tv)
        season_item = Item.objects.create(
            media_id="tv",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Show",
            season_number=1,
        )
        season = Season(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        Season.save_base(season)
        Event.objects.create(
            item=season_item,
            content_number=3,
            datetime=timezone.now() - timedelta(days=30),
        )
        entry = BasicMedia.objects.filter_media_prefetch(
            self.user,
            "tv",
            MediaTypes.TV.value,
            Sources.MANUAL.value,
        )[0]
        target = {
            "media_id": "tv",
            "source": Sources.MANUAL.value,
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
        }

        rendered = {
            card: self.render("chip", card, tv_item, entry) for card in CHIP_POSITIONS
        }
        rendered["details"] = self.render(
            "large",
            "details",
            target,
            season,
            finish_ref=tv_item,
            finish_entry=entry,
        )

        for html in rendered.values():
            self.assertIn("Mark S1E1 watched", html)
            self.assertIn('"action": "next"', html)
        for card in CHIP_POSITIONS:
            self.assertIn('"media_type": "tv"', rendered[card])
        self.assertIn('"media_type": "season"', rendered["details"])
        self.assertIn(f'"instance_id": "{season.id}"', rendered["details"])
        # The page's finish button finishes the whole show
        self.assertIn('"finish": "1"', rendered["details"])
        self.assertIn('aria-label="Mark show watched"', rendered["details"])


class SimpleAction:
    """The parts of an action the tests compare."""

    def __init__(self, key, label, target_status):
        """Keep the key, label and status the action leads to."""
        self.key = key
        self.label = label
        self.target_status = target_status


class EpisodeActionButtonTests(TestCase):
    """Test that an episode's page and chip draw the same action."""

    def episode(self, **fields):
        """Return an episode's state as a provider's processed episode gives it."""
        return {
            "media_id": "1396",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.EPISODE.value,
            "season_number": 1,
            "episode_number": 2,
            "air_date": "2008-01-20",
            "watched": False,
            "repeat_planned": False,
            "history": [],
            **fields,
        }

    def render(self, size, episode, season, **context):
        """Render the action button of an episode in a season's list or its page."""
        return render_to_string(
            "app/components/action_button.html",
            {
                **context,
                "size": size,
                "card": "details",
                "ref": episode,
                "entry": season,
                "on_card": False,
                "user": get_user_model()(),
            },
        )

    def test_each_state_reads_the_same_in_the_chip_and_on_the_page(self):
        """Watching, rewatching, planning a rewatch and waiting read the same."""
        season = SimpleNamespace(id=7, status=Status.IN_PROGRESS.value)
        cases = (
            ({}, "Mark watched", "watch"),
            ({"watched": True, "history": [1]}, "Plan to rewatch", "plan_repeat"),
            (
                {"watched": True, "repeat_planned": True, "history": [1]},
                "Mark rewatched",
                "watch",
            ),
            ({"air_date": "2099-01-01"}, "E2 · January 1, 2099", None),
        )

        for fields, label, key in cases:
            for size in ("chip", "large"):
                html = self.render(size, self.episode(**fields), season)
                with self.subTest(fields=fields, size=size):
                    self.assertIn(label, html)
                    if key:
                        self.assertIn(f'"action": "{key}", "card": "details"', html)
                        self.assertIn('"media_type": "episode"', html)
                        self.assertIn('"episode_number": "2"', html)
                    else:
                        self.assertNotIn("quick_action", html)

    def test_untracked_season_rests_on_the_plus(self):
        """An episode of a season not tracked shows the plus, not a planned icon."""
        html = self.render("chip", self.episode(), None)

        self.assertIn(" ".join(icon_paths("app/icons/plus.svg")[:1]), html)
        self.assertIn("text-gray-200", html)

    def test_episode_done_in_a_rewatch_says_how_often_and_opens_the_form(self):
        """With nothing left to do, the page says how often, and opens the form."""
        season = SimpleNamespace(id=7, status=Status.REPEATING.value)
        latest = SimpleNamespace(id=42)
        episode = self.episode(watched=True, history=[1, 2])

        html = self.render(
            "large",
            episode,
            season,
            form_url="/track/",
            form_target="#track-box",
            form_entry=latest,
        )

        self.assertRegex(html, r"</svg>\s*Watched 2 times\s*</button>")
        self.assertIn('hx-get="/track/"', html)
        self.assertIn('"instance_id": "42"', html)
        self.assertNotIn("quick_action", html)
