"""Ratings from IMDb, Rotten Tomatoes and Metacritic, through the OMDb API."""

import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache

from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://www.omdbapi.com/"

# Ratings load with the page, so OMDb gets a few seconds, and a failure is
# remembered for an hour rather than retried on every page
TIMEOUT = 5
FAILURE_CACHE_TIMEOUT = 3600

# OMDb's names for the rating sources, and the source each one is shown as
SOURCES = {
    "Internet Movie Database": "imdb",
    "Rotten Tomatoes": "rotten_tomatoes",
    "Metacritic": "metacritic",
}


def get_ratings(external_links):
    """Return the IMDb, Rotten Tomatoes and Metacritic ratings of a movie or show.

    The title is found by the IMDb link in `external_links`. Each rating is a
    dict of `source`, `score`, `scale` and `votes`. Returns an empty list when
    no OMDb key is set, the title has no IMDb link, or OMDb can't be reached.
    """
    imdb_url = (external_links or {}).get("IMDb", "")
    imdb_id = re.search(r"tt\d+", imdb_url)
    if not settings.OMDB_API or not imdb_id:
        return []

    cache_key = f"omdb_ratings_{imdb_id.group()}"
    ratings = cache.get(cache_key)
    if ratings is None:
        try:
            response = services.session.get(
                base_url,
                params={"apikey": settings.OMDB_API, "i": imdb_id.group()},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            ratings = process_ratings(response.json())
        except (requests.exceptions.RequestException, ValueError) as error:
            # Extra ratings aren't worth failing the page for. The error's
            # message has the URL, and with it the key, so it isn't logged
            status = getattr(getattr(error, "response", None), "status_code", None)
            logger.warning(
                "Failed to get OMDb ratings: %s %s",
                type(error).__name__,
                status or "",
            )
            cache.set(cache_key, [], FAILURE_CACHE_TIMEOUT)
            return []
        cache.set(cache_key, ratings)
    return ratings


def process_ratings(response):
    """Return the ratings of an OMDb response, in the order of `SOURCES`."""
    if response.get("Response") != "True":
        return []

    by_source = {}
    for rating in response.get("Ratings", []):
        source = SOURCES.get(rating.get("Source"))
        if source:
            # e.g. "8.7/10", "83%" or "73/100"
            score, _, scale = rating.get("Value", "").partition("/")
            by_source[source] = {
                "source": source,
                "score": score,
                "scale": f"/{scale}" if scale else "",
                "votes": None,
            }

    if "imdb" in by_source:
        votes = response.get("imdbVotes", "").replace(",", "")
        by_source["imdb"]["votes"] = int(votes) if votes.isdigit() else None
    return [by_source[source] for source in SOURCES.values() if source in by_source]
