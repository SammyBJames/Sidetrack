import html
import json
import re

from django.urls import reverse

QUICK_ACTION_BUTTON = re.compile(
    r"<button[^>]*?hx-post=\"([^\"]*)\"[^>]*?hx-vals='([^']*)'[^>]*>",
)


def click_quick_action(client, url, **match):
    """Load a page and post its first one-click button as a click would.

    The button is the first whose `hx-vals` hold every `match` item; its
    `hx-post` URL and `hx-vals` are posted unchanged. Returns the button's
    values and the response.
    """
    content = client.get(url).content.decode()
    for raw_url, raw_vals in QUICK_ACTION_BUTTON.findall(content):
        post_url = html.unescape(raw_url)
        vals = json.loads(raw_vals)
        if not post_url.startswith(reverse("quick_action")):
            continue
        if all(vals.get(key) == value for key, value in match.items()):
            response = client.post(post_url, vals, headers={"HX-Request": "true"})
            return vals, response
    msg = f"No one-click button with {match} on {url}"
    raise AssertionError(msg)
