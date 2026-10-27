import re
from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import config
from app.models import Episode, Item, MediaTypes, Season, Sources, Status
from events.models import Event


def raw_episode(number, **fields):
    """Return an episode as TMDB lists it in a season."""
    return {
        "episode_number": number,
        "name": f"Episode Name {number}",
        "overview": f"Overview {number}",
        "air_date": "2008-01-20",
        "still_path": f"/still{number}.jpg",
        "runtime": 47,
        "vote_average": 8.25,
        "vote_count": 120,
        "guest_stars": [],
        "crew": [],
        **fields,
    }


def tv_metadata():
    """Return a show with its first season, as tv_with_seasons returns it."""
    return {
        "title": "Breaking Bad",
        "media_id": "1396",
        "source": Sources.TMDB.value,
        "media_type": MediaTypes.TV.value,
        "image": "http://example.com/show.jpg",
        "related": {
            # Season 2 hasn't come out yet
            "seasons": [
                {
                    "media_id": "1396",
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.SEASON.value,
                    "title": "Breaking Bad",
                    "season_number": number,
                    "season_title": title,
                    "image": "http://example.com/season.jpg",
                    "max_progress": episodes,
                    "first_air_date": first_air_date,
                }
                for number, title, episodes, first_air_date in (
                    (0, "Specials", 3, "2009-02-17"),
                    (1, "Season 1", 7, "2008-01-20"),
                    (2, "Season 2", 13, "2099-01-01"),
                )
            ],
            "recommendations": [
                {
                    "media_id": "1398",
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "title": "The Sopranos",
                    "image": "http://example.com/sopranos.jpg",
                },
            ],
        },
        "season/1": {
            "title": "Breaking Bad",
            "season_title": "Season 1",
            "season_number": 1,
            "media_id": "1396",
            "media_type": MediaTypes.SEASON.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/tv/1396/season/1",
            "image": "http://example.com/season.jpg",
            "backdrop": "http://example.com/backdrop.jpg",
            "episodes": [
                raw_episode(
                    1,
                    guest_stars=[
                        {
                            "id": 10,
                            "name": "Guest Person",
                            "character": "Tuco",
                            "profile_path": None,
                        },
                    ],
                    crew=[
                        {
                            "id": 11,
                            "name": "Vince Gilligan",
                            "job": "Director",
                            "profile_path": None,
                        },
                    ],
                ),
                raw_episode(2),
                # Not out yet
                raw_episode(3, air_date="2099-01-01"),
            ],
        },
    }


def fake_metadata(media_type, *_):
    """Return the show, or its first season when a season is asked for."""
    if media_type == MediaTypes.SEASON.value:
        return tv_metadata()["season/1"]
    return tv_metadata()


@patch("app.providers.services.get_media_metadata", side_effect=fake_metadata)
class EpisodeDetailsViewTests(TestCase):
    """Test the episode page and the season page's episodes."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def episode_url(self, number):
        """Return the page of an episode of the first season."""
        return reverse(
            "episode_details",
            kwargs={
                "source": Sources.TMDB.value,
                "media_id": "1396",
                "title": "breaking-bad",
                "season_number": 1,
                "episode_number": number,
            },
        )

    def season_url(self):
        """Return the page of the first season."""
        return reverse(
            "season_details",
            kwargs={
                "source": Sources.TMDB.value,
                "media_id": "1396",
                "title": "breaking-bad",
                "season_number": 1,
            },
        )

    def watch(self, number, notes=""):
        """Track the first season and watch one of its episodes."""
        season_item, _ = Item.objects.get_or_create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={"title": "Breaking Bad", "image": "http://example.com/s.jpg"},
        )
        season, _ = Season.objects.get_or_create(
            item=season_item,
            user=self.user,
            defaults={"status": Status.IN_PROGRESS.value},
        )
        episode_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=number,
            title="Breaking Bad",
            image="http://example.com/e.jpg",
        )
        # bulk_create skips the save that looks up the season's metadata
        return Episode.objects.bulk_create(
            [Episode(related_season=season, item=episode_item, notes=notes)],
        )[0]

    def test_episode_page(self, _):
        """The page shows the episode, its people, and the way back and onward."""
        response = self.client.get(self.episode_url(1))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/episode_details.html")
        self.assertEqual(response.context["media"]["episode_title"], "Episode Name 1")
        self.assertContains(response, "Episode Name 1</h1>")
        self.assertContains(response, "Guest Person")
        self.assertContains(response, "Vince Gilligan")
        self.assertContains(response, "Mark watched")
        # TMDB's rating of the episode
        self.assertContains(response, "8.2<")
        # Back to the show and season, on to the next episode, and no previous one
        self.assertContains(
            response,
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "1396",
                    "title": "breaking-bad",
                },
            ),
        )
        self.assertContains(response, f'href="{self.season_url()}"')
        self.assertContains(response, f'href="{self.episode_url(2)}"')
        self.assertContains(response, 'aria-label="Next episode"')
        self.assertNotContains(response, 'aria-label="Previous episode"')

    def test_unknown_episode(self, _):
        """An episode the season doesn't have isn't found."""
        response = self.client.get(self.episode_url(9))

        self.assertEqual(response.status_code, 404)

    def test_watches_can_be_edited(self, _):
        """Each watch of the episode has a form to change its date and notes."""
        watch = self.watch(1, notes="Loved it")

        response = self.client.get(self.episode_url(1))

        self.assertRegex(
            response.content.decode(),
            r"Your Watches</h2>\s*<span[^>]*>1</span>",
        )
        self.assertContains(response, "Loved it")
        self.assertContains(response, f'name="instance_id" value="{watch.id}"')
        self.assertContains(response, reverse("media_save"))
        self.assertNotContains(response, "Mark watched")

    def test_watch_tiles_have_no_delete_but_the_dialog_does(self, _):
        """A watch is deleted from its edit dialog, which asks first, not its tile."""
        self.watch(1)

        content = self.client.get(self.episode_url(1)).content.decode()

        tiles = content.split("Your Watches")[1].split("Edit Watch</h2>")[0]
        self.assertNotIn(reverse("media_delete"), tiles)
        self.assertIn('aria-label="Edit watch"', content)
        edit_dialog = content.split("Edit Watch</h2>")[1]
        self.assertRegex(
            edit_dialog,
            r'hx-post="'
            + reverse("media_delete")
            + r'[^"]*"[^>]*hx-confirm="Delete this watch\?"',
        )
        self.assertIn('aria-label="Delete watch"', edit_dialog)

    def test_previous_watches_in_the_track_dialog_ask_before_deleting(self, _):
        """Previous watches in the Track Episode dialog delete after a confirm."""
        watch = self.watch(1)

        content = self.client.get(self.episode_url(1)).content.decode()

        track_dialog = content.split("Track Episode</h2>")[1].split("Edit Watch")[0]
        self.assertIn(f'"instance_id": "{watch.id}"', track_dialog)
        self.assertIn('hx-confirm="Delete this watch?"', track_dialog)

    def test_the_edit_dialogs_fields_delete_the_watch(self, _):
        """Posting the Edit Watch form's fields to media_delete removes the watch."""
        watch = self.watch(1)

        self.client.post(
            reverse("media_delete") + f"?next={self.episode_url(1)}",
            {
                "instance_id": watch.id,
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "season_number": 1,
                "end_date": "2024-10-06",
                "notes": "",
            },
        )

        self.assertFalse(Episode.objects.filter(pk=watch.pk).exists())

    def test_editing_a_watch_saves_it(self, _):
        """A watch's edit form changes its notes, keeping its rating and season."""
        watch = self.watch(1, notes="First thoughts")
        Episode.objects.filter(pk=watch.pk).update(score=8)
        Season.objects.filter(user=self.user).update(status=Status.COMPLETED.value)

        response = self.client.post(
            reverse("media_save") + f"?next={self.episode_url(1)}",
            {
                "instance_id": watch.id,
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "season_number": 1,
                "end_date": "2024-10-06",
                "notes": "Second thoughts",
            },
        )

        self.assertRedirects(response, self.episode_url(1))
        watch.refresh_from_db()
        self.assertEqual(watch.notes, "Second thoughts")
        self.assertEqual(watch.end_date.date().isoformat(), "2024-10-06")
        self.assertEqual(watch.score, 8)
        self.assertEqual(
            Season.objects.get(user=self.user).status,
            Status.COMPLETED.value,
        )

    def test_one_click_watch_adds_one_watch(self, _):
        """A second click, e.g. while the page reloads, doesn't watch it again."""
        self.watch(1)
        data = {
            "media_id": "1396",
            "source": Sources.TMDB.value,
            "season_number": 1,
            "episode_number": 2,
            "quick": "1",
        }

        self.client.post(reverse("episode_save"), data)
        self.client.post(reverse("episode_save"), data)

        self.assertEqual(
            Episode.objects.filter(
                related_season__user=self.user,
                item__episode_number=2,
            ).count(),
            1,
        )

    def test_last_episode_has_no_next(self, _):
        """The season's last episode leads back to the one before it only."""
        response = self.client.get(self.episode_url(3))

        self.assertContains(response, 'aria-label="Previous episode"')
        self.assertNotContains(response, 'aria-label="Next episode"')

    def test_watches_show_their_date_and_rating(self, _):
        """A watch reads when it was in words, with stars that rate it."""
        watch = self.watch(1)
        Episode.objects.filter(pk=watch.pk).update(
            end_date=datetime(2024, 10, 6, 12, tzinfo=UTC),
            score=8,
        )

        response = self.client.get(self.episode_url(1))

        self.assertContains(response, "October 6, 2024")
        score_url = reverse(
            "update_media_score",
            args=[MediaTypes.EPISODE.value, watch.id],
        )
        self.assertContains(response, f"ratingStars(8.0, '{score_url}'")

    def test_rating_a_watch_keeps_the_season_status(self, _):
        """Rating an earlier episode of a finished season doesn't reopen it."""
        watch = self.watch(1)
        Season.objects.filter(user=self.user).update(status=Status.COMPLETED.value)

        self.client.post(
            reverse("update_media_score", args=[MediaTypes.EPISODE.value, watch.id]),
            {"score": "7.5"},
        )

        watch.refresh_from_db()
        self.assertEqual(float(watch.score), 7.5)
        self.assertEqual(
            Season.objects.get(user=self.user).status,
            Status.COMPLETED.value,
        )

    def test_season_episodes_link_to_their_pages(self, _):
        """A season's episodes open their pages, and can be watched in one click."""
        self.watch(1)

        response = self.client.get(self.season_url())

        self.assertContains(response, f'href="{self.episode_url(1)}"')
        self.assertContains(response, f'href="{self.episode_url(2)}"')
        # The watched episode offers to plan a rewatch, and the unwatched one to
        # mark it
        self.assertContains(response, 'aria-label="Plan to rewatch"', 1)
        self.assertContains(response, 'title="Watched 1 time"', 1)
        self.assertContains(response, 'aria-label="Mark watched"', 1)
        # The unaired episode shows when it airs instead
        self.assertContains(
            response, 'aria-label="Not released yet: E3 · January 1, 2099"', 1
        )
        self.assertNotContains(response, "Track episode")

    def test_season_menu_lists_specials_last(self, _):
        """The season menu has the show's specials after its numbered seasons."""
        response = self.client.get(self.season_url())

        content = response.content.decode()
        self.assertContains(response, 'aria-label="Breadcrumb"')
        self.assertLess(content.index(">Season 2<"), content.index(">Specials<"))

    def test_unwatched_episodes_of_a_tracked_season_show_planned(self, _):
        """Unwatched episodes of a tracked season show the planned icon, like cards."""
        self.watch(1)

        response = self.client.get(self.season_url())

        planned_color = config.get_status_text_color(Status.PLANNING.value)
        self.assertRegex(
            response.content.decode(),
            rf'(?s)aria-label="Mark watched"(?:(?!</button>).)*{planned_color}',
        )

    def test_rewatched_season_shows_what_is_left(self, _):
        """While a season is rewatched, episodes not seen again are offered again."""
        first = self.watch(1)
        self.watch(2)
        season = first.related_season
        season.status = Status.PLANNING_REPEAT.value
        season.repeat_started_at = timezone.now()
        Season.save_base(season)
        Episode.objects.bulk_create(
            [Episode(related_season=season, item=first.item)],
        )

        content = self.client.get(self.season_url()).content.decode()

        self.assertIn('aria-label="Rewatched"', content)
        repeat_color = config.get_status_text_color(Status.PLANNING_REPEAT.value)
        self.assertRegex(
            content,
            rf'(?s)aria-label="Mark watched"(?:(?!</button>).)*{repeat_color}',
        )
        self.assertContains(self.client.get(self.episode_url(2)), "Mark rewatched")

        data = {
            "media_id": "1396",
            "source": Sources.TMDB.value,
            "season_number": 1,
            "episode_number": 2,
            "quick": "1",
        }
        self.client.post(reverse("episode_save"), data)
        self.client.post(reverse("episode_save"), data)

        self.assertEqual(season.episodes.filter(item__episode_number=2).count(), 2)
        season.refresh_from_db()
        self.assertEqual(season.status, Status.COMPLETED.value)

    def test_single_episode_rewatch(self, _):
        """A watched episode can be planned again alone, leaving its season as it is."""
        first = self.watch(1)
        season = first.related_season
        season.status = Status.COMPLETED.value
        Season.save_base(season)
        page = self.client.get(self.episode_url(1))
        self.assertContains(page, "Plan to rewatch")
        self.assertContains(page, reverse("episode_plan_repeat"))
        data = {
            "media_id": "1396",
            "source": Sources.TMDB.value,
            "season_number": 1,
            "episode_number": 1,
        }

        self.client.post(reverse("episode_plan_repeat"), data)
        # An unwatched episode isn't planned
        self.client.post(reverse("episode_plan_repeat"), {**data, "episode_number": 2})

        season.refresh_from_db()
        self.assertEqual(season.planned_repeat_episodes, [1])
        repeat_color = config.get_status_text_color(Status.PLANNING_REPEAT.value)
        self.assertRegex(
            self.client.get(self.season_url()).content.decode(),
            rf'(?s)aria-label="Mark watched"(?:(?!</button>).)*{repeat_color}',
        )
        self.assertContains(self.client.get(self.episode_url(1)), "Mark rewatched")

        self.client.post(reverse("episode_save"), {**data, "quick": "1"})
        self.client.post(reverse("episode_save"), {**data, "quick": "1"})

        season.refresh_from_db()
        self.assertEqual(season.episodes.count(), 2)
        self.assertEqual(season.planned_repeat_episodes, [])
        self.assertEqual(season.status, Status.COMPLETED.value)

    def test_season_can_be_marked_watched_at_once(self, _):
        """Beside its next episode, a season offers to mark all of it watched."""
        first = self.watch(1)

        response = self.client.get(self.season_url())

        self.assertContains(response, 'aria-label="Mark season watched"')
        self.assertContains(response, '"action": "complete", "finish": "1"')

        self.client.post(
            reverse("quick_action"),
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "instance_id": first.related_season.id,
                "action": "complete",
                "finish": "1",
                "card": "details",
            },
        )

        # The released episodes are watched, and the unaired one is left
        self.assertEqual(
            sorted(
                first.related_season.episodes.values_list(
                    "item__episode_number",
                    flat=True,
                ),
            ),
            [1, 2],
        )

    def test_breadcrumb_ends_at_the_current_page(self, _):
        """The breadcrumb links up to the show and season, and names the page."""
        response = self.client.get(self.episode_url(1))

        self.assertRegex(
            response.content.decode(),
            r'aria-current="page"[^>]*>\s*Episode 1\s*<',
        )

    def test_show_page_season_cards(self, _):
        """Season cards count their episodes, and their progress while in progress."""
        self.watch(1)
        Season.objects.filter(user=self.user).update(status=Status.IN_PROGRESS.value)

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "1396",
                    "title": "breaking-bad",
                },
            ),
        )

        content = re.sub(r"\s+", " ", response.content.decode())
        self.assertIn("1 / 7 Episodes", content)
        self.assertIn("3 Episodes", content)
        self.assertNotIn("1 / 3 Episodes", content)
        # The unreleased season shows when it comes out instead of an action
        self.assertIn('aria-label="Not released yet: E1 · January 1, 2099"', content)
        self.assertEqual(content.count("Not released yet:"), 1)

    def test_caught_up_season_and_show_show_the_next_release(self, _):
        """With every released episode watched, both pages show the next one's date."""
        first = self.watch(1)
        self.watch(2)
        season = first.related_season
        season.related_tv.status = Status.IN_PROGRESS.value
        season.related_tv.save_base()
        for number, aired in ((1, True), (2, True), (3, False)):
            Event.objects.create(
                item=season.item,
                content_number=number,
                datetime=timezone.now() + timezone.timedelta(days=-30 if aired else 30),
            )
        show_url = reverse(
            "media_details",
            kwargs={
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "media_id": "1396",
                "title": "breaking-bad",
            },
        )

        for url in (self.season_url(), show_url):
            content = self.client.get(url).content.decode()
            self.assertRegex(content, r'<span title="Next episode">S1E3 · ')
            self.assertNotIn("Mark S1E3 watched", content)

    def test_show_in_progress_watches_its_next_episode(self, _):
        """A show in progress offers its next episode, watched in its season."""
        self.watch(1)
        season = Season.objects.get(user=self.user)
        season.related_tv.status = Status.IN_PROGRESS.value
        season.related_tv.save_base()

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "1396",
                    "title": "breaking-bad",
                },
            ),
        )

        self.assertContains(response, "Mark S1E2 watched")
        self.assertContains(
            response,
            f'"media_type": "{MediaTypes.SEASON.value}", "season_number": "1", '
            f'"instance_id": "{season.id}", "action": "next"',
        )

    def test_episode_menu_lists_the_season(self, _):
        """The episode menu lists the season's episodes, marking the current one."""
        response = self.client.get(self.episode_url(2))

        content = response.content.decode()
        self.assertContains(response, 'aria-label="Previous episode"')
        self.assertContains(response, 'aria-label="Next episode"')
        self.assertRegex(content, r"Episode 2\s*<span")
        for number in (1, 2, 3):
            self.assertIn(f'href="{self.episode_url(number)}"', content)
        self.assertRegex(
            content,
            rf'href="{self.episode_url(2)}"\s*class="[^"]*bg-white/10[^"]*"\s*'
            r'aria-current="page"',
        )

    def test_season_page_shows_the_shows_recommendations(self, _):
        """A season page has the same recommendations as its show."""
        response = self.client.get(self.season_url())

        self.assertRegex(
            response.content.decode(),
            r'x-data="scrollRow"[^<]*>\s*<div[^>]*>\s*<h2[^>]*>Recommendations</h2>',
        )
        self.assertContains(response, "The Sopranos")


class ManualShowTests(TestCase):
    """Test the pages of a show made by hand, whose metadata has no air dates."""

    def setUp(self):
        """Create a user and log in, and a show with one season of two episodes."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        common = {
            "media_id": "m1",
            "source": Sources.MANUAL.value,
            "image": "http://example.com/manual.jpg",
        }
        Item.objects.create(
            **common, media_type=MediaTypes.TV.value, title="Home Movies"
        )
        Item.objects.create(
            **common,
            media_type=MediaTypes.SEASON.value,
            title="Home Movies",
            season_number=1,
        )
        for number in (1, 2):
            Item.objects.create(
                **common,
                media_type=MediaTypes.EPISODE.value,
                title=f"Manual Episode {number}",
                season_number=1,
                episode_number=number,
            )

    def test_episode_page(self):
        """A manual episode has a page like any other."""
        response = self.client.get(
            reverse(
                "episode_details",
                kwargs={
                    "source": Sources.MANUAL.value,
                    "media_id": "m1",
                    "title": "home-movies",
                    "season_number": 1,
                    "episode_number": 2,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manual Episode 2</h1>")
        self.assertContains(response, 'aria-label="Previous episode"')

    def test_season_cards_are_not_upcoming(self):
        """Manual seasons have no air dates, so they aren't shown as unreleased."""
        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MANUAL.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "m1",
                    "title": "home-movies",
                },
            ),
        )

        self.assertContains(response, "2 Episodes")
        self.assertNotContains(response, "Not released yet")
        # The card offers its action, as the release date would replace it
        self.assertContains(response, 'aria-label="Watchlist"')
