"""
Archaeologist — Deterministic Analytics

Pure functions over a snapshot dict (see snapshot.py).
No external I/O, no LLM. Just facts the UI can render directly.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any


# ==================== HELPERS ====================

def _all_playlist_tracks(snapshot: dict) -> list[dict]:
    out = []
    for pl in snapshot.get("playlists", []):
        for t in pl.get("tracks", []):
            out.append(t)
    return out


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Spotify returns "2024-08-13T22:11:23Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


# ==================== 1. COLLECTION OVERVIEW ====================

def collection_overview(snapshot: dict) -> dict:
    """Totals + averages for the dashboard summary card."""
    playlists = snapshot.get("playlists", [])
    saved = snapshot.get("saved_tracks", [])

    pl_track_lists = [pl.get("tracks", []) for pl in playlists]
    total_pl_tracks = sum(len(t) for t in pl_track_lists)

    all_tracks = [t for tl in pl_track_lists for t in tl] + saved
    unique_artist_ids: set[str] = set()
    unique_album_ids: set[str] = set()
    for t in all_tracks:
        for aid in t.get("artist_ids", []) or []:
            if aid:
                unique_artist_ids.add(aid)
        if t.get("album_id"):
            unique_album_ids.add(t["album_id"])

    avg_playlist_size = (total_pl_tracks / len(playlists)) if playlists else 0
    longest = max(((pl.get("name"), len(pl.get("tracks", []))) for pl in playlists),
                  key=lambda x: x[1], default=(None, 0))
    shortest = min(((pl.get("name"), len(pl.get("tracks", []))) for pl in playlists if pl.get("tracks")),
                   key=lambda x: x[1], default=(None, 0))

    return {
        "total_playlists": len(playlists),
        "total_playlist_tracks": total_pl_tracks,
        "total_saved_tracks": len(saved),
        "total_saved_albums": len(snapshot.get("saved_albums", [])),
        "unique_artists": len(unique_artist_ids),
        "unique_albums": len(unique_album_ids),
        "average_playlist_size": round(avg_playlist_size, 1),
        "longest_playlist": {"name": longest[0], "tracks": longest[1]} if longest[0] else None,
        "shortest_playlist": {"name": shortest[0], "tracks": shortest[1]} if shortest[0] else None,
        "built_at": snapshot.get("built_at"),
        "truncated": snapshot.get("truncated", []),
    }


# ==================== 2. PLAYLIST OVERLAP ====================

def playlist_overlap(snapshot: dict, top_n: int = 20, min_size: int = 5) -> list[dict]:
    """
    Jaccard similarity between playlist pairs.
    Returns top_n pairs with overlap %. Only considers playlists with >= min_size tracks.
    """
    playlists = [pl for pl in snapshot.get("playlists", []) if len(pl.get("tracks", [])) >= min_size]

    # Build per-playlist track sets keyed by track id (skip local files / unknown ids)
    sets: list[tuple[str, set[str]]] = []
    for pl in playlists:
        ids = {t.get("id") for t in pl.get("tracks", []) if t.get("id")}
        if ids:
            sets.append((pl.get("name") or pl.get("id"), ids))

    pairs: list[dict] = []
    for i in range(len(sets)):
        name_i, set_i = sets[i]
        for j in range(i + 1, len(sets)):
            name_j, set_j = sets[j]
            inter = set_i & set_j
            if not inter:
                continue
            union = set_i | set_j
            score = len(inter) / len(union) if union else 0
            pairs.append({
                "a": name_i,
                "b": name_j,
                "shared_tracks": len(inter),
                "overlap": round(score, 4),
                "overlap_pct": round(score * 100, 1),
            })

    pairs.sort(key=lambda p: p["overlap"], reverse=True)
    return pairs[:top_n]


# ==================== 3. ARTIST DOMINANCE ====================

def artist_dominance(snapshot: dict, top_n: int = 50) -> list[dict]:
    """Most-occurring artists across the entire collection (playlists + saved)."""
    counter: Counter[str] = Counter()
    names: dict[str, str] = {}
    for t in _all_playlist_tracks(snapshot) + snapshot.get("saved_tracks", []):
        for aid, aname in zip(t.get("artist_ids", []) or [], t.get("artist_names", []) or []):
            if not aid:
                continue
            counter[aid] += 1
            names.setdefault(aid, aname)

    ranked = counter.most_common(top_n)
    return [{"artist_id": aid, "name": names.get(aid, "?"), "track_count": n} for aid, n in ranked]


# ==================== 4. FORGOTTEN SONGS ====================

def forgotten_songs(snapshot: dict, top_n: int = 50, months_threshold: int = 12) -> list[dict]:
    """
    Tracks added > N months ago, NOT in any top-tracks list, NOT in recent plays.
    Returned oldest first (most forgotten).
    """
    cutoff = datetime.utcnow() - timedelta(days=months_threshold * 30)

    top_ids: set[str] = set()
    for tr in snapshot.get("top_tracks", {}).values():
        for t in tr:
            if t.get("id"):
                top_ids.add(t["id"])
    recent_ids = {t.get("id") for t in snapshot.get("recent", []) if t.get("id")}

    seen: set[str] = set()
    candidates: list[dict] = []
    # Pull from playlists + saved
    pool: list[dict] = []
    for pl in snapshot.get("playlists", []):
        for t in pl.get("tracks", []):
            if t.get("id"):
                pool.append({**t, "_source": pl.get("name")})
    for t in snapshot.get("saved_tracks", []):
        if t.get("id"):
            pool.append({**t, "_source": "Liked Songs"})

    for t in pool:
        tid = t["id"]
        if tid in seen or tid in top_ids or tid in recent_ids:
            continue
        added = _parse_iso(t.get("added_at"))
        if not added or added > cutoff:
            continue
        seen.add(tid)
        candidates.append({
            "id": tid,
            "name": t.get("name"),
            "artist": ", ".join(t.get("artist_names", []) or []),
            "album": t.get("album_name"),
            "added_at": t.get("added_at"),
            "source": t.get("_source"),
        })

    candidates.sort(key=lambda c: c.get("added_at") or "")
    return candidates[:top_n]


# ==================== 5. PLAYLIST HEALTH ====================

def playlist_health(snapshot: dict) -> list[dict]:
    """
    Per-playlist scorecard:
      - duplicate_pct: how much of the playlist is repeated tracks
      - artist_diversity: unique artists / tracks (0-1)
      - overlap_score: average jaccard with every other playlist
      - score: 0-100 composite (higher = healthier)
    """
    playlists = snapshot.get("playlists", [])
    # Pre-compute id sets for cross-overlap
    id_sets: dict[str, set[str]] = {}
    for pl in playlists:
        id_sets[pl.get("id")] = {t.get("id") for t in pl.get("tracks", []) if t.get("id")}

    results: list[dict] = []
    for pl in playlists:
        tracks = pl.get("tracks", [])
        if not tracks:
            continue
        ids = [t.get("id") for t in tracks if t.get("id")]
        unique_ids = set(ids)
        duplicate_pct = round((1 - len(unique_ids) / len(ids)) * 100, 1) if ids else 0.0

        artist_ids: list[str] = []
        for t in tracks:
            for aid in t.get("artist_ids", []) or []:
                if aid:
                    artist_ids.append(aid)
        artist_diversity = round(len(set(artist_ids)) / len(artist_ids), 3) if artist_ids else 0.0

        # Average jaccard with other playlists (skip self, skip empty)
        my_set = id_sets.get(pl.get("id")) or set()
        overlaps: list[float] = []
        if my_set:
            for other_id, other_set in id_sets.items():
                if other_id == pl.get("id") or not other_set:
                    continue
                union = my_set | other_set
                if not union:
                    continue
                overlaps.append(len(my_set & other_set) / len(union))
        avg_overlap = round(sum(overlaps) / len(overlaps), 3) if overlaps else 0.0

        # Composite score: high diversity good, high duplicates bad, high overlap mildly bad
        score = (
            (1 - duplicate_pct / 100) * 40        # 0-40 — no duplicates
            + artist_diversity * 40               # 0-40 — diverse artists
            + (1 - min(avg_overlap, 0.5) / 0.5) * 20  # 0-20 — distinct from siblings
        )
        results.append({
            "id": pl.get("id"),
            "name": pl.get("name"),
            "track_count": len(tracks),
            "duplicate_pct": duplicate_pct,
            "artist_diversity": artist_diversity,
            "avg_overlap": avg_overlap,
            "score": round(score, 1),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# ==================== 6. LISTENING EVOLUTION ====================

def listening_evolution(snapshot: dict) -> dict:
    """
    Compare top artists/tracks across short_term (4w), medium_term (6mo), long_term (all-time).
    Surface rising, declining, and persistent items.
    """
    top_artists = snapshot.get("top_artists", {})
    top_tracks = snapshot.get("top_tracks", {})

    def _rank_map(items: list[dict]) -> dict[str, int]:
        return {it.get("id"): idx for idx, it in enumerate(items) if it.get("id")}

    short_a = top_artists.get("short_term", []) or []
    med_a = top_artists.get("medium_term", []) or []
    long_a = top_artists.get("long_term", []) or []
    short_t = top_tracks.get("short_term", []) or []
    med_t = top_tracks.get("medium_term", []) or []
    long_t = top_tracks.get("long_term", []) or []

    short_a_rank = _rank_map(short_a)
    long_a_rank = _rank_map(long_a)
    short_a_meta = {a.get("id"): a for a in short_a if a.get("id")}
    long_a_meta = {a.get("id"): a for a in long_a if a.get("id")}

    # Rising artists: in short_term but not long_term, or much higher rank in short_term
    rising = []
    for aid, srank in short_a_rank.items():
        lrank = long_a_rank.get(aid)
        if lrank is None:
            rising.append({
                **short_a_meta[aid],
                "short_rank": srank + 1,
                "long_rank": None,
                "movement": "new",
            })
        elif lrank - srank >= 10:
            rising.append({
                **short_a_meta[aid],
                "short_rank": srank + 1,
                "long_rank": lrank + 1,
                "movement": "up",
            })
    rising.sort(key=lambda x: (x["movement"] != "new", x.get("short_rank") or 999))

    # Declining: in long_term but not short_term
    declining = []
    for aid, lrank in long_a_rank.items():
        if aid not in short_a_rank:
            declining.append({
                **long_a_meta[aid],
                "short_rank": None,
                "long_rank": lrank + 1,
                "movement": "dropped",
            })
    declining.sort(key=lambda x: x.get("long_rank") or 999)

    # Persistent: in both
    persistent = []
    for aid in set(short_a_rank) & set(long_a_rank):
        persistent.append({
            **short_a_meta[aid],
            "short_rank": short_a_rank[aid] + 1,
            "long_rank": long_a_rank[aid] + 1,
        })
    persistent.sort(key=lambda x: x.get("short_rank") or 999)

    # Genre trends — sum popularity-weighted occurrences per genre per range
    def _genre_dist(artists: list[dict]) -> Counter:
        c: Counter = Counter()
        for a in artists:
            for g in a.get("genres", []) or []:
                c[g] += 1
        return c

    short_g = _genre_dist(short_a)
    med_g = _genre_dist(med_a)
    long_g = _genre_dist(long_a)

    # Pick genres that gained the most going short - long
    gain: list[dict] = []
    for g, sc in short_g.items():
        lc = long_g.get(g, 0)
        if sc > lc:
            gain.append({"genre": g, "short": sc, "long": lc, "delta": sc - lc})
    gain.sort(key=lambda x: x["delta"], reverse=True)

    lose: list[dict] = []
    for g, lc in long_g.items():
        sc = short_g.get(g, 0)
        if lc > sc:
            lose.append({"genre": g, "short": sc, "long": lc, "delta": lc - sc})
    lose.sort(key=lambda x: x["delta"], reverse=True)

    # Track-level returns/new entries (top 20 of each list)
    short_t_ids = {t.get("id"): t for t in short_t if t.get("id")}
    long_t_ids = {t.get("id") for t in long_t if t.get("id")}
    new_tracks = [t for tid, t in short_t_ids.items() if tid not in long_t_ids][:20]

    return {
        "rising_artists": rising[:20],
        "declining_artists": declining[:20],
        "persistent_artists": persistent[:20],
        "emerging_genres": gain[:10],
        "fading_genres": lose[:10],
        "breakout_tracks": new_tracks,
        "ranges": {
            "short_term_label": "Last 4 weeks",
            "medium_term_label": "Last 6 months",
            "long_term_label": "All time",
        },
    }
