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
        """Track the first season and add a watch of one of its episodes."""
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
        episode_item, _ = Item.objects.get_or_create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=number,
            defaults={"title": "Breaking Bad", "image": "http://example.com/e.jpg"},
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

    def track_modal_url(self, number):
        """Return the URL of the entry dialog of an episode of the first season."""
        return reverse(
            "track_modal",
            kwargs={
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "season_number": 1,
                "episode_number": number,
            },
        )

    def test_edit_tracking_opens_the_latest_entry(self, _):
        """The pencil opens the dialog on the entry added last, of any status."""
        self.watch(1, notes="Loved it")
        latest = self.entry(1, Status.PLANNING_REPEAT.value)
        Episode.objects.filter(pk=latest.pk).update(
            start_date=datetime(2024, 10, 5, 12, tzinfo=UTC),
            score=7,
            notes="Again",
        )
        # Another episode's entry, added later, isn't this one's
        self.watch(2)

        page_response = self.client.get(self.episode_url(1))
        self.assertEqual(page_response.context["latest_entry"], latest)
        page = page_response.content.decode()
        self.assertIn('aria-label="Edit tracking"', page)
        self.assertIn(self.track_modal_url(1), page)
        self.assertIn(f'"instance_id": "{latest.id}"', page)

        response = self.client.get(
            self.track_modal_url(1),
            {"return_url": self.episode_url(1)},
        )

        self.assertTemplateUsed(response, "app/components/fill_track.html")
        self.assertEqual(response.context["media"], latest)
        self.assertEqual(str(response.context["title"]), "Breaking Bad S1E1")
        content = response.content.decode()
        self.assertRegex(
            content,
            r'value="Planning repeat"\s*checked>',
        )
        self.assertRegex(content, r"</svg>\s*Planning Rewatch\s*</span>")
        self.assertIn("mediaForm(7.0, null)", content)
        for name in ("start_date", "end_date", "notes"):
            self.assertIn(f'name="{name}"', content)
        self.assertIn('value="2024-10-05', content)
        self.assertIn(f'name="instance_id" value="{latest.id}"', content)
        self.assertIn(f'hx-post="{reverse("media_save")}', content)
        self.assertIn('aria-label="Delete entry"', content)
        self.assertIn("$dispatch('dialog-ready')", content)

    def test_saving_the_edit_dialog_changes_the_entry(self, _):
        """The dialog's fields save the status, dates, notes and rating."""
        planned = self.entry(1, Status.PLANNING.value)

        response = self.client.post(
            reverse("media_save") + f"?next={self.episode_url(1)}",
            {
                "instance_id": planned.id,
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "season_number": 1,
                "episode_number": 1,
                "status": Status.COMPLETED.value,
                "start_date": "2024-10-05",
                "end_date": "2024-10-06",
                "notes": "Second thoughts",
                "score": "8.5",
            },
        )

        self.assertRedirects(response, self.episode_url(1))
        planned.refresh_from_db()
        self.assertEqual(planned.status, Status.COMPLETED.value)
        self.assertEqual(planned.start_date.date().isoformat(), "2024-10-05")
        self.assertEqual(planned.end_date.date().isoformat(), "2024-10-06")
        self.assertEqual(planned.notes, "Second thoughts")
        self.assertEqual(float(planned.score), 8.5)
        # Watching it moved the season on, as adding a watch does
        self.assertEqual(
            Season.objects.get(user=self.user).status,
            Status.IN_PROGRESS.value,
        )

    def test_deleting_from_the_edit_dialog_removes_the_entry(self, _):
        """Posting the dialog's fields to media_delete removes the entry."""
        watch = self.watch(1)

        self.client.post(
            reverse("media_delete") + f"?next={self.episode_url(1)}",
            {
                "instance_id": watch.id,
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "season_number": 1,
                "episode_number": 1,
                "status": Status.COMPLETED.value,
                "end_date": "2024-10-06",
                "notes": "",
                "score": "",
            },
        )

        self.assertFalse(Episode.objects.filter(pk=watch.pk).exists())

    def test_an_episode_without_entries_offers_to_add_one(self, _):
        """With no entry yet, the pencil opens the dialog to add the first."""
        page = self.client.get(self.episode_url(1)).content.decode()

        self.assertIn('aria-label="Add to tracker"', page)
        self.assertNotIn('aria-label="Add new entry"', page)
        response = self.client.get(
            self.track_modal_url(1),
            {"return_url": self.episode_url(1), "instance_id": ""},
        )
        self.assertIsNone(response.context["media"])
        self.assertContains(response, reverse("episode_save"))

    def test_add_new_entry_opens_an_empty_dialog(self, _):
        """Add new entry opens the dialog for a watch, to post to episode_save."""
        self.watch(1)

        page = self.client.get(self.episode_url(1)).content.decode()
        self.assertIn('aria-label="Add new entry"', page)

        response = self.client.get(
            self.track_modal_url(1),
            {"return_url": self.episode_url(1), "is_create": "1"},
        )

        self.assertIsNone(response.context["media"])
        self.assertEqual(response.context["title"], "Breaking Bad S1E1")
        content = response.content.decode()
        self.assertRegex(content, r'value="Completed"\s*checked>')
        self.assertIn(f'hx-post="{reverse("episode_save")}', content)
        self.assertIn('name="episode_number" value="1"', content)
        self.assertIn('name="instance_id"', content)
        self.assertNotIn('name="instance_id" value="', content)
        self.assertNotIn("Delete entry", content)
        self.assertIn('name="start_date"', content)
        self.assertIn("mediaForm(null, null)", content)

    def add_entry(self, number, **fields):
        """Post the add-entry dialog of an episode of the first season."""
        return self.client.post(
            reverse("episode_save") + f"?next={self.episode_url(number)}",
            {
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "season_number": 1,
                "episode_number": number,
                "instance_id": "",
                "status": Status.COMPLETED.value,
                "start_date": "",
                "end_date": "",
                "notes": "",
                "score": "",
                **fields,
            },
        )

    def test_adding_an_entry_records_another_watch(self, _):
        """A new watch of a watched episode is another entry, with all its fields."""
        first = self.watch(1)

        response = self.add_entry(
            1,
            start_date="2024-10-05",
            end_date="2024-10-06",
            notes="Better the second time",
            score="9",
        )

        self.assertRedirects(response, self.episode_url(1))
        entry = Episode.objects.exclude(pk=first.pk).get()
        self.assertEqual(entry.status, Status.COMPLETED.value)
        self.assertEqual(entry.start_date.date().isoformat(), "2024-10-05")
        self.assertEqual(entry.end_date.date().isoformat(), "2024-10-06")
        self.assertEqual(entry.notes, "Better the second time")
        self.assertEqual(entry.score, 9)

    def test_adding_a_watch_turns_the_pending_entry_into_it(self, _):
        """A watch added for a planned episode is its plan, watched, not another."""
        planned = self.entry(2, Status.PLANNING.value)

        self.add_entry(2, end_date="2024-10-06", score="6")

        entry = Episode.objects.get()
        self.assertEqual(entry.pk, planned.pk)
        self.assertEqual(entry.status, Status.COMPLETED.value)
        self.assertEqual(entry.score, 6)
        self.assertEqual(entry.end_date.date().isoformat(), "2024-10-06")

    def test_adding_an_entry_in_another_status(self, _):
        """An entry added in a status that isn't a watch is added as it is."""
        season = self.watch(1).related_season
        self.watch(2)

        self.add_entry(2, status=Status.PLANNING_REPEAT.value, notes="Soon")

        self.assertEqual(
            sorted(Episode.objects.values_list("status", "notes")),
            [
                (Status.COMPLETED.value, ""),
                (Status.COMPLETED.value, ""),
                (Status.PLANNING_REPEAT.value, "Soon"),
            ],
        )
        added = Episode.objects.get(status=Status.PLANNING_REPEAT.value)
        self.assertEqual(added.item.episode_number, 2)
        season.refresh_from_db()
        self.assertEqual(season.status, Status.IN_PROGRESS.value)

    def test_a_first_planned_entry_plans_its_season(self, _):
        """Planning an episode of a season not tracked yet plans the season."""
        self.add_entry(1, status=Status.PLANNING.value)

        self.assertEqual(
            Season.objects.get(user=self.user).status,
            Status.PLANNING.value,
        )
        self.assertEqual(Episode.objects.get().status, Status.PLANNING.value)

    def test_an_invalid_entry_is_refused(self, _):
        """A status episodes don't have isn't saved."""
        response = self.add_entry(1, status="Watched")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Episode.objects.exists())

    def test_completing_a_season_settles_an_episodes_other_plans(self, _):
        """With two entries planning an episode, completing its season keeps one."""
        self.watch(2)
        self.add_entry(1, status=Status.PLANNING.value)
        self.add_entry(1, status=Status.IN_PROGRESS.value, start_date="2024-10-05")
        season = Season.objects.get(user=self.user)
        self.assertEqual(season.episodes.filter(item__episode_number=1).count(), 2)

        self.client.post(
            reverse("quick_action"),
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "instance_id": season.id,
                "action": "complete",
                "finish": "1",
                "card": "details",
            },
        )

        self.assertEqual(
            list(
                season.episodes.filter(item__episode_number=1).values_list(
                    "status",
                    flat=True,
                ),
            ),
            [Status.COMPLETED.value],
        )

    def test_tiles_are_the_episodes_entries(self, _):
        """Your Watches lists every entry with its status, each opening its dialog."""
        watch = self.watch(1, notes="Loved it")
        planned = self.entry(1, Status.PLANNING_REPEAT.value)
        other = self.watch(2)

        response = self.client.get(self.episode_url(1))

        self.assertEqual(response.context["entries"], [planned, watch])
        content = response.content.decode()
        self.assertRegex(content, r"Your Watches</h2>\s*<span[^>]*>2</span>")
        self.assertContains(response, "Loved it")
        tiles = content.split("Your Watches</h2>")[1]
        self.assertIn('title="Planning Rewatch"', tiles)
        self.assertIn('title="Completed"', tiles)
        for entry in (watch, planned):
            self.assertIn(f'"instance_id": "{entry.id}"', tiles)
        self.assertNotIn(f'"instance_id": "{other.id}"', tiles)
        self.assertIn('aria-label="Edit entry"', tiles)
        # The tiles don't delete; their dialogs do
        self.assertNotIn(reverse("media_delete"), content)
        # The watches count only the completed entry
        self.assertEqual(len(response.context["media"]["history"]), 1)

    def test_episode_page_has_only_entry_dialogs(self, _):
        """The episode page has only the entry dialogs, which fetch their forms."""
        self.watch(1)

        content = self.client.get(self.episode_url(1)).content.decode()

        for old in ("Track Episode", "Edit Watch", "Add watch", "Fill in air date"):
            self.assertNotIn(old, content)
        self.assertNotIn('@click="trackOpen = true"', content)
        self.assertNotIn(reverse("media_save"), content)

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
        second = self.watch(2)
        season = first.related_season
        season.status = Status.PLANNING_REPEAT.value
        Season.save_base(season)
        # The first episode was watched again, and the second is still to be
        Episode.objects.bulk_create(
            [
                Episode(related_season=season, item=first.item),
                Episode(
                    related_season=season,
                    item=second.item,
                    status=Status.PLANNING_REPEAT.value,
                ),
            ],
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

        self.assertEqual(
            list(
                season.episodes.filter(
                    status=Status.PLANNING_REPEAT.value,
                ).values_list("item__episode_number", flat=True),
            ),
            [1],
        )
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
        self.assertEqual(season.episodes.watches().count(), 2)
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

    def entry(self, number, status):
        """Add an entry of an episode of the first season in a status."""
        watch = self.watch(number)
        Episode.objects.filter(pk=watch.pk).update(status=status)
        return watch

    def test_unfinished_entries_are_not_watches_on_the_episode_page(self, _):
        """Only completed entries are listed and counted as the episode's watches."""
        self.entry(1, Status.PLANNING.value)

        response = self.client.get(self.episode_url(1))

        self.assertEqual(response.context["media"]["history"], [])
        self.assertNotContains(response, "Your Watches")
        self.assertContains(response, "Mark watched")

    def test_one_click_watches_an_episode_only_planned(self, _):
        """A planned entry doesn't stop one click from adding the watch."""
        self.entry(2, Status.PLANNING.value)

        self.client.post(
            reverse("episode_save"),
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "season_number": 1,
                "episode_number": 2,
                "quick": "1",
            },
        )

        self.assertEqual(
            Episode.objects.filter(
                item__episode_number=2,
                status=Status.COMPLETED.value,
            ).count(),
            1,
        )

    def test_rewatch_is_planned_once_for_watched_episodes(self, _):
        """A watched episode is planned again once, and one only planned isn't."""
        self.watch(1)
        self.entry(2, Status.PLANNING.value)
        data = {
            "media_id": "1396",
            "source": Sources.TMDB.value,
            "season_number": 1,
        }

        for number in (1, 1, 2):
            self.client.post(
                reverse("episode_plan_repeat"),
                {**data, "episode_number": number},
            )

        self.assertEqual(
            sorted(
                Episode.objects.values_list("item__episode_number", "status"),
            ),
            [
                (1, Status.COMPLETED.value),
                (1, Status.PLANNING_REPEAT.value),
                (2, Status.PLANNING.value),
            ],
        )


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
