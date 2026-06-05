"""
Archaeologist — Library-Internal Discovery

Pure functions over a snapshot. No external API calls, no LLM.
We extract structure that's already inside the user's collection but hard
to see by scrolling: clusters, outliers, dormant tracks, hidden links.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable


# ==================== HELPERS ====================

def _iter_all_tracks(snapshot: dict) -> Iterable[tuple[dict, str]]:
    """Yield (track, source_label) for every track in playlists + saved."""
    for pl in snapshot.get("playlists", []):
        src = pl.get("name") or "Unknown playlist"
        for t in pl.get("tracks", []):
            yield t, src
    for t in snapshot.get("saved_tracks", []):
        yield t, "Liked Songs"


def _collect_artist_index(snapshot: dict) -> dict[str, dict]:
    """Build {artist_id: {name, genres, popularity, followers, track_count, playlist_count}}.

    Genres come from top_artists payloads (Spotify only attaches genres there, not on
    track-level artist objects). Track and playlist counts come from the user's library.
    """
    index: dict[str, dict] = {}

    # Seed names/genres from top_artists (which DO carry genres)
    for ranges in (snapshot.get("top_artists") or {}).values():
        for a in ranges or []:
            aid = a.get("id")
            if not aid:
                continue
            entry = index.setdefault(aid, {
                "id": aid,
                "name": a.get("name"),
                "genres": [],
                "popularity": None,
                "followers": None,
                "track_count": 0,
                "playlist_ids": set(),
            })
            # Take the union of genres across ranges (they're usually the same set)
            for g in a.get("genres", []) or []:
                if g not in entry["genres"]:
                    entry["genres"].append(g)
            if entry["popularity"] is None and a.get("popularity") is not None:
                entry["popularity"] = a.get("popularity")
            if entry["followers"] is None and a.get("followers") is not None:
                entry["followers"] = a.get("followers")

    # Count occurrences across playlists + saved
    for pl in snapshot.get("playlists", []):
        pid = pl.get("id")
        for t in pl.get("tracks", []):
            for aid, aname in zip(t.get("artist_ids") or [], t.get("artist_names") or []):
                if not aid:
                    continue
                entry = index.setdefault(aid, {
                    "id": aid, "name": aname, "genres": [],
                    "popularity": None, "followers": None,
                    "track_count": 0, "playlist_ids": set(),
                })
                entry["track_count"] += 1
                if pid:
                    entry["playlist_ids"].add(pid)
                if not entry.get("name"):
                    entry["name"] = aname
    for t in snapshot.get("saved_tracks", []):
        for aid, aname in zip(t.get("artist_ids") or [], t.get("artist_names") or []):
            if not aid:
                continue
            entry = index.setdefault(aid, {
                "id": aid, "name": aname, "genres": [],
                "popularity": None, "followers": None,
                "track_count": 0, "playlist_ids": set(),
            })
            entry["track_count"] += 1

    # Convert playlist_ids set → int count
    for entry in index.values():
        entry["playlist_count"] = len(entry.pop("playlist_ids", set()))
    return index


def _user_top_genres(snapshot: dict, top_n: int = 12) -> list[tuple[str, int]]:
    """Return user's top genres weighted by appearance across all top_artists ranges."""
    c: Counter = Counter()
    for ranges in (snapshot.get("top_artists") or {}).values():
        for a in ranges or []:
            for g in a.get("genres", []) or []:
                c[g] += 1
    return c.most_common(top_n)


# ==================== 1. GENRE CLUSTERS ====================

def genre_clusters(snapshot: dict, min_artists: int = 3, max_clusters: int = 12) -> list[dict]:
    """
    Group artists by their dominant Spotify genre tag.
    Each cluster: {genre, artist_count, track_count, sample_artists[6], sample_playlists[4]}
    Excludes clusters smaller than min_artists.
    """
    artists = _collect_artist_index(snapshot)

    # Map artist → primary genre = first genre Spotify lists (Spotify orders by relevance)
    by_genre: dict[str, list[dict]] = defaultdict(list)
    for a in artists.values():
        primary = (a.get("genres") or [None])[0]
        if not primary:
            continue
        by_genre[primary].append(a)

    # Compute per-playlist appearance of artists in each genre
    playlist_membership: dict[str, set[str]] = defaultdict(set)  # genre -> {playlist_name}
    for pl in snapshot.get("playlists", []):
        for t in pl.get("tracks", []):
            for aid in t.get("artist_ids") or []:
                if not aid or aid not in artists:
                    continue
                primary = (artists[aid].get("genres") or [None])[0]
                if not primary:
                    continue
                if pl.get("name"):
                    playlist_membership[primary].add(pl["name"])

    clusters: list[dict] = []
    for genre, members in by_genre.items():
        if len(members) < min_artists:
            continue
        members_sorted = sorted(members, key=lambda x: x.get("track_count", 0), reverse=True)
        track_count = sum(m.get("track_count", 0) for m in members)
        sample = [{"id": m["id"], "name": m["name"], "track_count": m["track_count"]}
                  for m in members_sorted[:6]]
        clusters.append({
            "genre": genre,
            "artist_count": len(members),
            "track_count": track_count,
            "sample_artists": sample,
            "sample_playlists": sorted(list(playlist_membership.get(genre, set())))[:4],
        })

    clusters.sort(key=lambda c: c["track_count"], reverse=True)
    return clusters[:max_clusters]


# ==================== 2. GENRE OUTLIERS ====================

def genre_outliers(snapshot: dict, top_n: int = 30) -> list[dict]:
    """
    Artists who don't fit any of your main clusters.
    Definition: their primary genre is in a cluster with fewer than 3 artists in your library
    OR they have no genre tag at all (a known Spotify gap for less-popular acts).
    """
    artists = _collect_artist_index(snapshot)
    by_genre: Counter = Counter()
    for a in artists.values():
        primary = (a.get("genres") or [None])[0]
        if primary:
            by_genre[primary] += 1

    outliers: list[dict] = []
    for a in artists.values():
        primary = (a.get("genres") or [None])[0]
        cluster_size = by_genre.get(primary, 0)
        if primary is None or cluster_size < 3:
            outliers.append({
                "id": a["id"],
                "name": a.get("name"),
                "primary_genre": primary,
                "cluster_size": cluster_size,
                "track_count": a.get("track_count", 0),
                "playlist_count": a.get("playlist_count", 0),
                "popularity": a.get("popularity"),
                "reason": "no Spotify genre tag" if not primary else f"only {cluster_size} artist(s) in your library share genre '{primary}'",
            })

    # Surface the most "established but isolated" first: high track count but small cluster
    outliers.sort(key=lambda x: (x["track_count"], x.get("popularity") or 0), reverse=True)
    return outliers[:top_n]


# ==================== 3. HIDDEN GEMS ====================

def hidden_gems(snapshot: dict, top_n: int = 50) -> list[dict]:
    """
    Tracks worth a second look. We have no per-user play counts (Spotify API doesn't expose
    them), so "play count" is proxied as "absent from top_tracks and recent_played".

    Criteria (track must satisfy 1 + 2, then ranked by 3, 4, 5):
      1. In your collection (playlist or liked)
      2. NOT in any top_tracks range AND NOT in recent plays
      3. Artist's genres overlap with your user-level top genres
      4. Bonus: high artist popularity but low track popularity (a "deep cut" by a known act)
      5. Multi-playlist appearance (signal that you cared enough to put it in multiple)
    """
    top_track_ids: set[str] = set()
    for tracks in (snapshot.get("top_tracks") or {}).values():
        for t in tracks or []:
            if t.get("id"):
                top_track_ids.add(t["id"])
    recent_ids = {t.get("id") for t in (snapshot.get("recent") or []) if t.get("id")}

    artists = _collect_artist_index(snapshot)
    top_genres = {g for g, _ in _user_top_genres(snapshot, top_n=12)}

    # Index track → playlists it appears in (for the multi-playlist signal)
    track_to_playlists: dict[str, set[str]] = defaultdict(set)
    for pl in snapshot.get("playlists", []):
        for t in pl.get("tracks", []):
            tid = t.get("id")
            if tid:
                track_to_playlists[tid].add(pl.get("name") or pl.get("id"))

    seen: set[str] = set()
    candidates: list[dict] = []
    for t, src in _iter_all_tracks(snapshot):
        tid = t.get("id")
        if not tid or tid in seen:
            continue
        if tid in top_track_ids or tid in recent_ids:
            continue
        seen.add(tid)

        # Score the track
        score = 0.0
        reasons: list[str] = []

        # Genre alignment — use primary artist's genres
        primary_artist_id = (t.get("artist_ids") or [None])[0]
        primary_artist = artists.get(primary_artist_id) if primary_artist_id else None
        artist_genres = set(primary_artist.get("genres", [])) if primary_artist else set()
        genre_match = artist_genres & top_genres
        if genre_match:
            score += 2.0 + 0.5 * len(genre_match)
            top_match = next(iter(genre_match))
            reasons.append(f"matches your top genre · {top_match}")

        # Deep cut: high artist popularity, low track popularity
        art_pop = (primary_artist or {}).get("popularity") or 0
        tr_pop = t.get("popularity") or 0
        if art_pop >= 60 and tr_pop <= 35:
            score += 2.0
            reasons.append("deep cut from a popular artist")

        # Multi-playlist
        in_playlists = track_to_playlists.get(tid, set())
        if len(in_playlists) >= 2:
            score += 1.0
            reasons.append(f"in {len(in_playlists)} of your playlists")

        # Skip totally unremarkable tracks (no signal at all)
        if score <= 0:
            continue

        candidates.append({
            "id": tid,
            "name": t.get("name"),
            "artist": ", ".join(t.get("artist_names", []) or []),
            "album": t.get("album_name"),
            "source": src,
            "reasons": reasons,
            "score": round(score, 2),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_n]


# ==================== 4. ARTIST CO-OCCURRENCE ====================

def artist_co_occurrence(snapshot: dict, min_playlists: int = 3, top_n: int = 20) -> list[dict]:
    """
    Pairs of artists that appear together in many of your playlists.
    Surfaces hidden scenes/axes in your library.
    """
    # artist_id → set of playlist IDs they appear in
    artist_playlists: dict[str, set[str]] = defaultdict(set)
    artist_names: dict[str, str] = {}
    for pl in snapshot.get("playlists", []):
        pid = pl.get("id")
        if not pid:
            continue
        playlist_artists: set[str] = set()
        for t in pl.get("tracks", []):
            for aid, aname in zip(t.get("artist_ids") or [], t.get("artist_names") or []):
                if not aid:
                    continue
                playlist_artists.add(aid)
                artist_names.setdefault(aid, aname)
        for aid in playlist_artists:
            artist_playlists[aid].add(pid)

    # Generate pair counts via nested-loop over playlists (cheaper than all-pairs)
    pair_counts: Counter = Counter()
    for pl in snapshot.get("playlists", []):
        pid = pl.get("id")
        if not pid:
            continue
        # Unique artist ids in this playlist (sorted for deterministic pairing)
        pl_artists = sorted({aid for t in pl.get("tracks", [])
                             for aid in (t.get("artist_ids") or []) if aid})
        # Only consider playlists with a reasonable number of distinct artists (avoid
        # single-artist playlists from inflating pair counts they were never part of)
        if len(pl_artists) < 2 or len(pl_artists) > 200:
            continue
        for i in range(len(pl_artists)):
            for j in range(i + 1, len(pl_artists)):
                pair_counts[(pl_artists[i], pl_artists[j])] += 1

    pairs: list[dict] = []
    for (a, b), c in pair_counts.items():
        if c < min_playlists:
            continue
        pairs.append({
            "a_id": a, "a_name": artist_names.get(a, "?"),
            "b_id": b, "b_name": artist_names.get(b, "?"),
            "shared_playlists": c,
        })
    pairs.sort(key=lambda x: x["shared_playlists"], reverse=True)
    return pairs[:top_n]
