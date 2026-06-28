"""
Blueprint API v1 : endpoints JSON pour une application mobile de
localisation de cartes postales.

Routes :
  GET  /api/v1/bounds          → zone GPS couverte par les cartes (rectangle)
  GET  /api/v1/nearby          → cartes dans un rayon autour d'une position
  GET  /api/v1/next-update     → délai recommandé avant le prochain poll
  POST /api/v1/update          → enregistre un repérage de carte sur le terrain

Authentification (endpoint POST) :
  Utilise la table ``auths`` de la base SQLite via ``model.check_auth()``.
  Les comptes sont créés avec ``model.write_auth(email, password)``.
  Le corps JSON doit contenir les champs ``email`` et ``password``.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("api_v1", __name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Rayon de la Terre en mètres (WGS-84 approx.)
_EARTH_R = 6_371_000.0

# Délai minimum entre deux polls (secondes), même en mouvement rapide
_POLL_MIN_S = 10
# Délai maximum entre deux polls quand aucune carte n'est proche
_POLL_MAX_S = 300


# ---------------------------------------------------------------------------
# Helpers géographiques
# ---------------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance en mètres entre deux points (formule de Haversine)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


def _cards_with_coord(model) -> list[dict]:
    """Retourne toutes les cartes uniques ayant des coordonnées GPS."""
    return [
        c for c in model.list_unique_cards()
        if c.get("coord") and c["coord"][0] is not None and c["coord"][1] is not None
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp.route("/api/v1/bounds")
def bounds():
    """
    Zone GPS couverte par l'ensemble des cartes postales géolocalisées.

    Retourne le rectangle englobant (bounding box) sous la forme :
      { "min_lat", "max_lat", "min_lon", "max_lon", "count" }
    """
    model = current_app.model
    cards = _cards_with_coord(model)

    if not cards:
        return jsonify({"count": 0, "bounds": None})

    lats = [c["coord"][0] for c in cards]
    lons = [c["coord"][1] for c in cards]

    return jsonify({
        "count": len(cards),
        "bounds": {
            "min_lat": min(lats),
            "max_lat": max(lats),
            "min_lon": min(lons),
            "max_lon": max(lons),
        },
    })


@bp.route("/api/v1/nearby")
def nearby():
    """
    Cartes postales dans un rayon autour d'une position GPS.

    Paramètres de requête (tous obligatoires) :
      lat    : latitude (float)
      lon    : longitude (float)
      radius : rayon de recherche en mètres (float, max 50 000)

    Retourne la liste des cartes triées par distance croissante, avec
    pour chaque carte : id, title, coord, distance_m, recto (size_div10).
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), 50_000)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon et radius sont obligatoires (float)"}), 400

    model = current_app.model
    cards = _cards_with_coord(model)

    results = []
    for card in cards:
        dist = _haversine(lat, lon, card["coord"][0], card["coord"][1])
        if dist <= radius:
            results.append({
                "id": card["id"],
                "title": card.get("title"),
                "coord": card["coord"],
                "distance_m": round(dist, 1),
                "recto": f"size_div10/{card['id']}_R.png",
            })

    results.sort(key=lambda x: x["distance_m"])

    return jsonify({"count": len(results), "cards": results})


@bp.route("/api/v1/next-update")
def next_update():
    """
    Délai recommandé (en secondes) avant le prochain appel à /api/v1/nearby.

    Paramètres de requête :
      lat    : latitude (float)
      lon    : longitude (float)
      radius : rayon de recherche en mètres (float)
      speed  : vitesse de déplacement en m/s (float, 0 = immobile)
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), 50_000)
        speed = max(float(request.args.get("speed", 0)), 0.0)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon, radius (et optionnellement speed) sont obligatoires"}), 400

    if speed <= 0:
        return jsonify({"next_update_s": _POLL_MAX_S, "reason": "immobile"})

    model = current_app.model
    cards = _cards_with_coord(model)

    min_dist_in_radius: float | None = None
    for card in cards:
        dist = _haversine(lat, lon, card["coord"][0], card["coord"][1])
        if dist <= radius:
            if min_dist_in_radius is None or dist < min_dist_in_radius:
                min_dist_in_radius = dist

    effective_distance = min_dist_in_radius if min_dist_in_radius is not None else radius
    remaining = max(radius - effective_distance, 0)
    delay = max(_POLL_MIN_S, min(remaining / speed, _POLL_MAX_S))

    return jsonify({
        "next_update_s": round(delay, 1),
        "reason": "moving",
        "speed_ms": speed,
        "radius_m": radius,
        "nearest_card_m": round(min_dist_in_radius, 1) if min_dist_in_radius is not None else None,
    })


import os

# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: Path) -> bool:
    """
    Tente d'acquérir un verrou exclusif via un fichier .lck.

    Utilise ``O_CREAT | O_EXCL`` qui est atomique sur POSIX : seul le
    processus qui crée le fichier en premier obtient le verrou.

    Attend jusqu'à ``LOCK_TIMEOUT`` secondes (config) que le fichier
    disparaisse si quelqu'un d'autre le tient, par sondages espacés de
    ``LOCK_POLL_INTERVAL`` secondes (config).
    Retourne True si le verrou est acquis, False en cas de timeout.
    """
    timeout = current_app.config.get("LOCK_TIMEOUT", 60.0)
    poll = current_app.config.get("LOCK_POLL_INTERVAL", 2.0)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)


def _release_lock(lock_path: Path) -> None:
    """Relâche le verrou en supprimant le fichier .lck."""
    try:
        lock_path.unlink()
    except OSError:
        pass


@bp.route("/api/v1/update", methods=["POST"])
def update():
    """
    Enregistre le repérage d'une carte postale sur le terrain.

    Corps JSON (Content-Type: application/json) :
      {
        "email"   : "utilisateur@example.com",
        "password": "mot de passe",
        "card_id" : "123",
        "lat"     : 46.749,
        "lon"     : 5.620
      }

    L'authentification est vérifiée via ``model.check_auth(email, password)``
    (table ``auths`` de la base SQLite, mots de passe hashés PBKDF2-SHA256).

    L'écriture dans ``updates.json`` est protégée par un lockfile
    ``updates.json.lck`` : si ce fichier existe, on attend jusqu'à 10
    secondes qu'il disparaisse avant d'écrire (protection contre les
    écritures concurrentes depuis plusieurs workers gunicorn).

    En cas de succès, enregistre le repérage dans datadir/updates.json :
      { "card_id", "email", "lat", "lon", "ts" (timestamp UNIX) }

    Codes de retour :
      200 { "status": "ok", "card_id": "...", "ts": ... }
      401 { "error": "unauthorized" }
      400 { "error": "..." }   — champ manquant ou invalide
      503 { "error": "..." }   — timeout sur le lockfile (rare)
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}

    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))

    if not current_app.model.check_auth(email, password):
        return jsonify({"error": "unauthorized"}), 401

    card_id = str(data.get("card_id", "")).strip()
    if not card_id:
        return jsonify({"error": "card_id est obligatoire"}), 400

    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "lat et lon sont obligatoires (float)"}), 400

    ts = int(time.time())
    entry = {"card_id": card_id, "email": email, "lat": lat, "lon": lon, "ts": ts}

    datadir = Path(current_app.config["DATADIR"])
    updates_path = datadir / "updates.json"
    lock_suffix = current_app.config.get("LOCK_SUFFIX", ".lck")
    lock_path = Path(str(updates_path) + lock_suffix)

    timeout = current_app.config.get("LOCK_TIMEOUT", 60.0)
    if not _acquire_lock(lock_path):
        return jsonify({
            "error": f"verrou {lock_path.name} toujours présent après {timeout:.0f}s"
        }), 503

    try:
        try:
            updates: list[dict] = json.loads(updates_path.read_text(encoding="utf-8"))
            if not isinstance(updates, list):
                updates = []
        except (OSError, json.JSONDecodeError):
            updates = []

        updates.append(entry)

        tmp_path = updates_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(updates, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(updates_path)
    finally:
        _release_lock(lock_path)

    return jsonify({"status": "ok", "card_id": card_id, "ts": ts})
