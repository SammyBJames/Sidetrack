import threading
import time
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Book,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)
from app.providers import services


def _page(page, total_pages, count=2, media_type=MediaTypes.MOVIE.value):
    """Return a search response for one page of `count` results."""
    return {
        "page": page,
        "total_results": total_pages * count,
        "total_pages": total_pages,
        "results": [
            {
                "media_id": str(page * 100 + n),
                "title": f"Result {page}-{n}",
                "media_type": media_type,
                "source": Sources.TMDB.value,
                "image": "http://example.com/image.jpg",
            }
            for n in range(count)
        ],
    }


class MediaSearchViewTests(TestCase):
    """Test the media search view: header, scrolling pages and empty state."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.search")
    def test_media_search_view(self, mock_search):
        """Test the media search view."""
        mock_search.return_value = {
            "page": 1,
            "total_results": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": "238",
                    "title": "Test Movie",
                    "media_type": MediaTypes.MOVIE.value,
                    "source": Sources.TMDB.value,
                    "image": "http://example.com/image.jpg",
                },
            ],
        }

        response = self.client.get(
            reverse("search") + "?media_type=movie&q=test",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/search.html")

        mock_search.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "test",
            1,
            Sources.TMDB.value,
        )

    @patch("app.providers.services.search")
    def test_list_layout_shows_rows_for_tracked_and_untracked_results(
        self,
        mock_search,
    ):
        """The list layout is the library's rows, with an add action when untracked."""
        tracked = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Tracked Movie",
            image="http://example.com/1.jpg",
        )
        Movie.objects.create(item=tracked, user=self.user, status=Status.PLANNING.value)
        mock_search.return_value = {
            "page": 1,
            "total_results": 2,
            "total_pages": 1,
            "results": [
                {
                    "media_id": media_id,
                    "title": title,
                    "media_type": MediaTypes.MOVIE.value,
                    "source": Sources.TMDB.value,
                    "image": "http://example.com/image.jpg",
                    "release_year": year,
                }
                for media_id, title, year in (
                    ("1", "Tracked Movie", 2001),
                    ("2", "New Movie", 2002),
                )
            ],
        }

        response = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "movie", "layout": "list"},
        )

        self.assertTemplateUsed(response, "app/components/media_row.html")
        self.assertTemplateNotUsed(response, "app/components/media_card_list.html")
        html = response.content.decode()
        tracked_row, new_row = html.split('class="media-row')[1:]
        self.assertIn(">Tracked Movie</a>", tracked_row)
        self.assertIn("Planning", tracked_row)
        self.assertIn('"action": "complete"', tracked_row)
        self.assertIn(">New Movie</a>", new_row)
        self.assertIn("<span>2001</span>", tracked_row)
        self.assertIn("<span>2002</span>", new_row)
        self.assertIn('"action": "plan"', new_row)
        # Dashes for its empty status and rating
        self.assertEqual(new_row.count('text-gray-600">—'), 2)
        # Nothing of the library's filter
        self.assertNotIn("filter-form", html)

    @patch("app.providers.services.search")
    def test_list_layout_shows_a_started_items_progress(self, mock_search):
        """A started book's row shows its progress, as search has no progress column."""
        item = Item.objects.create(
            media_id="1",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Started Book",
            image="http://example.com/1.jpg",
        )
        # Without save, which fetches the book's metadata
        Book.objects.bulk_create(
            [
                Book(
                    item=item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=123,
                ),
            ],
        )
        mock_search.return_value = {
            "page": 1,
            "total_results": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": "1",
                    "title": "Started Book",
                    "media_type": MediaTypes.BOOK.value,
                    "source": Sources.HARDCOVER.value,
                    "image": "http://example.com/1.jpg",
                    "release_year": 2001,
                },
            ],
        }

        response = self.client.get(
            reverse("search"),
            {"media_type": "book", "q": "book", "layout": "list"},
        )

        row = response.content.decode().split('class="media-row')[1]
        self.assertIn("123", row)
        self.assertIn("&nbsp;Pages", row)

    @patch("app.providers.services.search")
    def test_header_shows_query_and_count(self, mock_search):
        """The title names the query and the count of results sits beside it."""
        mock_search.return_value = _page(1, 1, count=2)

        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune"},
        ).content.decode()

        self.assertIn("Results for “dune”", html)
        self.assertIn('text-gray-400">2</span>', html)
        self.assertNotIn("hx-get=", html.split("<main")[-1].split("</main>")[0])

    @patch("app.providers.services.search")
    def test_source_switch_only_for_types_with_several_sources(self, mock_search):
        """Manga links to its other source; movies have no switch."""
        mock_search.return_value = _page(1, 1, media_type=MediaTypes.MANGA.value)
        manga = self.client.get(
            reverse("search"),
            {"media_type": "manga", "q": "berserk", "layout": "list"},
        ).content.decode()
        self.assertIn('aria-label="Source"', manga)
        self.assertIn("source=mangaupdates", manga)
        self.assertIn("layout=list", manga)
        self.assertIn("MangaUpdates", manga)

        mock_search.return_value = _page(1, 1)
        movie = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune"},
        ).content.decode()
        self.assertNotIn('aria-label="Source"', movie)

    @patch("app.providers.services.search")
    def test_more_pages_end_in_a_sentinel_in_both_layouts(self, mock_search):
        """The last item is followed by a sentinel for the next page."""
        mock_search.side_effect = lambda *_: _page(1, 3)
        for layout in ("grid", "list"):
            html = self.client.get(
                reverse("search"),
                {
                    "media_type": "manga",
                    "q": "dune",
                    "source": "mangaupdates",
                    "layout": layout,
                },
            ).content.decode()
            self.assertEqual(html.count('hx-trigger="intersect once"'), 1)
            self.assertLess(html.index("Result 1-1"), html.index("intersect once"))
            self.assertIn(
                "/search?q=dune&amp;media_type=manga&amp;source=mangaupdates"
                f"&amp;layout={layout}&amp;page=2",
                html,
            )

    @patch("app.providers.services.search")
    def test_htmx_page_returns_only_items_and_next_sentinel(self, mock_search):
        """A further page is its items and the next sentinel; the last has none."""
        mock_search.return_value = _page(2, 3)
        response = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune", "page": 2},
            headers={"HX-Request": "true"},
        )
        html = response.content.decode()
        self.assertTemplateNotUsed(response, "app/search.html")
        self.assertNotIn("<html", html)
        self.assertIn("Result 2-0", html)
        self.assertIn("page=3", html)
        mock_search.assert_called_once_with("movie", "dune", 2, Sources.TMDB.value)

        mock_search.return_value = _page(3, 3)
        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune", "page": 3},
            headers={"HX-Request": "true"},
        ).content.decode()
        self.assertIn("Result 3-0", html)
        self.assertNotIn("hx-get", html)

    @patch("app.providers.services.search")
    def test_no_results_shows_the_empty_state(self, mock_search):
        """An empty search shows the dashed box naming the query and a link to All."""
        mock_search.return_value = _page(1, 0, count=0)

        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "zzzz"},
        ).content.decode()

        self.assertIn("border-dashed", html)
        self.assertIn("No matches for “zzzz”", html)
        self.assertNotIn("media-grid", html)
        # No count beside the title, and a way to widen the search to All
        self.assertNotIn('text-gray-400">0</span>', html)
        empty_box = html.split("border-dashed")[1]
        self.assertIn("Search all media", empty_box)
        self.assertIn('href="?q=zzzz"', empty_box)

    @patch("app.providers.services.search")
    def test_query_is_urlencoded_in_links_and_escaped_in_text(self, mock_search):
        """`&` and quotes in the query don't break links or inject markup."""
        mock_search.return_value = _page(1, 2, media_type=MediaTypes.MANGA.value)
        query = 'a&b "c" <i>'

        html = self.client.get(
            reverse("search"),
            {"media_type": "manga", "q": query, "layout": "list"},
        ).content.decode()

        encoded = "a%26b+%22c%22+%3Ci%3E"
        self.assertIn("Results for “a&amp;b &quot;c&quot; &lt;i&gt;”", html)
        self.assertNotIn("<i>”", html)
        # The source and layout links, the sentinel and each pill all carry it
        self.assertGreaterEqual(html.count(f"q={encoded}"), 5)
        self.assertEqual(html.count("q=a"), html.count(f"q={encoded}"))
        self.assertNotIn("q=a&b", html)
        self.assertNotIn("q=a&amp;b", html)

    @patch("app.providers.services.search")
    def test_empty_page_past_the_end_has_no_sentinel(self, mock_search):
        """A page with no results ends the scroll even if total_pages says more."""
        mock_search.return_value = {**_page(5, 9, count=0), "total_pages": 9}

        html = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune", "page": 5},
            headers={"HX-Request": "true"},
        ).content.decode()

        self.assertNotIn("hx-get", html)

    @patch("app.providers.services.search")
    def test_soft_navigation_request_gets_the_full_page(self, mock_search):
        """An HX-Request that is a soft navigation swaps the body, so it is full."""
        mock_search.return_value = _page(1, 2)

        response = self.client.get(
            reverse("search"),
            {"media_type": "movie", "q": "dune"},
            headers={"HX-Request": "true", "X-Soft-Navigation": "true"},
        )

        self.assertTemplateUsed(response, "app/search.html")


def _rows(html):
    """Return the All page's rows' markup, in order, as `(type, section)`."""
    return [
        (section.split('title="Search only ')[1].split('"')[0], section)
        for section in html.split("<section")[1:]
    ]


def _by_type(responses):
    """Return a `services.search` fake answering with `responses[media_type]`.

    A value that is an exception is raised.
    """

    def fake(media_type, query, page, source=None):  # noqa: ARG001
        """Return or raise what `responses` holds for the type."""
        response = responses.get(media_type, _page(1, 0, count=0))
        if isinstance(response, Exception):
            raise response
        return response

    return fake


class MediaSearchAllTests(TestCase):
    """Test the All search: a row for each enabled type."""

    def setUp(self):
        """Create a user with a few types enabled and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        for media_type in MediaTypes.values:
            if hasattr(self.user, f"{media_type}_enabled"):
                setattr(self.user, f"{media_type}_enabled", False)
        self.user.tv_enabled = True
        self.user.movie_enabled = True
        self.user.anime_enabled = True
        self.user.save()
        self.client.login(**self.credentials)

    def search(self, **params):
        """Return the page for a search of `dune` with `params`."""
        return self.client.get(reverse("search"), {"q": "dune", **params})

    @patch("app.providers.services.search")
    def test_missing_all_or_unknown_type_searches_every_enabled_type(self, mock_search):
        """No, `all` or an unknown type is All, over enabled types only."""
        mock_search.side_effect = lambda media_type, *_: _page(
            1, 1, media_type=media_type
        )
        for params in ({}, {"media_type": "all"}, {"media_type": "bogus"}):
            mock_search.reset_mock()
            response = self.search(**params)

            self.assertEqual(response.status_code, 200)
            searched = sorted(call.args[0] for call in mock_search.call_args_list)
            self.assertEqual(searched, ["anime", "movie", "tv"])
            for call in mock_search.call_args_list:
                self.assertEqual(call.args[1:3], ("dune", 1))
            self.assertEqual(
                [name for name, _ in _rows(response.content.decode())],
                ["TV Shows", "Movies", "Anime"],
            )

    @patch("app.providers.services.search")
    def test_row_header_links_to_the_narrowed_search_with_its_count(self, mock_search):
        """A row's header names the type, links to its search and shows the count."""
        mock_search.side_effect = _by_type(
            {"movie": {**_page(1, 3), "total_results": 57}},
        )

        html = self.search(layout="list").content.decode()

        ((name, row),) = _rows(html)
        self.assertEqual(name, "Movies")
        self.assertIn("q=dune&amp;layout=list&amp;media_type=movie", row)
        self.assertNotIn("source=", row)
        self.assertIn('text-gray-500">57</span>', row)
        self.assertIn("Result 1-0", row)
        self.assertIn("scrollRow", row)

    @patch("app.providers.services.search")
    def test_made_up_total_shows_no_count_but_a_short_page_does(self, mock_search):
        """MAL's made-up total is hidden in a row and title; a short page is exact."""
        made_up = {**_page(1, 3, media_type="anime"), "total_estimated": True}
        mock_search.side_effect = _by_type({"anime": made_up})
        html = self.search().content.decode()
        self.assertNotIn('text-gray-500">6</span>', html)
        self.assertIn("Result 1-0", html)

        narrowed = self.search(media_type="anime").content.decode()
        self.assertNotIn('text-gray-400">6</span>', narrowed)
        self.assertIn("Results for “dune”", narrowed)

        exact = {**_page(1, 1, media_type="anime"), "total_estimated": False}
        mock_search.side_effect = _by_type({"anime": exact})
        self.assertIn('text-gray-500">2</span>', self.search().content.decode())
        self.assertIn(
            'text-gray-400">2</span>',
            self.search(media_type="anime").content.decode(),
        )

    @patch("app.providers.services.search")
    def test_empty_rows_are_left_out_and_all_empty_shows_the_empty_state(
        self,
        mock_search,
    ):
        """Rows without results are hidden; with none left the dashed box shows."""
        mock_search.side_effect = _by_type({"tv": _page(1, 1, media_type="tv")})
        html = self.search().content.decode()
        self.assertEqual([name for name, _ in _rows(html)], ["TV Shows"])
        self.assertNotIn("border-dashed", html)

        mock_search.side_effect = _by_type({})
        html = self.search().content.decode()
        self.assertEqual(_rows(html), [])
        self.assertIn("No matches for “dune”", html)

    @patch("app.providers.services.search")
    def test_failing_source_shows_its_note_while_other_rows_render(self, mock_search):
        """Any error is a note in its row, whose header still links."""
        mock_search.side_effect = _by_type(
            {
                "tv": services.ProviderAPIError(Sources.TMDB.value, Exception("x")),
                "movie": _page(1, 1),
                "anime": requests.ConnectionError(),
                "game": KeyError("unexpected"),
            },
        )
        self.user.game_enabled = True
        self.user.save()

        # The unexpected error's thread would print its traceback
        with patch("threading.excepthook"):
            html = self.search().content.decode()

        rows = dict(_rows(html))
        self.assertIn("reach The Movie Database", rows["TV Shows"])
        self.assertIn("media_type=tv", rows["TV Shows"])
        self.assertIn("reach MyAnimeList", rows["Anime"])
        self.assertIn("reach", rows["Games"])
        self.assertIn("Result 1-0", rows["Movies"])

    @patch("app.providers.services.SEARCH_ALL_TIMEOUT", 0.2)
    @patch("app.providers.services.search")
    def test_slow_source_shows_the_slow_note_and_the_page_returns(self, mock_search):
        """A source past the deadline is a note; the page doesn't wait for it."""
        release = threading.Event()
        finished = threading.Event()
        self.addCleanup(release.set)
        fast = _by_type({"movie": _page(1, 1)})

        def fake(media_type, *args):
            """Block the TV search until released and answer the others at once."""
            if media_type == "tv":
                release.wait(10)
                finished.set()
            return fast(media_type, *args)

        mock_search.side_effect = fake

        html = self.search().content.decode()

        rows = dict(_rows(html))
        self.assertIn("The Movie Database is taking too long", rows["TV Shows"])
        self.assertIn("Result 1-0", rows["Movies"])
        # The response came back while the slow search was still blocked
        self.assertFalse(finished.is_set())

    @patch("app.providers.services.SEARCH_ALL_TIMEOUT", 0.3)
    @patch("app.providers.services.search")
    def test_slow_sources_share_one_deadline(self, mock_search):
        """Three slow sources hold the search for one timeout, not three."""
        release = threading.Event()
        self.addCleanup(release.set)

        def fake(*_):
            """Block until released."""
            release.wait(10)

        mock_search.side_effect = fake

        start = time.monotonic()
        with self.assertLogs("app.providers.services", "WARNING"):
            rows = services.search_all(["tv", "movie", "anime"], "dune")

        self.assertLess(time.monotonic() - start, 0.6)
        self.assertEqual([row["error"] for row in rows], ["slow"] * 3)

    @patch("app.providers.services.SEARCH_ALL_TIMEOUT", 0.2)
    @patch("app.providers.services.search")
    def test_slow_source_is_logged_and_runs_in_a_daemon_thread(self, mock_search):
        """A slow search is logged, can't hold up exit, and can't change its row."""
        release = threading.Event()
        self.addCleanup(release.set)
        threads = []

        def fake(*_):
            """Record the searching thread, then block until released."""
            threads.append(threading.current_thread())
            release.wait(10)
            return _page(1, 1)

        mock_search.side_effect = fake

        with self.assertLogs("app.providers.services", "WARNING") as logs:
            rows = services.search_all(["tv"], "dune")

        self.assertEqual(rows[0]["error"], "slow")
        self.assertIn("Search of tv for All took over", logs.output[0])
        self.assertTrue(threads[0].daemon)

        release.set()
        threads[0].join(5)
        self.assertEqual(rows[0]["error"], "slow")
        self.assertEqual(rows[0]["results"], [])

    @patch("app.providers.services.search")
    def test_user_with_every_type_disabled_gets_the_empty_state(self, mock_search):
        """With no enabled type nothing is searched and the empty state shows."""
        for media_type in MediaTypes.values:
            if hasattr(self.user, f"{media_type}_enabled"):
                setattr(self.user, f"{media_type}_enabled", False)
        self.user.save()

        response = self.search()

        self.assertEqual(response.status_code, 200)
        self.assertIn("No matches for “dune”", response.content.decode())
        mock_search.assert_not_called()

    @patch("app.providers.services.search")
    def test_searches_run_in_parallel(self, mock_search):
        """Every type's search waits on a barrier only they all reach."""
        barrier = threading.Barrier(3, timeout=5)

        def fake(media_type, *_):
            """Wait for every type's search to start, then return one result."""
            barrier.wait()
            return _page(1, 1, media_type=media_type)

        mock_search.side_effect = fake

        rows = services.search_all(["tv", "movie", "anime"], "dune")

        self.assertEqual([row["error"] for row in rows], [None] * 3)
        self.assertEqual([row["media_type"] for row in rows], ["tv", "movie", "anime"])

    @patch("app.providers.services.search")
    def test_each_search_caps_its_rate_limit_wait(self, mock_search):
        """The cap is set inside the thread, as context variables don't cross."""
        caps = []

        def fake(*_):
            """Record the rate limit cap seen in the searching thread."""
            caps.append(services._rate_limit_cap.get())
            return _page(1, 0, count=0)

        mock_search.side_effect = fake

        services.search_all(["tv", "movie"], "dune")

        self.assertEqual(
            [cap.seconds for cap in caps], [services.SEARCH_ALL_TIMEOUT] * 2
        )

    @patch("app.providers.services.search")
    def test_blank_query_calls_no_source(self, mock_search):
        """A blank or missing `q` shows a prompt, narrowed or not."""
        for params in ({"q": "  "}, {}, {"q": " ", "media_type": "movie"}):
            response = self.client.get(reverse("search"), params)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Search your media", response.content.decode())

        mock_search.assert_not_called()

    @patch("app.providers.services.search")
    def test_pills_start_with_all_highlight_the_current_and_keep_q_and_layout(
        self,
        mock_search,
    ):
        """The pills link to All and each enabled type, keeping q and layout."""
        mock_search.side_effect = lambda *_: _page(1, 2, media_type="manga")
        self.user.manga_enabled = True
        self.user.save()

        html = self.search(
            media_type="manga",
            layout="list",
            source="mangaupdates",
            page=1,
        ).content.decode()

        pills = html.split('aria-label="Media type"')[1].split("</div>")[0]
        links = pills.split("<a ")[1:]
        self.assertEqual(len(links), 5)
        self.assertIn(">All</a>", links[0])
        self.assertIn('aria-current="false"', links[0])
        self.assertNotIn("media_type", links[0])
        for link in links:
            self.assertIn("q=dune", link)
            self.assertIn("layout=list", link)
            self.assertNotIn("source", link)
            self.assertNotIn("page=", link)
        current = [link for link in links if 'aria-current="true"' in link]
        self.assertEqual(len(current), 1)
        self.assertIn(">Manga</a>", current[0])

        html = self.search().content.decode()
        pills = html.split('aria-label="Media type"')[1].split("</div>")[0]
        self.assertIn('aria-current="true"', pills.split("<a ")[1])

    @patch("app.providers.services.search")
    def test_navbar_starts_on_all_and_on_the_narrowed_type_on_its_page(
        self,
        mock_search,
    ):
        """The dropdown lists All first; it starts on All except on a narrowed page."""
        mock_search.side_effect = lambda *_: _page(1, 1)

        narrowed = self.search(media_type="movie").content.decode()
        self.assertIn("display: 'Movies', value: 'movie'", narrowed)

        all_page = self.search().content.decode()
        self.assertIn("display: 'All', value: 'all'", all_page)

        home = self.client.get(reverse("home")).content.decode()
        self.assertIn("display: 'All', value: 'all'", home)
        self.assertIn(
            "[{&#x27;display&#x27;: &#x27;All&#x27;, &#x27;value&#x27;:"
            " &#x27;all&#x27;}, {&#x27;display&#x27;: &#x27;TV",
            home,
        )
