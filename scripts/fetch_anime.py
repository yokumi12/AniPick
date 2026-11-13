import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "data" / "anime.json"
API_URL = "https://kitsu.io/api/edge/anime"
PAGE_SIZE = 20


def fetch_page(page):
    query = urlencode(
        {
            "page[limit]": PAGE_SIZE,
            "page[offset]": (page - 1) * PAGE_SIZE,
            "sort": "popularityRank",
            "include": "genres,categories",
            "fields[anime]": (
                "slug,canonicalTitle,titles,startDate,episodeCount,averageRating,"
                "synopsis,posterImage,ageRating,youtubeVideoId,genres,categories,"
                "mediaRelationships"
            ),
            "fields[genres]": "name",
            "fields[categories]": "title",
        }
    )
    request = Request(
        f"{API_URL}?{query}",
        headers={
            "Accept": "application/vnd.api+json",
            "User-Agent": "AnimeCapstone/0.1 (student project)",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_relationships(anime_id):
    query = urlencode(
        {
            "include": "mediaRelationships.source,mediaRelationships.destination",
            "fields[anime]": "canonicalTitle,startDate,episodeCount,mediaRelationships",
            "fields[mediaRelationships]": "role,source,destination",
        }
    )
    request = Request(
        f"{API_URL}/{anime_id}?{query}",
        headers={
            "Accept": "application/vnd.api+json",
            "User-Agent": "AnimeCapstone/0.1 (student project)",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code != 429:
            raise
        retry_after = error.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else 3
        print(f"Rate limited while fetching anime {anime_id}; waiting {wait:g}s and retrying.")
        time.sleep(wait)
        with urlopen(request, timeout=30) as response:
            return json.load(response)


def related_anime_for(anime_id, relationship_ids, included):
    related_items = []
    for relationship_ref in relationship_ids:
        relationship = included.get(("mediaRelationships", relationship_ref["id"]))
        if not relationship:
            continue
        endpoints = relationship.get("relationships", {})
        source_ref = endpoints.get("source", {}).get("data")
        destination_ref = endpoints.get("destination", {}).get("data")
        if not source_ref or not destination_ref:
            continue
        if source_ref.get("id") == anime_id:
            related_ref = destination_ref
            role = relationship.get("attributes", {}).get("role")
        elif destination_ref.get("id") == anime_id:
            related_ref = source_ref
            role = relationship.get("attributes", {}).get("role")
            if role == "sequel":
                role = "prequel"
            elif role == "prequel":
                role = "sequel"
        else:
            continue
        related_anime = included.get(("anime", related_ref.get("id")))
        if related_ref.get("type") != "anime" or not related_anime:
            continue
        related_attributes = related_anime.get("attributes", {})
        related_start_date = related_attributes.get("startDate")
        related_items.append(
            {
                "id": str(related_ref["id"]),
                "title": related_attributes.get("canonicalTitle") or related_ref["id"],
                "role": role,
                "year": (
                    int(related_start_date[:4])
                    if related_start_date and related_start_date[:4].isdigit()
                    else None
                ),
                "episodes": related_attributes.get("episodeCount"),
                "url": related_anime.get("links", {}).get("self"),
            }
        )
    related_items.sort(
        key=lambda item: (
            item["year"] is None,
            item["year"] or 0,
            item["title"].casefold(),
        )
    )
    return related_items


def normalize_anime(anime, genre_names, category_names, included):
    attributes = anime.get("attributes", {})
    genre_relationship = anime.get("relationships", {}).get("genres", {}).get("data", [])
    genres = [
        genre_names[genre["id"]]
        for genre in genre_relationship
        if genre.get("id") in genre_names and genre_names[genre["id"]]
    ]
    category_relationship = (
        anime.get("relationships", {}).get("categories", {}).get("data", [])
    )
    categories = [
        category_names[category["id"]].casefold()
        for category in category_relationship
        if category.get("id") in category_names and category_names[category["id"]]
    ]
    adult_terms = ("ecchi", "hentai", "erotic", "adult", "sexual", "nudity")
    age_rating = str(attributes.get("ageRating") or "").replace("+", "").upper()
    if not genres or age_rating == "R18" or any(
        term in genre.casefold() for genre in genres for term in adult_terms
    ) or any(term in category for category in categories for term in adult_terms):
        return None

    start_date = attributes.get("startDate")
    rating = attributes.get("averageRating")
    anime_id = str(anime["id"])
    relationship_ids = (
        anime.get("relationships", {}).get("mediaRelationships", {}).get("data", [])
    )

    return {
        "id": anime_id,
        "slug": attributes.get("slug"),
        "title": attributes.get("canonicalTitle"),
        "titles": attributes.get("titles", {}),
        "year": int(start_date[:4]) if start_date and start_date[:4].isdigit() else None,
        "episodes": attributes.get("episodeCount"),
        "score": round(float(rating) / 10, 2) if rating else None,
        "genres": genres,
        "synopsis": attributes.get("synopsis"),
        "poster_image": attributes.get("posterImage", {}).get("small"),
        "youtube_video_id": attributes.get("youtubeVideoId"),
        "related_anime": related_anime_for(anime_id, relationship_ids, included),
        "url": anime.get("links", {}).get("self"),
    }


def collect_items(page_count):
    items = []
    relationships_fetched = 0
    for page in range(1, page_count + 1):
        try:
            response = fetch_page(page)
        except HTTPError as error:
            retry_after = error.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else 3
            if error.code == 429:
                print(f"Rate limited on page {page}; waiting {wait:g}s and retrying.")
                time.sleep(wait)
                response = fetch_page(page)
            else:
                raise RuntimeError(f"Kitsu API returned HTTP {error.code} on page {page}.") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"Could not reach the Kitsu API on page {page}: {error}") from error

        genre_names = {
            genre["id"]: genre.get("attributes", {}).get("name")
            for genre in response.get("included", [])
            if genre.get("type") == "genres"
        }
        category_names = {
            category["id"]: category.get("attributes", {}).get("title")
            for category in response.get("included", [])
            if category.get("type") == "categories"
        }
        included = {
            (resource.get("type"), resource.get("id")): resource
            for resource in response.get("included", [])
        }
        normalized_items = [
            normalize_anime(anime, genre_names, category_names, included)
            for anime in response.get("data", [])
        ]
        page_items = [item for item in normalized_items if item is not None]
        for index, anime in enumerate(response.get("data", [])):
            if normalized_items[index] is None:
                continue
            relationship_ids = (
                anime.get("relationships", {})
                .get("mediaRelationships", {})
                .get("data", [])
            )
            if not relationship_ids:
                continue
            if relationships_fetched:
                time.sleep(1)
            try:
                relationship_response = fetch_relationships(anime["id"])
            except HTTPError as error:
                raise RuntimeError(
                    f"Kitsu API returned HTTP {error.code} while fetching relationships "
                    f"for anime {anime['id']}."
                ) from error
            except (URLError, TimeoutError) as error:
                raise RuntimeError(
                    f"Could not fetch Kitsu relationships for anime {anime['id']}: {error}"
                ) from error
            relationships_fetched += 1
            relationship_included = {
                (resource.get("type"), resource.get("id")): resource
                for resource in relationship_response.get("included", [])
            }
            normalized_items[index]["related_anime"] = related_anime_for(
                str(anime["id"]),
                relationship_ids,
                relationship_included,
            )
        items.extend(page_items)

        excluded_count = len(normalized_items) - len(page_items)
        print(
            f"Fetched page {page}: {len(page_items)} kept, "
            f"{excluded_count} adult/ecchi or genre-missing records excluded."
        )
        if not response.get("data") or not response.get("links", {}).get("next"):
            break
        time.sleep(1)

    return items


def save_snapshot(items):
    snapshot = {
        "source": "Kitsu API (https://kitsu.io)",
        "source_url": API_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = OUTPUT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(OUTPUT_PATH)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch a local anime catalog with genres from the Kitsu API."
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=4,
        help="Maximum number of API pages to fetch (20 records per page; default: 4).",
    )
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be at least 1.")

    items = collect_items(args.pages)
    if not items:
        raise RuntimeError("The API returned no anime records; the existing snapshot was not replaced.")
    save_snapshot(items)
    print(f"Saved {len(items)} anime records to {OUTPUT_PATH}.")


if __name__ == "__main__":
    main()
