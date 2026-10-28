"""Template tag that decides what an action button draws and posts.

`action_button.html` draws what `get_action_button` returns, in every position
a media's one-click action appears in.
"""

import json
from dataclasses import dataclass
from urllib.parse import quote

from django import template
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from app import config, quick_actions
from app.models import MediaTypes, Status
from app.templatetags.app_tags import (
    air_date_label,
    play_count,
    release_label,
    season_release_label,
)

register = template.Library()

ACTION = "action"
RELEASE = "release"
STATUS = "status"
NONE = "none"

PLUS_ICON = "app/icons/plus.svg"
CLOCK_ICON = "app/icons/clock.svg"
NO_STATUS_COLOR = "text-gray-200"
NO_STATUS_FILL = "bg-surface-600 hover:bg-surface-550"

# Tells the card the log dialog was opened from, so the dialog can answer it
LOG_ONCLICK = (
    "window.dispatchEvent(new CustomEvent('open-log', "
    "{detail: {card: this.closest('.media-card, .media-row')}}))"
)
# The element a card's response replaces, by the `card` it was posted from
CARD_TARGETS = {"row": ".media-row", "grid": ".media-card", "home": ".media-card"}


@dataclass(frozen=True)
class Button:
    """What a button draws, whichever position it is in.

    `kind` is an ACTION, or what it shows without one: a RELEASE, a STATUS or
    NONE. An action's `attrs` are its htmx attributes, its `icon` and `fill`
    those of the status it leads to, and its `rest_icon` and `rest_color` those
    of the status it is in. A fallback has the `icon`, `label` and `fill` it
    shows. `state` is what it shows without an action, which for an action is
    only there to colour a release shown beside it. `crossfade` is whether the
    large button shows `rest_label` until hover, with `base_fill` as its color
    then.
    """

    kind: str
    action: quick_actions.QuickAction | None = None
    state: quick_actions.ButtonState | None = None
    attrs: str = ""
    label: str = ""
    title: str = ""
    icon: str = ""
    fill: str = ""
    base_fill: str = ""
    rest_icon: str = PLUS_ICON
    rest_color: str = NO_STATUS_COLOR
    rest_label: str = ""
    crossfade: bool = False
    reveal: bool = False
    tap_through: bool = False
    finish: "Button | None" = None
    form_attrs: str = ""

    @property
    def clock_color(self):
        """Return the color of a release's clock, by the status it waits in."""
        if self.state is None or not self.state.release:
            return ""
        return config.get_status_clock_color(self.state.status)


def _field(obj, name):
    """Return a field of a metadata dict or a model, or None."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _json_attr(name, value):
    """Return an htmx attribute holding JSON in single quotes, left unescaped."""
    safe = (
        json.dumps(value)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("'", "\\u0027")
    )
    return mark_safe(f"{name}='{safe}'")  # noqa: S308 escaped above


def _attrs(pairs):
    """Return HTML attributes from (name, value) pairs, dict values as JSON."""
    return format_html_join(
        " ",
        "{}",
        (
            (
                _json_attr(name, value)
                if isinstance(value, dict)
                else format_html('{}="{}"', name, value),
            )
            for name, value in pairs
        ),
    )


def _post_attrs(action, p, *, finish=False):
    """Return the htmx attributes of a button that posts `action`, or opens its log.

    `p` is what the button is for, from `get_action_button`.
    """
    ref, entry, card = p["ref"], p["entry"], p["card"]
    media_type = _field(ref, "media_type")
    season_number = _field(ref, "season_number")
    card_state = {} if card == "details" else p["card_state"]
    if action.key == quick_actions.LOG:
        return _attrs(
            [
                ("hx-get", reverse("log_modal", args=[media_type, entry.id])),
                ("hx-vals", {"card": card, **card_state, "next": p["path"]}),
                ("hx-target", "#log-dialog"),
                ("hx-swap", "innerHTML"),
                ("hx-sync", "#log-dialog:replace"),
                ("onclick", LOG_ONCLICK),
                *_extra_attrs(p),
            ]
        )

    vals = {
        "media_id": str(_field(ref, "media_id")),
        "source": _field(ref, "source"),
        "media_type": media_type,
        "season_number": "" if season_number is None else str(season_number),
    }
    if media_type == MediaTypes.EPISODE.value:
        vals["episode_number"] = str(_field(ref, "episode_number"))
    vals["instance_id"] = str(_field(entry, "id") or "")
    vals["action"] = action.key
    if finish:
        vals["finish"] = "1"
    vals["card"] = card
    vals.update(card_state)
    url = reverse("quick_action")
    if card == "details":
        # The page is reloaded, back to where it was
        url += f"?next={quote(p['path'])}"
        where = [("hx-swap", "none")]
    else:
        where = [
            ("hx-target", f"closest {CARD_TARGETS[card]}"),
            ("hx-swap", "outerHTML"),
        ]
    return _attrs(
        [
            ("hx-post", url),
            ("hx-vals", vals),
            ("hx-headers", {"X-CSRFToken": p["csrf_token"]}),
            *where,
            *_extra_attrs(p),
        ]
    )


def _extra_attrs(p):
    """Return the attributes every action button has, after its own."""
    return [
        *([("hx-include", "#filter-form")] if p["filtered"] else []),
        ("hx-disabled-elt", "this"),
    ]


def _rest_status(entry, episode):
    """Return the status a button's media is in, or None when it isn't tracked."""
    if episode is not None:
        return quick_actions.episode_status(entry, episode)
    return entry.status if entry else None


def _plays(media_type, ref, entry, user_medias):
    """Return how often media was finished."""
    if media_type == MediaTypes.EPISODE.value:
        return len(ref["history"])
    return play_count(entry, user_medias or [entry])


def _release_label(state, media_type, ref, user):
    """Return when a release is: its event, else an episode's air date."""
    if state.event:
        label = (
            season_release_label if media_type == MediaTypes.TV.value else release_label
        )
        return label(state.event, user)
    # Without one, only an episode is waiting, known by its air date
    return air_date_label(ref["air_date"], ref["episode_number"])


def build_button(p):
    """Return the Button for what `p` describes, as `get_action_button` collects it."""
    ref, entry, card = p["ref"], p["entry"], p["card"]
    media_type = _field(ref, "media_type")
    episode = ref if media_type == MediaTypes.EPISODE.value else None

    if p["readonly"]:
        action = None
        state = quick_actions.ButtonState(entry.status) if entry else None
    else:
        action = quick_actions.get_quick_action(media_type, entry, episode)
        state = quick_actions.get_button_state(entry, episode)

    rest_status = _rest_status(entry, episode)
    completed = rest_status == Status.COMPLETED.value
    plays = _plays(media_type, ref, entry, p["all_entries"]) if completed else 0
    past = config.get_verb(media_type, past_tense=True).capitalize()
    rest_label = (f"{past} {plays} times" if plays > 1 else past) if completed else ""
    if action is None:
        return _fallback_button(p, media_type, state, rest_label)

    fill = config.get_status_button_color(action.target_status)
    crossfade = action.key == quick_actions.PLAN_REPEAT and bool(rest_label)
    finish = None
    if p["finish_ref"] is not None and action.key in (
        quick_actions.NEXT,
        quick_actions.LOG,
    ):
        finish_action = quick_actions.get_finish_action(
            _field(p["finish_ref"], "media_type"), p["finish_entry"]
        )
        if finish_action:
            finish = Button(
                ACTION,
                action=finish_action,
                attrs=_post_attrs(
                    finish_action,
                    {**p, "ref": p["finish_ref"], "entry": p["finish_entry"]},
                    finish=True,
                ),
                icon=finish_action.icon,
                fill=config.get_status_button_color(finish_action.target_status),
            )
    return Button(
        ACTION,
        action=action,
        attrs=_post_attrs(action, p),
        title=rest_label if crossfade and plays > 1 else action.label,
        icon=action.icon,
        fill=fill,
        base_fill=config.get_status_button_color(rest_status) if crossfade else fill,
        rest_icon=config.get_status_icon(rest_status) if rest_status else PLUS_ICON,
        rest_color=(
            config.get_status_text_color(rest_status)
            if rest_status
            else NO_STATUS_COLOR
        ),
        rest_label=rest_label,
        crossfade=crossfade,
        reveal=card == "home",
        tap_through=p["tap_through"],
        finish=finish,
        state=state,
    )


def _fallback_button(p, media_type, state, rest_label):
    """Return the Button of media with no action, from what it waits for or is in."""
    ref = p["ref"]
    form_attrs = ""
    if p["form_url"]:
        form_attrs = _attrs(
            [
                ("hx-get", p["form_url"]),
                (
                    "hx-vals",
                    {
                        "return_url": quote(p["path"]),
                        "instance_id": str(_field(p["form_entry"], "id") or ""),
                    },
                ),
                ("hx-target", p["form_target"]),
            ]
        )
    base = {"state": state, "form_attrs": form_attrs}
    if state is None:
        return Button(
            NONE, label="Add to tracker", icon=PLUS_ICON, fill=NO_STATUS_FILL, **base
        )

    fill = config.get_status_button_color(state.status)
    if state.release:
        label = _release_label(state, media_type, ref, p["user"])
        return Button(RELEASE, label=label, icon=CLOCK_ICON, fill=fill, **base)
    readable = config.get_status_label(state.status, media_type)
    return Button(
        STATUS,
        label=rest_label or readable,
        title=rest_label or readable,
        icon=config.get_status_icon(state.status),
        fill=fill,
        rest_icon=config.get_status_icon(state.status),
        rest_color=config.get_status_text_color(state.status),
        **base,
    )


@register.simple_tag(takes_context=True)
def get_action_button(context):
    """Return the Button `action_button.html` draws for what it was included with.

    That is a `ref` (the media's Item, a metadata dict or an episode's state),
    its `entry` the user tracks it with or None, and the `card` it is in: grid,
    row, home or details. Optional are `readonly` (for someone else's entry,
    shown without an action), `filtered` (a list sorted by a filter, whose form
    is posted along), `on_card` (false off a card, so a tap on a phone isn't
    left to the card's link), `all_entries` (all of the user's entries of it,
    for how often it was finished) and, on details pages, `finish_ref` and
    `finish_entry` (the page's own media, whose finish button goes beside the
    action) and `form_url`, `form_target` and `form_entry` (the tracking form a
    button without an action opens). A card also posts its `title`, `year`,
    `active` and `home_status`.
    """
    request = context.get("request")
    user = context.get("user")
    p = {
        "ref": context["ref"],
        "entry": context.get("entry"),
        "card": context["card"],
        "readonly": context.get("readonly", False),
        "filtered": context.get("filtered", False),
        "all_entries": context.get("all_entries"),
        "finish_ref": context.get("finish_ref"),
        "finish_entry": context.get("finish_entry"),
        "form_url": context.get("form_url"),
        "form_target": context.get("form_target"),
        "form_entry": context.get("form_entry"),
        "user": user,
        "path": request.get_full_path() if request else "",
        "csrf_token": str(context.get("csrf_token", "")),
        "tap_through": bool(
            context.get("on_card", True)
            and getattr(user, "clickable_media_cards", False)
        ),
        "card_state": {
            "title": str(context.get("title") or ""),
            "release_year": str(context.get("year") or ""),
            "active": "true" if context.get("active") else "",
            "home_status": context.get("home_status") or "",
        },
    }
    return build_button(p)
