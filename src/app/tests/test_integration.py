import os
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

from app.models import BoardGame, Item, MediaTypes, Movie, Sources, Status


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
        self.page.get_by_role("button", name="Track episode").click()
        expect(self.page.get_by_role("main")).to_contain_text("Track Episode")
        self.page.get_by_role("button", name="More date options").first.click()
        self.page.get_by_role("button", name="Fill in air date").click()
        self.page.get_by_role("button", name="Add watch").click()
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
        self.page.get_by_placeholder("Search tv shows...").click()
        self.page.get_by_placeholder("Search tv shows...").fill("breaking bad")
        self.page.get_by_placeholder("Search tv shows...").press("Enter")
        expect(self.page.locator("h2")).to_contain_text("Search Results")
        self.page.get_by_title("Breaking Bad", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")

        notes = self.page.locator("#track-tv-1396 textarea")
        for _ in range(2):
            self.page.get_by_role("button", name="Add to tracker").click()
            fold = self.page.get_by_role("button", name="Advanced")
            expect(fold).to_be_visible()
            # htmx settles a swapped-in dialog's attributes a moment after inserting it
            self.page.wait_for_timeout(300)
            expect(notes).to_be_hidden()
            fold.click()
            expect(notes).to_be_visible()
            fold.click()
            expect(notes).to_be_hidden()
            self.page.locator("#track-tv-1396").get_by_label("Close").click()

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
        track_button = self.page.locator("""[hx-vals*='"card": "details"']""")
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
