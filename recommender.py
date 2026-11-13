"""Local content-based recommendations over synopsis text and genres."""

import math
import hashlib
import re
from threading import Lock

import database


GENRE_WEIGHT = 3
GENRE_BLEND = 0.3
AI_TAG_WEIGHT = 3
TOKEN_PATTERN = re.compile(r"[a-z]{3,}")
STOPWORDS = frozenset(
    "the and for with that this from his her their they them are was were has have had "
    "who whom whose which what when where while into onto over under about after before "
    "his its not but all can will would could should one two out only also than then "
    "there here being been more most some such very just own other each both any".split()
)

KOREAN_HINTS = {
    "힐링": (["peaceful", "daily", "friends", "calm"], ["Slice of Life"]),
    "일상": (["daily", "ordinary", "everyday"], ["Slice of Life"]),
    "가벼운": (["comedy", "fun", "lighthearted"], ["Comedy"]),
    "웃긴": (["comedy", "funny", "hilarious"], ["Comedy"]),
    "코미디": ([], ["Comedy"]),
    "전투": (["battle", "fight", "fighting", "war"], ["Action"]),
    "액션": ([], ["Action"]),
    "세계관": (["world", "kingdom", "magic", "legend"], ["Fantasy"]),
    "판타지": ([], ["Fantasy"]),
    "마법": (["magic", "wizard", "spell"], ["Magic"]),
    "모험": (["journey", "adventure", "quest"], ["Adventure"]),
    "로맨스": (["love", "romance", "relationship"], ["Romance"]),
    "연애": (["love", "romance", "relationship"], ["Romance"]),
    "학원": (["school", "student", "class"], ["School"]),
    "학교": (["school", "student", "class"], ["School"]),
    "미스터리": (["mystery", "secret", "murder"], ["Mystery"]),
    "추리": (["mystery", "detective", "murder"], ["Mystery"]),
    "심리": (["mind", "psychological", "game"], ["Psychological"]),
    "스릴러": (["death", "fear", "survival"], ["Thriller"]),
    "긴장": (["death", "survival", "fear"], ["Thriller"]),
    "공포": (["fear", "horror", "monster"], ["Horror"]),
    "sf": (["future", "space", "technology"], ["Sci-Fi"]),
    "우주": (["space", "planet", "galaxy"], ["Space", "Sci-Fi"]),
    "로봇": (["robot", "mecha", "pilot"], ["Mecha"]),
    "스포츠": (["team", "tournament", "match"], ["Sports"]),
    "음악": (["band", "music", "song"], ["Music"]),
    "초능력": (["power", "powers", "ability"], ["Super Power"]),
    "능력": (["power", "powers", "ability"], ["Super Power"]),
    "군대": (["military", "soldier", "war"], ["Military"]),
    "우정": (["friends", "friendship", "bond"], ["Friendship"]),
    "가족": (["family", "mother", "father"], ["Family"]),
    "눈물": (["tragic", "loss", "sacrifice"], ["Drama"]),
    "감동": (["tragic", "loss", "bond"], ["Drama"]),
    "드라마": ([], ["Drama"]),
    "초자연": (["spirit", "ghost", "demon"], ["Supernatural"]),
    "귀신": (["spirit", "ghost", "demon"], ["Supernatural"]),
    "복수": (["revenge", "kill", "vengeance"], ["Action"]),
}

_lock = Lock()
_index = None


def _tokenize(text):
    tokens = []
    for word in TOKEN_PATTERN.findall((text or "").lower()):
        if word in STOPWORDS:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        tokens.append(word)
    return tokens


def _genre_token(genre):
    return "genre_" + re.sub(r"[^a-z]", "", genre.lower())


def _ai_tag_token(tag):
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:16]
    return "ai_tag_" + digest


def _ai_tag_names(item):
    names = []
    for tag in item.get("ai_tags", []):
        name = tag.get("name") if isinstance(tag, dict) else tag
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _norm(vector):
    return math.sqrt(sum(value * value for value in vector.values())) or 1.0


def _cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    dot = sum(value * b.get(term, 0.0) for term, value in a.items())
    return dot / (_norm(a) * _norm(b))


def _build(catalog):
    documents = {}
    items = {}
    for item in catalog:
        anime_id = str(item["id"])
        tokens = _tokenize(item.get("synopsis"))
        for genre in item.get("genres", []):
            tokens.extend([_genre_token(genre)] * GENRE_WEIGHT)
        for name in _ai_tag_names(item):
            tokens.extend([_ai_tag_token(name)] * AI_TAG_WEIGHT)
        documents[anime_id] = tokens
        items[anime_id] = item

    doc_count = max(len(documents), 1)
    frequency = {}
    for tokens in documents.values():
        for term in set(tokens):
            frequency[term] = frequency.get(term, 0) + 1
    idf = {
        term: math.log((1 + doc_count) / (1 + count)) + 1
        for term, count in frequency.items()
    }

    vectors = {}
    for anime_id, tokens in documents.items():
        counts = {}
        for term in tokens:
            counts[term] = counts.get(term, 0) + 1
        vectors[anime_id] = {
            term: (1 + math.log(count)) * idf[term] for term, count in counts.items()
        }
    return {"items": items, "vectors": vectors, "idf": idf}


def get_index(force=False):
    global _index
    with _lock:
        if _index is None or force:
            _index = _build(database.get_catalog())
        return _index


def _with_particle(word):
    last = word[-1:] if word else ""
    has_batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return f"‘{word}’{'과' if has_batchim else '와'}"


def _title(item):
    return item.get("title_ko") or item.get("title") or ""


def similar(anime_id, limit=6):
    index = get_index()
    anime_id = str(anime_id)
    base = index["vectors"].get(anime_id)
    if base is None:
        return None
    base_genres = set(index["items"][anime_id].get("genres", []))
    scored = []
    for other_id, vector in index["vectors"].items():
        if other_id == anime_id:
            continue
        shared = [
            genre
            for genre in index["items"][other_id].get("genres", [])
            if genre in base_genres
        ]
        base_tags = set(_ai_tag_names(index["items"][anime_id]))
        shared_tags = [
            tag for tag in _ai_tag_names(index["items"][other_id]) if tag in base_tags
        ]
        scored.append(
            {
                "id": other_id,
                "score": round(_cosine(base, vector), 4),
                "shared_genres": shared,
                "shared_tags": shared_tags,
            }
        )
    scored.sort(key=lambda row: row["score"], reverse=True)
    return scored[:limit]


def genre_weights(favorite_ids):
    index = get_index()
    counts = {}
    for anime_id in favorite_ids:
        item = index["items"].get(str(anime_id))
        for genre in (item or {}).get("genres", []):
            counts[genre] = counts.get(genre, 0) + 1
    top = max(counts.values()) if counts else 0
    return {genre: round(count / top, 4) for genre, count in counts.items()} if top else {}


def recommend(favorite_ids, preferred_genres=(), limit=20):
    """Rank un-favorited catalog entries by synopsis and genre similarity."""
    index = get_index()
    favorites = [str(fid) for fid in favorite_ids if str(fid) in index["vectors"]]
    preferred = set(preferred_genres)
    if not favorites and not preferred:
        return []

    taste = {}
    if favorites:
        for favorite_id in favorites:
            for term, value in index["vectors"][favorite_id].items():
                taste[term] = taste.get(term, 0.0) + value / len(favorites)

    favorite_set = set(favorites)
    results = []
    for anime_id, vector in index["vectors"].items():
        if anime_id in favorite_set:
            continue
        item = index["items"][anime_id]
        similarity_score = _cosine(taste, vector) if taste else 0.0
        item_genres = set(item.get("genres", []))
        overlap = (
            len(preferred & item_genres) / len(preferred)
            if preferred
            else 0.0
        )
        if favorites and preferred:
            score = (1 - GENRE_BLEND) * similarity_score + GENRE_BLEND * overlap
        else:
            score = similarity_score if favorites else overlap

        nearest = max(
            favorites,
            key=lambda favorite_id: _cosine(index["vectors"][favorite_id], vector),
            default=None,
        )
        reason = ""
        if nearest and _cosine(index["vectors"][nearest], vector) > 0:
            reason = (
                f"찜한 {_with_particle(_title(index['items'][nearest]))} 줄거리가 비슷해요"
            )
        matched = sorted(preferred & item_genres)
        results.append(
            {
                "id": anime_id,
                "score": round(score, 4),
                "reason": reason,
                "matched_genres": matched,
            }
        )
    results.sort(key=lambda row: row["score"], reverse=True)
    return results[:limit]


def search(query, limit=12):
    """Search title and synopsis using English terms and Korean topic hints."""
    index = get_index()
    query = (query or "").strip().lower()[:100]
    if not query:
        return []

    terms = list(_tokenize(query))
    wanted_genres = set()
    matched_hints = []
    for hint, (words, genres) in KOREAN_HINTS.items():
        if hint in query:
            matched_hints.append(hint)
            for word in words:
                terms.extend(_tokenize(word))
            wanted_genres.update(genres)
    for genre in wanted_genres:
        terms.extend([_genre_token(genre)] * GENRE_WEIGHT)

    counts = {}
    for term in terms:
        counts[term] = counts.get(term, 0) + 1
    query_vector = {
        term: (1 + math.log(count)) * index["idf"][term]
        for term, count in counts.items()
        if term in index["idf"]
    }

    results = []
    for anime_id, vector in index["vectors"].items():
        item = index["items"][anime_id]
        score = _cosine(query_vector, vector) if query_vector else 0.0
        names = [
            _title(item),
            item.get("title") or "",
            *(item.get("titles") or {}).values(),
        ]
        if any(query in name.lower() for name in names if isinstance(name, str)):
            score += 1.0
        matched_tags = [
            tag for tag in _ai_tag_names(item) if tag.lower() in query
        ]
        score += len(matched_tags)
        if score <= 0:
            continue
        shared = sorted(wanted_genres & set(item.get("genres", [])))
        reason = (
            f"AI 추출 태그 ‘{', '.join(matched_tags)}’와 관련 있는 작품이에요"
            if matched_tags
            else f"‘{', '.join(matched_hints)}’ 검색어와 관련 있는 작품이에요"
            if matched_hints
            else "검색어와 줄거리가 비슷해요"
        )
        results.append(
            {
                "id": anime_id,
                "score": round(score, 4),
                "reason": reason,
                "matched_genres": shared,
                "matched_tags": matched_tags,
            }
        )
    results.sort(key=lambda row: row["score"], reverse=True)
    return results[:limit]
