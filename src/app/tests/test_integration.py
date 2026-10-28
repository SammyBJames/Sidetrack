import os
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

from app.models import (
    BoardGame,
    Book,
    Episode,
    Game,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)


class IntegrationTest(StaticLiveServerTestCase):
    """Integration tests for the application."""

    @classmethod
    def setUpClass(cls):
        """Set up the test class."""
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        super().setUpClass()
        cls.playwright = sync_playwright().start()
        # use headless=False, slow_mo=200 to see the browser
        cls.browser = cls.playwright.chromium.launch()
        cls.page = cls.browser.new_page()

    def setUp(self):
        """Set up test data for CustomList model."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.page.goto(f"{self.live_server_url}/")
        self.page.get_by_placeholder("Enter your username").fill(
            self.credentials["username"],
        )
        self.page.get_by_placeholder("Enter your password").fill(
            self.credentials["password"],
        )
        self.page.get_by_role("button", name="Sign in").click()

    @classmethod
    def tearDownClass(cls):
        """Tear down the test class."""
        super().tearDownClass()
        cls.browser.close()
        cls.playwright.stop()

    def test_filter_rows_word_their_values(self):
        """Each filter row reads the value it's set to, and "Any" once cleared."""
        for title, imdb, runtime, genres in (
            ("Heat", Decimal("8.3"), 170, "|Drama|Crime|"),
            ("Up", Decimal("6.1"), 90, "|Drama|"),
        ):
            item = Item.objects.create(
                media_id=title,
                source=Sources.MANUAL.value,
                media_type=MediaTypes.MOVIE.value,
                title=title,
                image="https://example.com/image.jpg",
                imdb_score=imdb,
                runtime=runtime,
                genres=genres,
            )
            Movie.objects.create(item=item, user=self.user, status=Status.COMPLETED)
        url = reverse("medialist", args=["test", MediaTypes.MOVIE.value])
        self.page.goto(
            f"{self.live_server_url}{url}?imdb_min=7&runtime_min=90"
            "&added_within=30d&genre=Drama&genre=Crime",
        )
        self.page.get_by_role("button", name="Filters, 4 active").click()

        rows = {
            key: self.page.locator(f'button[aria-controls="filter-{key}"]')
            for key in ("imdb", "runtime", "added", "genre")
        }
        for key, text in (
            ("imdb", "7+"),
            ("runtime", "1h 30m+"),
            ("added", "Past month"),
            ("genre", "Drama, Crime"),
        ):
            expect(rows[key]).to_contain_text(text)
            expect(rows[key]).to_have_attribute("aria-expanded", "true")
        # The slider's scale ends are formatted under its track
        expect(self.page.locator("#filter-runtime")).to_contain_text("1h 30m")
        expect(self.page.locator("#filter-runtime")).to_contain_text("2h 50m")

        panel = self.page.get_by_role("dialog", name="Filters")
        panel.get_by_role("button", name="Clear all").click()
        for row in rows.values():
            expect(row).to_contain_text("Any")

        # A bound or unrated, an upper bound and a custom end date
        self.page.goto(
            f"{self.live_server_url}{url}?score_min=7&score=unrated&runtime_max=165"
            "&added_max=2026-04-02",
        )
        self.page.get_by_role("button", name="Filters, 3 active").click()
        for key, text in (
            ("score", "7+ or unrated"),
            ("runtime", "up to 2h 45m"),
            ("added", "Until Apr 2, 2026"),
        ):
            expect(
                self.page.locator(f'button[aria-controls="filter-{key}"]'),
            ).to_contain_text(text)

        item = Item.objects.create(
            media_id="Duel",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOARDGAME.value,
            title="Duel",
            image="https://example.com/image.jpg",
            min_players=2,
            max_players=4,
        )
        BoardGame.objects.create(item=item, user=self.user, status=Status.COMPLETED)
        url = reverse("medialist", args=["test", MediaTypes.BOARDGAME.value])
        self.page.goto(f"{self.live_server_url}{url}?players_min=4&players_max=4")
        self.page.get_by_role("button", name="Filters, 1 active").click()
        expect(
            self.page.locator('button[aria-controls="filter-players"]'),
        ).to_contain_text("Fits 4 players")

    def add_logged_entry(self, model, media_type, title, **item_fields):
        """Track an in progress manual book or game, which logs its progress."""
        item = Item.objects.create(
            media_id=title,
            source=Sources.MANUAL.value,
            media_type=media_type,
            title=title,
            image="https://example.com/image.jpg",
            **item_fields,
        )
        return model.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

    def test_logging_pages_from_a_books_page(self):
        """A book's primary button logs the page, and the last page finishes it."""
        book = self.add_logged_entry(Book, MediaTypes.BOOK.value, "Dune", length=300)
        self.page.goto(
            f"{self.live_server_url}"
            + reverse(
                "media_details",
                args=[Sources.MANUAL.value, MediaTypes.BOOK.value, "Dune", "dune"],
            ),
        )

        self.page.get_by_role("button", name="Log pages").click()
        dialog = self.page.locator("#log-dialog")
        expect(dialog).to_contain_text("of 300")
        expect(dialog.locator('input[name="page"]')).to_have_value("0")
        dialog.get_by_label("Next page").click()
        expect(dialog.locator('input[name="page"]')).to_have_value("1")
        dialog.locator('input[name="page"]').fill("120")
        dialog.get_by_role("button", name="Save").click()
        expect(self.page.get_by_text("120", exact=True)).to_be_visible()
        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (120, Status.IN_PROGRESS))

        # A page past the book's total, as in a longer edition, finishes it at the total
        self.page.get_by_role("button", name="Log pages").click()
        dialog.locator('input[name="page"]').fill("310")
        dialog.get_by_role("button", name="Save").click()
        expect(self.page.get_by_role("button", name="Log pages")).to_have_count(0)
        book.refresh_from_db()
        self.assertEqual((book.progress, book.status), (300, Status.COMPLETED))

    def test_logging_time_from_a_game_card(self):
        """A game card's chip opens the dialog, and the session is added in place."""
        game = self.add_logged_entry(Game, MediaTypes.GAME.value, "Hades")
        Game.objects.filter(pk=game.pk).update(progress=90)
        self.page.goto(
            f"{self.live_server_url}"
            + reverse("medialist", args=["test", MediaTypes.GAME.value]),
        )

        self.page.get_by_label("Log time").dispatch_event("click")
        dialog = self.page.locator("#log-dialog")
        expect(dialog).to_contain_text("Total so far: 1h 30min")
        dialog.get_by_label("Half an hour more").click()
        dialog.get_by_role("button", name="Save").click()
        expect(dialog).to_be_hidden()
        game.refresh_from_db()
        self.assertEqual(game.progress, 90 + 90)
        expect(self.page.locator(".media-card", has_text="Hades")).to_contain_text(
            "3h 00min",
        )

        # The card was re-rendered, and its chip opens the dialog again, but only
        # once the dialog has arrived, never showing the one closed before
        held = []

        def hold(route):
            """Keep the request pending until the test releases it."""
            held.append(route)

        self.page.route("**/log_modal/**", hold)
        self.addCleanup(self.page.unroute, "**/log_modal/**")
        self.page.get_by_label("Log time").dispatch_event("click")
        self.page.wait_for_timeout(500)
        self.assertFalse(self.page.locator("#log-dialog .dialog-sheet").is_visible())
        held[0].continue_()
        self.page.unroute("**/log_modal/**")
        expect(dialog).to_contain_text("Total so far: 3h 00min")

        # A double click on Save logs the session once
        dialog.get_by_role("button", name="Save").dblclick()
        expect(dialog).to_be_hidden()
        self.page.wait_for_timeout(500)
        game.refresh_from_db()
        self.assertEqual(game.progress, 180 + 60)

        self.page.get_by_label("Log time").dispatch_event("click")

        # Bad input is explained in the dialog, which stays open
        dialog.locator('input[name="played"]').fill("abc")
        dialog.get_by_role("button", name="Save").click()
        expect(dialog).to_contain_text("Invalid time format")

    def test_only_the_last_opened_log_dialog_shows(self):
        """Dialogs fetched one after another show and log the last clicked entry."""
        first = self.add_logged_entry(Game, MediaTypes.GAME.value, "Hades")
        last = self.add_logged_entry(Game, MediaTypes.GAME.value, "Celeste")
        self.page.goto(
            f"{self.live_server_url}"
            + reverse("medialist", args=["test", MediaTypes.GAME.value]),
        )
        held = []

        def hold(route):
            """Keep the request pending until the test releases it."""
            held.append(route)

        self.page.route("**/log_modal/**", hold)
        self.addCleanup(self.page.unroute, "**/log_modal/**")
        cards = self.page.locator(".media-card")
        cards.filter(has_text="Hades").get_by_label("Log time").dispatch_event("click")
        cards.filter(has_text="Celeste").get_by_label("Log time").dispatch_event(
            "click",
        )
        self.page.wait_for_timeout(300)
        # Release the last request first, then the earlier one
        for route in reversed(held):
            route.continue_()
            self.page.wait_for_timeout(300)

        dialog = self.page.locator("#log-dialog")
        expect(dialog).to_contain_text("Celeste")
        expect(dialog).not_to_contain_text("Hades")
        dialog.get_by_role("button", name="Save").click()
        expect(dialog).to_be_hidden()
        first.refresh_from_db()
        last.refresh_from_db()
        self.assertEqual((first.progress, last.progress), (0, 60))
        expect(cards.filter(has_text="Hades")).to_have_count(1)
        expect(cards.filter(has_text="Celeste")).to_contain_text("1h 00min")

    def add_tracked_movie(self):
        """Track a manual movie and return the URL of its page."""
        item = Item.objects.create(
            media_id="Heat",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Heat",
            image="https://example.com/image.jpg",
        )
        Movie.objects.create(item=item, user=self.user, status=Status.COMPLETED)
        return f"{self.live_server_url}" + reverse(
            "media_details",
            args=[Sources.MANUAL.value, MediaTypes.MOVIE.value, "Heat", "heat"],
        )

    def test_reopened_tracking_dialog_never_shows_the_old_one(self):
        """While a reopened dialog loads, the one closed before stays hidden."""
        self.page.goto(self.add_tracked_movie())
        sheet = self.page.locator(".dialog-sheet")
        notes = sheet.locator("textarea")
        self.page.get_by_role("button", name="Edit tracking").click()
        sheet.get_by_role("button", name="Advanced").click()
        expect(notes).to_be_visible()
        sheet.get_by_label("Close").click()

        held = []

        def hold(route):
            """Keep the request pending until the test releases it."""
            held.append(route)

        self.page.route("**/track_modal/**", hold)
        # The page is shared by the tests, so the held route must not outlive this one
        self.addCleanup(self.page.unroute, "**/track_modal/**")
        self.page.get_by_role("button", name="Edit tracking").click()
        self.page.wait_for_timeout(500)
        expect(sheet).to_be_hidden()
        held[0].continue_()
        expect(sheet).to_be_visible()
        expect(notes).to_be_hidden()

    def test_dialogs_close_only_on_a_click_that_began_outside(self):
        """Dragging a selection out of a dialog keeps it open; the backdrop closes."""
        self.page.goto(self.add_tracked_movie())
        sheet = self.page.locator(".dialog-sheet")
        self.page.get_by_role("button", name="Edit tracking").click()
        sheet.get_by_role("button", name="Advanced").click()
        notes = sheet.locator("textarea")
        notes.fill("some words to select")
        box = notes.bounding_box()
        self.page.mouse.move(box["x"] + 20, box["y"] + 10)
        self.page.mouse.down()
        self.page.mouse.move(2, 2)
        self.page.mouse.up()
        # Closing is animated away from the click, so give it time before looking
        self.page.wait_for_timeout(400)
        self.assertTrue(sheet.is_visible())

        self.page.mouse.move(2, 2)
        self.page.mouse.down()
        self.page.mouse.move(box["x"] + 20, box["y"] + 10)
        self.page.mouse.up()
        self.page.wait_for_timeout(400)
        self.assertTrue(sheet.is_visible())

        self.page.mouse.click(2, 2)
        expect(sheet).to_be_hidden()

    def test_escape_closes_the_open_dialog(self):
        """Escape closes any dialog on the shell, the list form among them."""
        self.page.goto(f"{self.live_server_url}" + reverse("lists"))
        self.page.get_by_role("button", name="New List").click()
        sheet = self.page.locator(".dialog-sheet", has_text="Create New List")
        expect(sheet).to_be_visible()
        self.page.keyboard.press("Escape")
        expect(sheet).to_be_hidden()

        self.page.goto(self.add_tracked_movie())
        self.page.get_by_role("button", name="Edit tracking").click()
        sheet = self.page.locator(".dialog-sheet")
        expect(sheet).to_be_visible()
        self.page.keyboard.press("Escape")
        expect(sheet).to_be_hidden()

    def test_season_progress_edit(self):
        """Test the progress edit of a season."""
        self.page.get_by_placeholder("Search tv shows...").fill("breaking bad")
        self.page.get_by_placeholder("Search tv shows...").press("Enter")
        expect(self.page.locator("h2")).to_contain_text("Search Results")
        self.page.get_by_title("Breaking Bad", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")
        self.page.get_by_title("Season 1").click()
        expect(self.page.get_by_role("main")).to_contain_text("Season 1")
        # The episode's own page has the form, and leads back to the season
        self.page.get_by_role("link", name="Pilot", exact=True).click()
        self.page.get_by_role("button", name="Add to tracker", exact=True).click()
        sheet = self.page.locator(".dialog-sheet")
        expect(sheet).to_contain_text("Breaking Bad S1E1")
        sheet.get_by_role("button", name="Advanced").click()
        sheet.locator('[name="end_date"]').fill("2008-01-20T12:00")
        sheet.get_by_role("button", name="Add", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Your Watches\n1")

        # Episode 1 air date is 2008-01-20
        fixed_date = date(2008, 1, 20)
        expect(self.page.get_by_role("main")).to_contain_text(
            fixed_date.strftime("%Y-%m-%d"),
        )
        self.page.get_by_label("Breadcrumb").get_by_role(
            "link", name="Season 1"
        ).click()
        # The watched episode offers to plan watching it again
        expect(
            self.page.locator("article").get_by_label("Plan to rewatch", exact=True),
        ).to_have_count(1)

        # The home card watches the next episode, which it names
        self.page.get_by_role("link", name="Home").click()
        expect(self.page.get_by_title("Breaking Bad \u2013 Season 1")).to_be_visible()
        self.page.locator("#media-grid-in-progress-season").get_by_role(
            "button",
            name="Mark S1E2 watched",
        ).first.click()
        self.page.get_by_title("Breaking Bad \u2013 Season 1").click()
        expect(
            self.page.locator("article").get_by_label("Plan to rewatch", exact=True),
        ).to_have_count(2)

    def test_episode_entry_dialogs(self):
        """An episode's first entry, another one, and the latest edited."""
        common = {
            "media_id": "m1",
            "source": Sources.MANUAL.value,
            "image": "https://example.com/image.jpg",
            "title": "Home Movies",
        }
        Item.objects.create(**common, media_type=MediaTypes.TV.value)
        Item.objects.create(
            **common, media_type=MediaTypes.SEASON.value, season_number=1
        )
        for number in (1, 2):
            Item.objects.create(
                **common,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=number,
            )
        self.page.goto(
            f"{self.live_server_url}"
            + reverse(
                "episode_details",
                args=[Sources.MANUAL.value, "m1", "home-movies", 1, 1],
            ),
        )
        main = self.page.get_by_role("main")
        sheet = self.page.locator(".dialog-sheet")

        # The first entry is a watch unless another status is chosen
        self.page.get_by_role("button", name="Add to tracker", exact=True).click()
        expect(sheet.get_by_role("radio", name="Completed")).to_be_checked()
        sheet.get_by_role("button", name="Add", exact=True).click()
        expect(main).to_contain_text("Your Watches\n1")

        self.page.get_by_role("button", name="Add entry").click()
        sheet.get_by_text("Planning Rewatch", exact=True).click()
        sheet.get_by_role("button", name="Add", exact=True).click()
        expect(main).to_contain_text("Your Watches\n2")

        # Edit tracking opens the entry added last
        self.page.get_by_role("button", name="Edit tracking").click()
        expect(sheet.get_by_role("radio", name="Planning Rewatch")).to_be_checked()
        sheet.get_by_text("Completed", exact=True).click()
        sheet.get_by_role("button", name="Advanced").click()
        sheet.locator("textarea").fill("Better the second time")
        sheet.get_by_role("button", name="Save").click()
        expect(main).to_contain_text("Better the second time")
        self.assertEqual(
            sorted(Episode.objects.values_list("status", "notes")),
            [
                (Status.COMPLETED.value, ""),
                (Status.COMPLETED.value, "Better the second time"),
            ],
        )

    def test_tv_completed(self):
        """Test the completed status of a TV show."""
        self.page.get_by_placeholder("Search tv shows...").click()
        self.page.get_by_placeholder("Search tv shows...").fill("breaking bad")
        self.page.get_by_placeholder("Search tv shows...").press("Enter")
        expect(self.page.locator("h2")).to_contain_text("Search Results")
        self.page.get_by_title("Breaking Bad", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")
        self.page.get_by_role("button", name="Add to tracker").click()
        expect(self.page.locator("#track-tv-1396")).to_contain_text("Rating")
        self.page.get_by_text("Completed", exact=True).click()
        self.page.get_by_role("button", name="Add", exact=True).click()
        self.page.get_by_role("link", name="TV Shows").click()
        self.page.get_by_role("button", name="List", exact=True).click()
        expect(self.page.locator(".media-row")).to_contain_text("62")

    def test_tracking_dialog_fold_starts_closed_each_time(self):
        """The Advanced fold of the tracking dialog is closed on every open."""
        self.page.goto(self.add_tracked_movie())
        sheet = self.page.locator(".dialog-sheet")
        notes = sheet.locator("textarea")
        for _ in range(2):
            self.page.get_by_role("button", name="Edit tracking").click()
            fold = sheet.get_by_role("button", name="Advanced")
            expect(fold).to_be_visible()
            # htmx settles a swapped-in dialog's attributes a moment after inserting it
            self.page.wait_for_timeout(300)
            expect(notes).to_be_hidden()
            fold.click()
            expect(notes).to_be_visible()
            fold.click()
            expect(notes).to_be_hidden()
            sheet.get_by_label("Close").click()

    def test_season_completed(self):
        """Test the completed status of a season."""
        self.page.get_by_placeholder("Search tv shows...").fill("breaking bad")
        self.page.get_by_placeholder("Search tv shows...").press("Enter")
        expect(self.page.locator("h2")).to_contain_text("Search Results")
        self.page.get_by_title("Breaking Bad", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")
        self.page.get_by_title("Season 1").click()
        expect(self.page.get_by_role("main")).to_contain_text("Season 1")
        self.page.get_by_role("button", name="Add to tracker").click()
        expect(self.page.locator("#track-season-1396-1")).to_contain_text("Rating")
        self.page.get_by_role("button", name="Add", exact=True).click()
        # Seasons have no list of their own, so their page shows the entry
        expect(self.page.get_by_role("main")).to_contain_text("Your History")
        # A finished season shows it, and offers to plan a rewatch on hover
        track_button = self.page.locator("""[hx-vals*='"card": "details"']""").first
        expect(track_button).to_contain_text("Watched")
        expect(track_button).to_contain_text("Plan to rewatch")
        expect(self.page.get_by_role("main")).to_contain_text("Progress\n7")

    def test_tv_manual(self):
        """Test the manual creation of a TV show."""
        # Create TV show
        self.page.get_by_role("link", name="Create Custom").click()
        self.page.get_by_placeholder("Enter title").click()
        self.page.get_by_placeholder("Enter title").fill("Friends")
        self.page.get_by_placeholder("Enter image URL").click()
        self.page.get_by_placeholder("Enter image URL").fill(
            "https://media.themoviedb.org/t/p/w300_and_h450_bestv2/2koX1xLkpTQM4IZebYvKysFW1Nh.jpg",
        )
        self.page.get_by_role("combobox").select_option("In progress")
        self.page.get_by_role("button", name="Create Entry").click()
        expect(self.page.locator("body")).to_contain_text(
            "Friends added successfully.",
        )

        # Create season
        self.page.get_by_role("button", name="Season").click()
        expect(self.page.get_by_role("main")).to_contain_text("Parent TV Show")
        self.page.get_by_placeholder("Search for a TV show...").click()
        self.page.get_by_placeholder("Search for a TV show...").type("fri")
        expect(self.page.locator("#parent-tv-results")).to_contain_text("Friends")
        self.page.get_by_role("button", name="Friends").click()
        self.page.get_by_placeholder("Enter image URL").click()
        self.page.get_by_placeholder("Enter image URL").fill(
            "https://media.themoviedb.org/t/p/w130_and_h195_bestv2/odCW88Cq5hAF0ZFVOkeJmeQv1nV.jpg",
        )
        self.page.get_by_role("button", name="Create Entry").click()
        expect(self.page.locator("body")).to_contain_text(
            "Friends S1 added successfully.",
        )

        # Create episode
        self.page.get_by_role("button", name="Episode").click()
        expect(self.page.get_by_role("main")).to_contain_text("Parent Season")
        self.page.get_by_placeholder("Search for a season...").click()
        self.page.get_by_placeholder("Search for a season...").type("frien")
        expect(self.page.locator("#parent-season-results")).to_contain_text(
            "Friends - Season 1",
        )
        self.page.get_by_role("button", name="Friends - Season").click()
        self.page.get_by_placeholder("Enter image URL").click()
        self.page.get_by_placeholder("Enter image URL").fill(
            "https://media.themoviedb.org/t/p/w227_and_h127_bestv2/v6Elr1W2elOyGi1MClgV0mIBVHC.jpg",
        )
        self.page.locator('input[name="end_date"]').fill("2025-03-07")
        self.page.get_by_role("button", name="Create Entry").click()
        expect(self.page.locator("body")).to_contain_text(
            "Friends S1E1 added successfully.",
        )

        # Check visibility
        self.page.get_by_role("link", name="TV Shows").click()
        self.page.get_by_role("button", name="Grid", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Friends")
        self.page.get_by_role("link", name="TV Shows").click()
        self.page.get_by_title("Friends").click()
        expect(self.page.get_by_role("main")).to_contain_text("Friends")
        expect(self.page.get_by_role("main")).to_contain_text("Season 1")
        self.page.get_by_title("Season 1").click()
        expect(self.page.get_by_role("main")).to_contain_text("Season 1")
        expect(self.page.get_by_role("main")).to_contain_text("Episode 1")

    def test_obfuscate_unseen_episodes_enabled(self):
        """Test that obfuscate_unseen_episodes setting is accessible and functional."""
        # Navigate to preferences
        self.page.get_by_role("link", name="Settings").click()
        self.page.get_by_role("link", name="Preferences").click()

        # Verify the obfuscate setting is visible
        expect(self.page.get_by_role("main")).to_contain_text(
            "Obfuscate Unseen Episodes"
        )
        expect(self.page.get_by_role("main")).to_contain_text(
            "unseen episode images and descriptions will be blurred"
        )

        # Find and check the obfuscate checkbox by clicking the label
        # The checkbox is sr-only (hidden), so we need to click the label
        obfuscate_label = self.page.locator(
            'label:has(input[name="obfuscate_unseen_episodes"])'
        )
        obfuscate_label.click()

        # Save preferences
        self.page.get_by_role("button", name="Save Preferences").click()

        # Verify success message
        expect(self.page.locator("body")).to_contain_text("Settings updated")

        # Verify setting persisted
        self.page.get_by_role("link", name="Preferences").click()
        obfuscate_checkbox = self.page.locator(
            'input[name="obfuscate_unseen_episodes"]'
        )
        expect(obfuscate_checkbox).to_be_checked()

    def test_obfuscate_unseen_episodes_disabled(self):
        """Test toggling obfuscate_unseen_episodes setting off."""
        # Navigate to preferences
        self.page.get_by_role("link", name="Settings").click()
        self.page.get_by_role("link", name="Preferences").click()

        # Find the obfuscate checkbox
        obfuscate_checkbox = self.page.locator(
            'input[name="obfuscate_unseen_episodes"]'
        )
        if obfuscate_checkbox.is_checked():
            # Click the label to uncheck (checkbox is sr-only, so click label)
            obfuscate_label = self.page.locator(
                'label:has(input[name="obfuscate_unseen_episodes"])'
            )
            obfuscate_label.click()

            # Save preferences
            self.page.get_by_role("button", name="Save Preferences").click()

            # Verify success message
            expect(self.page.locator("body")).to_contain_text("Settings updated")

            # Verify setting persisted as unchecked
            self.page.get_by_role("link", name="Preferences").click()
            obfuscate_checkbox = self.page.locator(
                'input[name="obfuscate_unseen_episodes"]'
            )
            expect(obfuscate_checkbox).not_to_be_checked()
