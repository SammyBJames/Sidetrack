import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from app import helpers
from app.models import REPEAT_STATUSES, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)
base_url = "https://api.themoviedb.org/3"
base_params = {
    "api_key": settings.TMDB_API,
    "language": settings.TMDB_LANG,
}

# Limits the appended images to those in the user's language, to pick a logo from
image_language = settings.TMDB_LANG.split("-")[0]
images_params = {"include_image_language": image_language}

# The most items TMDB adds to one response with append_to_response
MAX_APPENDS = 20

# The most cast and crew members shown, with a link to the rest on TMDB
MAX_PEOPLE = 30

# The crew jobs worth showing, most important first
CREW_JOBS = (
    "Creator",
    "Director",
    "Screenplay",
    "Writer",
    "Story",
    "Novel",
    "Characters",
    "Producer",
    "Executive Producer",
    "Original Music Composer",
    "Director of Photography",
    "Editor",
)

# The kinds of videos shown, trailers first
VIDEO_TYPES = (
    "Trailer",
    "Teaser",
    "Clip",
    "Featurette",
    "Behind the Scenes",
    "Opening Credits",
    "Bloopers",
)

# The most videos shown
MAX_VIDEOS = 20

# What a show's details add to its response, before any seasons
TV_APPENDS = (
    "recommendations,external_ids,watch/providers,images,content_ratings,"
    "aggregate_credits,videos"
)

# TMDB's release types, in the order their age rating is preferred: theatrical,
# limited theatrical, premiere, digital, physical and TV
RELEASE_TYPE_ORDER = (3, 2, 1, 4, 5, 6)


def handle_error(error):
    """Handle TMDB API errors."""
    error_resp = error.response
    status_code = error_resp.status_code

    try:
        error_json = error_resp.json()
    except requests.exceptions.JSONDecodeError as json_error:
        logger.exception("Failed to decode JSON response")
        raise services.ProviderAPIError(Sources.TMDB.value, error) from json_error

    # Handle authentication errors
    if status_code == requests.codes.unauthorized:
        details = error_json.get("status_message")
        if details:
            # Remove trailing period if present
            details = details.rstrip(".")
            raise services.ProviderAPIError(Sources.TMDB.value, error, details)

    raise services.ProviderAPIError(
        Sources.TMDB.value,
        error,
    )


def get_external_links(external_ids, tmdb_id=None):
    """Build external links dictionary from TMDB external_ids response."""
    links = {}

    if external_ids.get("imdb_id"):
        links["IMDb"] = f"https://www.imdb.com/title/{external_ids['imdb_id']}/"

    if external_ids.get("tvdb_id"):
        links["TVDB"] = (
            f"https://www.thetvdb.com/dereferrer/series/{external_ids['tvdb_id']}"
        )

    if external_ids.get("wikidata_id"):
        links["Wikidata"] = (
            f"https://www.wikidata.org/wiki/{external_ids['wikidata_id']}"
        )

    # Only passed in for movies as Letterboxd seldom supports TV
    if tmdb_id:
        # https://letterboxd.com/about/film-data/
        # Letterboxd will redirect to the correct movie
        # as they source their data from TMDB
        links["Letterboxd"] = f"https://www.letterboxd.com/tmdb/{tmdb_id}"

    return links


def search(media_type, query, page):
    """Search for media on TMDB."""
    cache_key = f"search_{Sources.TMDB.value}_{media_type}_{query}_{page}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/search/{media_type}"

        params = {
            **base_params,
            "query": query,
            "page": page,
        }

        if settings.TMDB_NSFW:
            params["include_adult"] = "true"

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        results = [
            {
                "media_id": media["id"],
                "source": Sources.TMDB.value,
                "media_type": media_type,
                "title": get_title(media),
                "image": get_image_url(media["poster_path"]),
                "release_year": get_release_year(media),
            }
            for media in response["results"]
        ]

        total_results = response["total_results"]
        per_page = 20  # TMDB always returns 20 results per page
        data = helpers.format_search_response(
            page,
            per_page,
            total_results,
            results,
        )

        cache.set(cache_key, data)

    return data


def find(external_id, external_source):
    """Search for media on TMDB."""
    cache_key = f"find_{Sources.TMDB.value}_{external_id}_{external_source}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/find/{external_id}"

        params = {
            **base_params,
            "external_source": external_source,
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        cache.set(cache_key, response)
        return response

    return data


def movie(media_id):
    """Return the metadata for the selected movie from The Movie Database."""
    cache_key = f"{Sources.TMDB.value}_{MediaTypes.MOVIE.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/movie/{media_id}"
        appends = [
            "recommendations",
            "external_ids",
            "credits",
            "watch/providers",
            "images",
            "release_dates",
            "videos",
        ]
        params = {
            **base_params,
            **images_params,
            "append_to_response": ",".join(appends),
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )

            if response.get("belongs_to_collection", {}) is not None and (
                collection_id := response.get("belongs_to_collection", {}).get("id")
            ):
                try:
                    collection_response = services.api_request(
                        Sources.TMDB.value,
                        "GET",
                        f"{base_url}/collection/{collection_id}",
                        params={**base_params},
                    )
                except requests.exceptions.HTTPError as error:
                    logger.warning("Failed to get collection: %s", error)
                    collection_response = {}
            else:
                collection_response = {}
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        # Filter out collection items from recommendations, to avoid duplicates
        collection_items = get_collection(collection_response)
        collection_ids = [item["media_id"] for item in collection_items]
        recommended_items = response.get("recommendations", {}).get("results", [])
        filtered_recommendations = [
            item for item in recommended_items if item["id"] not in collection_ids
        ]

        data = {
            "media_id": media_id,
            "source": Sources.TMDB.value,
            "source_url": f"https://www.themoviedb.org/movie/{media_id}",
            "media_type": MediaTypes.MOVIE.value,
            "title": response["title"],
            "max_progress": 1,
            "image": get_image_url(response["poster_path"]),
            "backdrop": get_backdrop_url(response.get("backdrop_path")),
            "logo": get_logo_url(response.get("images")),
            "certifications": get_movie_certifications(response.get("release_dates")),
            "synopsis": get_synopsis(response["overview"]),
            "genres": get_genres(response["genres"]),
            "score": get_score(response["vote_average"]),
            "score_count": response["vote_count"],
            "details": {
                "format": "Movie",
                "release_date": get_start_date(response["release_date"]),
                "status": response["status"],
                "runtime": get_readable_duration(response["runtime"]),
                "studios": get_companies(response["production_companies"]),
                "country": get_country(response["production_countries"]),
                "languages": get_languages(response["spoken_languages"]),
            },
            **get_people(response.get("credits")),
            "videos": get_videos(response.get("videos")),
            "related": {
                collection_response.get("name", "collection"): collection_items,
                "recommendations": get_related(
                    filtered_recommendations,
                    MediaTypes.MOVIE.value,
                ),
            },
            "external_links": get_external_links(
                response.get("external_ids", {}), media_id
            ),
            "providers": response.get("watch/providers", {}).get("results", {}),
        }

        cache.set(cache_key, data)

    return data


def get_cached_seasons(media_id, season_numbers):
    """Check cache for seasons and return cached data and list of uncached seasons."""
    cached_data = {}
    uncached_seasons = []

    for season_number in season_numbers:
        season_cache_key = (
            f"{Sources.TMDB.value}_{MediaTypes.SEASON.value}_{media_id}_{season_number}"
        )
        season_data = cache.get(season_cache_key)
        if season_data:
            cached_data[f"season/{season_number}"] = season_data
        else:
            uncached_seasons.append(season_number)

    return cached_data, uncached_seasons


def enrich_season_with_tv_data(season_data, tv_data, media_id, season_number):
    """Add TV show metadata to season metadata."""
    season_data["media_id"] = media_id
    season_data["source_url"] = (
        f"https://www.themoviedb.org/tv/{media_id}/season/{season_number}"
    )
    season_data["title"] = tv_data["title"]
    season_data["tvdb_id"] = tv_data["tvdb_id"]
    season_data["external_links"] = tv_data["external_links"]
    season_data["genres"] = tv_data["genres"]
    # seasons have no backdrop or logo of their own
    season_data["backdrop"] = tv_data.get("backdrop")
    season_data["logo"] = tv_data.get("logo")
    season_data["certifications"] = tv_data.get("certifications", {})
    if season_data["synopsis"] == "No synopsis available.":
        season_data["synopsis"] = tv_data["synopsis"]
    # seasons often have no poster of their own, use the show's instead
    if season_data["image"] == settings.IMG_NONE:
        season_data["image"] = tv_data["image"]
    return season_data


def fetch_and_cache_seasons(media_id, season_numbers, tv_data):
    """Fetch uncached seasons from API and cache them."""
    url = f"{base_url}/tv/{media_id}"
    season_appends = ("", "/watch/providers", "/aggregate_credits", "/videos")
    fetched_tv_data = tv_data
    result_data = {}

    remaining = list(season_numbers)
    while remaining:
        # The show's own appends only come along until it's fetched. TMDB
        # appends at most 20 items, and each season takes one of each above
        show_appends = TV_APPENDS.split(",") if fetched_tv_data is None else []
        batch_size = (MAX_APPENDS - len(show_appends)) // len(season_appends)
        season_subset, remaining = remaining[:batch_size], remaining[batch_size:]
        append_text = ",".join(
            [
                *show_appends,
                *(
                    f"season/{season}{append}"
                    for season in season_subset
                    for append in season_appends
                ),
            ],
        )

        params = {
            **base_params,
            **images_params,
            "append_to_response": append_text,
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        # Cache TV metadata if we haven't fetched it yet
        if fetched_tv_data is None:
            fetched_tv_data = process_tv(response)
            tv_cache_key = f"{Sources.TMDB.value}_{MediaTypes.TV.value}_{media_id}"
            cache.set(tv_cache_key, fetched_tv_data)

        # Process and cache each season
        for season_number in season_subset:
            season_key = f"season/{season_number}"
            if season_key not in response:
                msg = (
                    f"Season {season_number} not found in {Sources.TMDB.label} "
                    f"with ID {media_id}"
                )
                not_found_response = requests.Response()
                not_found_response.status_code = 404
                not_found_error = type("Error", (), {"response": not_found_response})
                raise services.ProviderAPIError(msg, error=not_found_error, details=msg)

            season_data = process_season(
                response[season_key],
                response[f"{season_key}/watch/providers"],
                response.get(f"{season_key}/aggregate_credits"),
                response.get(f"{season_key}/videos"),
            )
            season_data = enrich_season_with_tv_data(
                season_data,
                fetched_tv_data,
                media_id,
                season_number,
            )

            cache.set(
                f"{Sources.TMDB.value}_{MediaTypes.SEASON.value}_{media_id}_{season_number}",
                season_data,
            )
            result_data[season_key] = season_data

    return result_data, fetched_tv_data


def tv_with_seasons(media_id, season_numbers):
    """Return the metadata for the tv show with seasons appended to the response."""
    if not season_numbers:
        return tv(media_id)

    tv_cache_key = f"{Sources.TMDB.value}_{MediaTypes.TV.value}_{media_id}"
    tv_data = cache.get(tv_cache_key)

    cached_seasons, uncached_seasons = get_cached_seasons(media_id, season_numbers)

    if tv_data is None and not uncached_seasons:
        tv_data = tv(media_id)

    if uncached_seasons:
        fetched_seasons, fetched_tv_data = fetch_and_cache_seasons(
            media_id,
            uncached_seasons,
            tv_data,
        )

        if tv_data is None:
            tv_data = fetched_tv_data

        cached_seasons.update(fetched_seasons)

    return tv_data | cached_seasons


def tv(media_id):
    """Return the metadata for the selected tv show from The Movie Database."""
    cache_key = f"{Sources.TMDB.value}_{MediaTypes.TV.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/tv/{media_id}"
        params = {
            **base_params,
            **images_params,
            "append_to_response": TV_APPENDS,
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        data = process_tv(response)
        cache.set(cache_key, data)

    return data


def process_tv(response):
    """Process the metadata for the selected tv show from The Movie Database."""
    num_episodes = response["number_of_episodes"]
    next_episode = response.get("next_episode_to_air")
    last_episode = response.get("last_episode_to_air")
    return {
        "media_id": response["id"],
        "source": Sources.TMDB.value,
        "source_url": f"https://www.themoviedb.org/tv/{response['id']}",
        "media_type": MediaTypes.TV.value,
        "title": response["name"],
        "max_progress": num_episodes,
        "image": get_image_url(response["poster_path"]),
        "backdrop": get_backdrop_url(response.get("backdrop_path")),
        "logo": get_logo_url(response.get("images")),
        "certifications": get_tv_certifications(response.get("content_ratings")),
        **get_people(response.get("aggregate_credits"), response.get("created_by")),
        "videos": get_videos(response.get("videos")),
        "synopsis": get_synopsis(response["overview"]),
        "genres": get_genres(response["genres"]),
        "score": get_score(response["vote_average"]),
        "score_count": response["vote_count"],
        "details": {
            "format": "TV",
            "first_air_date": get_start_date(response["first_air_date"]),
            "last_air_date": response["last_air_date"],
            "status": response["status"],
            "seasons": response["number_of_seasons"],
            "episodes": num_episodes,
            "runtime": get_runtime_tv(response["episode_run_time"]),
            "studios": get_companies(response["production_companies"]),
            "country": get_country(response["production_countries"]),
            "languages": get_languages(response["spoken_languages"]),
        },
        "related": {
            "seasons": get_related(
                response["seasons"],
                MediaTypes.SEASON.value,
                response,
            ),
            "recommendations": get_related(
                response.get("recommendations", {}).get("results", []),
                MediaTypes.TV.value,
            ),
        },
        "tvdb_id": response.get("external_ids", {}).get("tvdb_id"),
        "external_links": get_external_links(response.get("external_ids", {})),
        "last_episode_season": last_episode["season_number"] if last_episode else None,
        "next_episode_season": next_episode["season_number"] if next_episode else None,
        "providers": response.get("watch/providers", {}).get("results", {}),
    }


def process_season(response, providers_response, credit_list, videos):
    """Process the metadata for the selected season from The Movie Database.

    `credit_list` and `videos` are the season's own, appended to the request.
    """
    episodes = response["episodes"]
    num_episodes = len(episodes)

    runtimes = []
    total_runtime = 0
    score_count = 0

    for episode in episodes:
        if episode["runtime"] is not None:
            runtimes.append(episode["runtime"])
            total_runtime += episode["runtime"]
        score_count += episode["vote_count"]

    avg_runtime = (
        get_readable_duration(sum(runtimes) / len(runtimes)) if runtimes else None
    )
    total_runtime = get_readable_duration(total_runtime) if total_runtime else None

    return {
        "source": Sources.TMDB.value,
        "media_type": MediaTypes.SEASON.value,
        "season_title": response["name"],
        "max_progress": episodes[-1]["episode_number"] if episodes else 0,
        "image": get_image_url(response["poster_path"]),
        "season_number": response["season_number"],
        "synopsis": get_synopsis(response["overview"]),
        "score": get_score(response["vote_average"]),
        "score_count": score_count,
        "details": {
            "first_air_date": get_start_date(response["air_date"]),
            "last_air_date": get_end_date(response),
            "episodes": num_episodes,
            "runtime": avg_runtime,
            "total_runtime": total_runtime,
        },
        "episodes": response["episodes"],
        "providers": providers_response.get("results", {}),
        **get_people(credit_list),
        "videos": get_videos(videos),
    }


def get_format(media_type):
    """Return media_type capitalized."""
    if media_type == MediaTypes.TV.value:
        return "TV"
    return "Movie"


def get_image_url(path):
    """Return the image URL for the media."""
    # when no image, value from response is null
    # e.g movie: 445290
    if path:
        return f"https://image.tmdb.org/t/p/w500{path}"
    return settings.IMG_NONE


def get_backdrop_url(path):
    """Return the URL of the wide backdrop image, or None when there is none."""
    if path:
        return f"https://image.tmdb.org/t/p/w1280{path}"
    return None


def get_logo_url(images):
    """Return the URL of the best voted title logo, or None when there is none.

    `images` is the appended images response, already limited to the user's
    language.
    """
    logos = [
        logo
        for logo in (images or {}).get("logos", [])
        if logo.get("iso_639_1") == image_language
    ]
    if not logos:
        return None
    best = max(logos, key=lambda logo: logo.get("vote_average", 0))
    return get_image_url(best["file_path"])


def get_movie_certifications(release_dates):
    """Return a movie's age rating in each country, e.g. {"US": "R"}.

    Countries rate each release, so the theatrical one is preferred.
    """
    certifications = {}
    for country in (release_dates or {}).get("results", []):
        rated = {
            release.get("type"): release["certification"]
            for release in country.get("release_dates", [])
            if release.get("certification")
        }
        for release_type in RELEASE_TYPE_ORDER:
            if release_type in rated:
                certifications[country["iso_3166_1"]] = rated[release_type]
                break
    return certifications


def get_tv_certifications(content_ratings):
    """Return a show's age rating in each country, e.g. {"US": "TV-MA"}."""
    return {
        country["iso_3166_1"]: country["rating"]
        for country in (content_ratings or {}).get("results", [])
        if country.get("rating")
    }


def get_people(credit_list, created_by=None):
    """Return the `cast` and `crew` of a movie, show or season, with their totals.

    `credit_list` is a movie's credits or a show or season's aggregate credits,
    whose people have several roles or jobs over their episodes. Shows also pass
    their creators. Each person is a dict of `id`, `name`, `role` and `image`.
    """
    credit_list = credit_list or {}
    cast = credit_list.get("cast", [])
    crew = get_crew(credit_list.get("crew", []), created_by or [])
    return {
        "cast": [
            {
                "id": member.get("id"),
                "name": member.get("name"),
                "role": get_character(member),
                "image": get_image_url(member.get("profile_path")),
            }
            for member in cast[:MAX_PEOPLE]
        ],
        "total_cast_count": len(cast),
        "crew": crew[:MAX_PEOPLE],
        "total_crew_count": len(crew),
    }


def get_character(member):
    """Return who a cast member plays, the main one first for shows."""
    if "roles" in member:
        return ", ".join(
            role["character"] for role in member["roles"][:2] if role.get("character")
        )
    return member.get("character")


def get_crew(crew, created_by):
    """Return the crew in the jobs worth showing, each person once with all of them.

    Creators come first, then people in most episodes, then the most important
    jobs; for movies that's only the jobs.
    """
    people = {}
    for member in created_by:
        people[member["id"]] = {
            "member": member,
            "jobs": ["Creator"],
            "episodes": float("inf"),
        }
    for member in crew:
        # Shows and seasons list each person's jobs, and movies one job per credit
        jobs = [job["job"] for job in member.get("jobs", [])] or [member.get("job")]
        jobs = [job for job in jobs if job in CREW_JOBS]
        if not jobs:
            continue
        person = people.setdefault(
            member["id"],
            {"member": member, "jobs": [], "episodes": 0},
        )
        person["jobs"] += [job for job in jobs if job not in person["jobs"]]
        person["episodes"] = max(
            person["episodes"],
            member.get("total_episode_count", 0),
        )

    def rank(person):
        return (-person["episodes"], min(CREW_JOBS.index(j) for j in person["jobs"]))

    return [
        {
            "id": person["member"]["id"],
            "name": person["member"].get("name"),
            "role": ", ".join(sorted(person["jobs"], key=CREW_JOBS.index)),
            "image": get_image_url(person["member"].get("profile_path")),
        }
        for person in sorted(people.values(), key=rank)
    ]


def get_videos(videos):
    """Return the YouTube videos to show, trailers first, then official, then newest.

    Each is a dict of `key`, the YouTube video ID, `name` and `type`.
    """
    results = [
        video
        for video in (videos or {}).get("results", [])
        if video.get("site") == "YouTube" and video.get("type") in VIDEO_TYPES
    ]
    results.sort(key=lambda video: video.get("published_at", ""), reverse=True)
    results.sort(
        key=lambda video: (VIDEO_TYPES.index(video["type"]), not video.get("official")),
    )
    return [
        {"key": video["key"], "name": video["name"], "type": video["type"]}
        for video in results[:MAX_VIDEOS]
    ]


def get_title(response):
    """Return the title for the media."""
    # tv shows have name instead of title
    try:
        return response["title"]
    except KeyError:
        return response["name"]


def get_release_year(response):
    """Return the release year for the media."""
    # tv shows have first_air_date instead of release_date
    # when unknown date, value from response is empty string
    date = response.get("release_date") or response.get("first_air_date")
    if not date:
        return None
    return int(date[:4])


def get_start_date(date):
    """Return the start date for the media."""
    # when unknown date, value from response is empty string
    # e.g movie: 445290
    if date == "":
        return None
    return date


def get_end_date(response):
    """Return the last air date for the season."""
    if response["episodes"]:
        return response["episodes"][-1]["air_date"]

    return None


def get_synopsis(text):
    """Return the synopsis for the media."""
    # when unknown synopsis, value from response is empty string
    # e.g movie: 445290
    if text == "":
        return "No synopsis available."
    return text


def get_readable_duration(duration):
    """Convert duration in minutes to a readable format."""
    # if unknown movie runtime, value from response is 0
    # e.g movie: 274613
    if duration:
        hours, minutes = divmod(int(duration), 60)
        return f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
    return None


def get_runtime_tv(runtime):
    """Return the runtime for the tv show."""
    # when unknown runtime, value from response is empty list
    # e.g: tv:66672
    if runtime:
        return get_readable_duration(runtime[0])
    return None


def season_scores_count(response):
    """Return the scores count for the season."""
    return sum(episode["vote_count"] for episode in response["episodes"])


def get_genres(genres):
    """Return the genres for the media."""
    # when unknown genres, value from response is empty list
    # e.g tv: 24795
    if genres:
        return [genre["name"] for genre in genres]
    return None


def get_country(countries):
    """Return the production country for the media."""
    # when unknown production country, value from response is empty list
    # e.g tv: 24795
    if countries:
        return countries[0]["name"]
    return None


def get_languages(languages):
    """Return the languages for the media."""
    # when unknown spoken languages, value from response is empty list
    # e.g tv: 24795
    if languages:
        return [language["english_name"] for language in languages]
    return None


def get_companies(companies):
    """Return the production companies for the media."""
    # when unknown production companies, value from response is empty list
    # e.g tv: 24795
    if companies:
        return [company["name"] for company in companies[:3]]
    return None


def get_score(score):
    """Return the score for the media with one decimal place."""
    # when unknown score, value from response is 0.0

    return round(score, 1)


def get_related(related_medias, media_type, parent_response=None):
    """Return list of related media for the selected media."""
    related = []
    for media in related_medias:
        poster_path = media["poster_path"]
        if media_type == MediaTypes.SEASON.value and not poster_path:
            poster_path = parent_response["poster_path"]

        data = {
            "source": Sources.TMDB.value,
            "media_type": media_type,
            "image": get_image_url(poster_path),
        }
        if media_type == MediaTypes.SEASON.value:
            data["media_id"] = parent_response["id"]
            data["title"] = parent_response["name"]
            data["season_number"] = media["season_number"]
            data["season_title"] = media["name"]
            data["first_air_date"] = get_start_date(media["air_date"])
            data["max_progress"] = media["episode_count"]
        else:
            data["media_id"] = media["id"]
            data["title"] = get_title(media)
            data["release_year"] = get_release_year(media)
        related.append(data)
    return related


def get_collection(collection_response):
    """Format media collection list to match related media."""

    def date_key(media):
        date = media.get("release_date", "")
        if date is None or date == "":
            # If release date is unknown, sort by title after known releases
            title = get_title(media)
            date = f"9999-99-99-{title}"
        return date

    parts = sorted(collection_response.get("parts", []), key=date_key)
    return [
        {
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "image": get_image_url(media["poster_path"]),
            "media_id": media["id"],
            "title": get_title(media),
            "release_year": get_release_year(media),
        }
        for media in parts
    ]


def filter_providers(all_providers, region):
    """Filter watch providers by region."""
    if region == "":
        return None

    if not all_providers:
        return []

    # Create a dict to get rid of duplicates across different provider types
    region_providers = all_providers.get(region, {})
    flatrate_providers = region_providers.get("flatrate", [])
    free_providers = region_providers.get("free", [])
    providers = {}
    for provider in [*flatrate_providers, *free_providers]:
        providers[provider.get("provider_id")] = provider

    # Convert dict back to list and add image URLs
    providers = list(providers.values())
    for provider in providers:
        provider["image"] = get_image_url(provider.get("logo_path"))

    providers.sort(key=lambda e: e.get("display_priority", 999))
    return providers


def process_episodes(season_metadata, episodes_in_db):
    """Process the episodes for the selected season, with the user's entries.

    Each episode's `history` is its watches, `watched` is whether it has
    been watched, which entries planned or being watched again also show, and
    `repeat_planned` whether it has an entry to watch it again.
    """
    episodes_metadata = []

    # Convert the queryset to a dictionary for efficient lookups
    tracked_episodes = {}
    for ep in episodes_in_db:
        episode_number = ep.item.episode_number
        if episode_number not in tracked_episodes:
            tracked_episodes[episode_number] = []
        tracked_episodes[episode_number].append(ep)

    for episode in season_metadata["episodes"]:
        episode_number = episode["episode_number"]
        entries = tracked_episodes.get(episode_number, [])

        episodes_metadata.append(
            {
                "media_id": season_metadata["media_id"],
                "media_type": MediaTypes.EPISODE.value,
                "source": Sources.TMDB.value,
                "season_number": season_metadata["season_number"],
                "episode_number": episode_number,
                "air_date": episode["air_date"],  # when unknown, response returns null
                "image": get_image_url(episode["still_path"]),
                "title": episode["name"],
                "overview": episode["overview"],
                "history": [ep for ep in entries if ep.is_watch],
                "watched": any(ep.marks_watched for ep in entries),
                "repeat_planned": any(ep.status in REPEAT_STATUSES for ep in entries),
                "runtime": get_readable_duration(episode["runtime"]),
                "runtime_minutes": episode["runtime"],
            },
        )
    return episodes_metadata


def process_episode_details(episode):
    """Return the rating, guest stars and crew of one of a season's episodes.

    `episode` is the episode as the season's response lists it.
    """
    return {
        "score": get_score(episode.get("vote_average") or 0),
        "score_count": episode.get("vote_count", 0),
        **get_people({"cast": episode.get("guest_stars"), "crew": episode.get("crew")}),
    }


def find_next_episode(episode_number, episodes_metadata):
    """Find the next episode number."""
    # Find the current episode in the sorted list
    current_episode_index = None
    for index, episode in enumerate(episodes_metadata):
        if episode["episode_number"] == episode_number:
            current_episode_index = index
            break

    # If episode not found or it's the last episode, return None
    if current_episode_index is None or current_episode_index + 1 >= len(
        episodes_metadata,
    ):
        return None

    # Return the next episode number
    return episodes_metadata[current_episode_index + 1]["episode_number"]


def episode(media_id, season_number, episode_number):
    """Return the metadata for the selected episode from The Movie Database."""
    tv_metadata = tv_with_seasons(media_id, [season_number])
    season_metadata = tv_metadata[f"season/{season_number}"]

    for episode in season_metadata["episodes"]:
        if episode["episode_number"] == int(episode_number):
            return {
                "title": season_metadata["title"],
                "season_title": season_metadata["season_title"],
                "episode_title": episode["name"],
                "image": get_image_url(episode["still_path"]),
            }

    # Episode not found - throw ProviderAPIError
    msg = (
        f"Episode {episode_number} not found in season {season_number} "
        f"for {Sources.TMDB.label} with ID {media_id}"
    )
    # Create a new response object with 404 status
    not_found_response = requests.Response()
    not_found_response.status_code = 404
    # Set the error attribute to match what ProviderAPIError expects
    not_found_error = type("Error", (), {"response": not_found_response})
    raise services.ProviderAPIError(
        Sources.TMDB.value,
        error=not_found_error,
        details=msg,
    )


def watch_provider_regions():
    """Return the available watch provider regions from The Movie Database."""
    cache_key = f"{Sources.TMDB.value}_watch_provider_regions"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/watch/providers/regions"
        params = {**base_params}

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        data = [("", "Disabled")]
        regions = response.get("results", [])
        for region in sorted(regions, key=lambda r: r.get("english_name", "")):
            key = region.get("iso_3166_1")
            name = region.get("english_name")
            if key:
                if not name:
                    name = key
                data.append((key, name))

        cache.set(cache_key, data)

    return data


def get_changed_ids(media_type):
    """Return changed TMDB ids for the given media type over the last days."""
    url = f"{base_url}/{media_type}/changes"
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=3)
    changed_ids = set()
    page = 1

    while True:
        params = {
            **base_params,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "page": page,
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        changed_ids.update(str(result["id"]) for result in response.get("results", []))

        total_pages = response.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    return changed_ids


def tv_changes():
    """Return changed TV ids from TMDB for the last days across all pages."""
    return get_changed_ids(MediaTypes.TV.value)


def movie_changes():
    """Return changed movie ids from TMDB for the last days across all pages."""
    return get_changed_ids(MediaTypes.MOVIE.value)
