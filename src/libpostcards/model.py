"""
libpostcards/model.py - Accès centralisé aux données : JSON, SQLite

Gère les cartes postales (cards) et les trajets (travels).
Pas de dépendance externe hormis la bibliothèque standard.

Recommandation pour votre script de publication :

bash# Bon : écrire la nouvelle base à côté, puis remplacement atomique
cp nouvelle_base.sqlite datadir/postcards.sqlite.tmp
mv datadir/postcards.sqlite.tmp datadir/postcards.sqlite

# À éviter : écraser directement le fichier en place
cp nouvelle_base.sqlite datadir/postcards.sqlite

mv / os.replace (remplacement atomique du fichier, change l'inode) → fonctionne de manière fiable avec ce mécanisme, même si gunicorn a une connexion active en cours.

cp en place (écrasement du contenu d'un fichier déjà ouvert par une connexion WAL active) → reste risqué indépendamment de mon code, car SQLite en mode WAL associe son fichier -shm à l'état du fichier au moment de l'ouverture ; écraser le contenu en place pendant qu'une connexion le tient ouvert peut produire des lectures incohérentes, peu importe la détection de changement côté applicatif.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth hashing (PBKDF2-HMAC-SHA256)
# ---------------------------------------------------------------------------
# Stored format: "pbkdf2$<iterations>$<hex-salt>$<hex-digest>"
# An empty password always fails verification.

_HASH_ALGO       = "sha256"
_HASH_ITERATIONS = 260_000   # OWASP 2023 recommendation for PBKDF2-SHA256
_SALT_BYTES      = 32


def _hash_password(password: str) -> str:
    """Return a salted PBKDF2 hash of *password* suitable for storage."""
    salt   = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        _HASH_ALGO, password.encode("utf-8"), salt, _HASH_ITERATIONS
    )
    return f"pbkdf2${_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Return True if *password* matches the stored PBKDF2 hash.

    Returns False immediately if *password* is empty, if *stored* is
    None/empty, or if the format is unrecognised.
    """
    if not password or not stored:
        return False
    try:
        scheme, iterations_s, salt_hex, digest_hex = stored.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    try:
        iterations = int(iterations_s)
        salt       = bytes.fromhex(salt_hex)
        expected   = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        _HASH_ALGO, password.encode("utf-8"), salt, iterations
    )
    return secrets.compare_digest(candidate, expected)

def _strip_accents(value: str | None) -> str | None:
    """Remove diacritics (accents) from a string and lowercase it.

    Used both to normalize values stored in SQLite (via a custom SQL
    function) and to normalize search terms, so that searches are
    accent-insensitive: "dodanes", "dôdanes" and "dodânes" all match
    each other.
    """
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        ch for ch in normalized if not unicodedata.combining(ch)
    )
    return without_accents.lower()

# ---------------------------------------------------------------------------
# Schéma SQL
# ---------------------------------------------------------------------------

_DDL_CARDS = """
CREATE TABLE IF NOT EXISTS cards (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    title2      TEXT,
    description TEXT,
    recto_ocr   TEXT,
    verso_ocr   TEXT,
    date        TEXT,
    cdate       INTEGER,
    mdate       INTEGER,
    address     TEXT,       -- JSON array sérialisé
    recto_text  TEXT,
    verso_text  TEXT,
    coord_lat   REAL,
    coord_lon   REAL,
    poi         TEXT,       -- JSON array sérialisé
    collections TEXT,       -- JSON array sérialisé
    doubles     TEXT        -- JSON array sérialisé
);
"""

_DDL_TRAVELS = """
CREATE TABLE IF NOT EXISTS travels (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    title2      TEXT,
    distance_m  INTEGER,
    distance_km REAL,
    start_lat   REAL,
    start_lon   REAL,
    end_lat     REAL,
    end_lon     REAL,
    count       INTEGER,
    cards       TEXT        -- JSON array sérialisé [{id, title}, ...]
);
"""

_DDL_POIS = """
CREATE TABLE IF NOT EXISTS pois (
    id          TEXT PRIMARY KEY,
    description TEXT,
    coord_lat   REAL,
    coord_lon   REAL
);
"""

_DDL_AUTHS = """
CREATE TABLE IF NOT EXISTS auths (
    email   TEXT PRIMARY KEY,
    auth    TEXT
);
"""

_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_cards_cdate ON cards (cdate);
CREATE INDEX IF NOT EXISTS idx_cards_mdate ON cards (mdate);
"""

# Valeurs par défaut pour une carte vide
_CARD_DEFAULTS: dict[str, Any] = {
    "id": None,
    "title": None,
    "title2": None,
    "description": None,
    "recto_ocr": None,
    "verso_ocr": None,
    "date": None,
    "cdate": None,
    "mdate": None,
    "address": [],
    "recto_text": None,
    "verso_text": None,
    "coord": None,
    "poi": [],
    "collections": [],
    "doubles": [],
}


# ---------------------------------------------------------------------------
# Helpers de (dé)sérialisation
# ---------------------------------------------------------------------------

def _card_to_row(card: dict) -> dict:
    """Convertit un dict carte en ligne SQL (champs plats)."""
    coord = card.get("coord") or []
    return {
        "id": str(card["id"]),
        "title": card.get("title"),
        "title2": card.get("title2"),
        "description": card.get("description"),
        "recto_ocr": card.get("recto_ocr"),
        "verso_ocr": card.get("verso_ocr"),
        "date": card.get("date"),
        "cdate": card.get("cdate"),
        "mdate": card.get("mdate"),
        "address": json.dumps(card.get("address") or [], ensure_ascii=False),
        "recto_text": card.get("recto_text"),
        "verso_text": card.get("verso_text"),
        "coord_lat": coord[0] if len(coord) > 0 else None,
        "coord_lon": coord[1] if len(coord) > 1 else None,
        "poi": json.dumps(card.get("poi") or [], ensure_ascii=False),
        "collections": json.dumps(card.get("collections") or [], ensure_ascii=False),
        "doubles": json.dumps(card.get("doubles") or [], ensure_ascii=False),
    }


def _row_to_card(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict carte."""
    d = dict(row)
    lat, lon = d.pop("coord_lat", None), d.pop("coord_lon", None)
    d["coord"] = [lat, lon] if (lat is not None and lon is not None) else None
    for field in ("address", "poi", "collections", "doubles"):
        raw = d.get(field)
        d[field] = json.loads(raw) if raw else []
    return d


def _travel_to_row(travel: dict) -> dict:
    """Convertit un dict trajet en ligne SQL (champs plats)."""
    start = travel.get("start") or []
    end = travel.get("end") or []
    return {
        "id": str(travel["id"]),
        "title": travel.get("title"),
        "title2": travel.get("title2"),
        "distance_m": travel.get("distance_m"),
        "distance_km": travel.get("distance_km"),
        "start_lat": start[0] if len(start) > 0 else None,
        "start_lon": start[1] if len(start) > 1 else None,
        "end_lat": end[0] if len(end) > 0 else None,
        "end_lon": end[1] if len(end) > 1 else None,
        "count": travel.get("count"),
        "cards": json.dumps(travel.get("cards") or [], ensure_ascii=False),
    }


def _row_to_travel(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict trajet."""
    d = dict(row)
    d["start"] = [d.pop("start_lat"), d.pop("start_lon")]
    d["end"] = [d.pop("end_lat"), d.pop("end_lon")]
    raw_cards = d.get("cards")
    d["cards"] = json.loads(raw_cards) if raw_cards else []
    return d


def _poi_to_row(poi: dict) -> dict:
    """Convertit un dict POI en ligne SQL (champs plats)."""
    coord = poi.get("coord") or []
    return {
        "id": str(poi["id"]),
        "description": poi.get("description"),
        "coord_lat": coord[0] if len(coord) > 0 else None,
        "coord_lon": coord[1] if len(coord) > 1 else None,
    }


def _row_to_poi(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict POI."""
    d = dict(row)
    lat, lon = d.pop("coord_lat", None), d.pop("coord_lon", None)
    d["coord"] = [lat, lon] if (lat is not None and lon is not None) else None
    return d


# ---------------------------------------------------------------------------
# Classe Model
# ---------------------------------------------------------------------------

class Model:
    """
    Accès centralisé aux données cartes postales.

    Paramètres
    ----------
    datadir : str | Path
        Répertoire racine des données (contient cards/ et postcards.sqlite).
    """

    def __init__(self, datadir: str | Path = "data") -> None:
        self.datadir = Path(datadir)
        self.cards_dir = self.datadir / "cards"
        self.db_path = self.datadir / "postcards.sqlite"
        self.pois_json    = self.datadir / "pois.json"
        self.updates_json = self.datadir / "updates.json"
        self._conn: sqlite3.Connection | None = None
        # Signature (mtime, inode) du fichier sqlite au moment de
        # l'ouverture de la connexion ; permet de détecter un
        # remplacement du fichier (publication d'une nouvelle base)
        # et de rouvrir automatiquement la connexion, sans nécessiter
        # de redémarrer le processus (utile avec gunicorn).
        self._db_signature: tuple[float, int] | None = None

    # ------------------------------------------------------------------
    # Connexion SQLite
    # ------------------------------------------------------------------

    def _current_db_signature(self) -> tuple[float, int] | None:
        """(mtime, inode) du fichier sqlite sur disque, ou None s'il est absent."""
        try:
            stat = self.db_path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_ino)

    def _get_conn(self) -> sqlite3.Connection:
        """
        Retourne (et ouvre si nécessaire) la connexion SQLite.

        Si le fichier sqlite a été remplacé depuis la dernière ouverture
        (mtime ou inode différent, par exemple après publication d'une
        nouvelle base de données), la connexion existante est fermée et
        une nouvelle est ouverte automatiquement.
        """
        current_signature = self._current_db_signature()

        if self._conn is not None and current_signature != self._db_signature:
            logger.info(
                "Changement détecté sur %s, réouverture de la connexion",
                self.db_path,
            )
            self.close()
            # En mode WAL, des fichiers -wal/-shm résiduels de l'ancienne
            # base peuvent subsister si seul le fichier .sqlite principal
            # a été remplacé (ex: publication via cp/mv). S'ils ne sont
            # pas supprimés, la nouvelle connexion risque de lire des
            # pages obsolètes issues de l'ancienne base.
            for suffix in ("-wal", "-shm"):
                stale_path = Path(str(self.db_path) + suffix)
                if stale_path.exists():
                    try:
                        stale_path.unlink()
                    except OSError:
                        logger.warning(
                            "Impossible de supprimer le fichier résiduel %s",
                            stale_path,
                        )

        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=10,
            )
            self._conn.row_factory = sqlite3.Row
            # Performance : WAL mode + foreign keys
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._db_signature = self._current_db_signature()

            # Fonction SQL personnalisée pour les recherches insensibles
            # aux accents et à la casse (ex: "dodanes", "dôdanes" et
            # "dodânes" doivent toutes se retrouver mutuellement).
            try:
                self._conn.create_function(
                    "unaccent_lower", 1, _strip_accents, deterministic=True
                )
            except sqlite3.NotSupportedError:
                # SQLite build too old to support the `deterministic` flag
                self._conn.create_function("unaccent_lower", 1, _strip_accents)

        return self._conn

    def close(self) -> None:
        """Ferme la connexion SQLite."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._db_signature = None

    def __enter__(self) -> "Model":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # JSON cards
    # ------------------------------------------------------------------

    def _json_path(self, card_id: str | int) -> Path:
        return self.cards_dir / f"{card_id}.json"

    def load_json(self, card_id: str | int) -> dict:
        """
        Lit le JSON d'une carte depuis cards/.

        Si l'id n'existe pas, retourne un dict avec tous les champs
        initialisés à leur valeur par défaut et l'id fourni.
        """
        path = self._json_path(card_id)
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        # Carte inconnue : retourne un squelette avec l'id
        skeleton = dict(_CARD_DEFAULTS)
        skeleton["id"] = str(card_id)
        now = int(time.time())
        skeleton["cdate"] = now
        skeleton["mdate"] = now
        return skeleton

    def write_json(self, card: dict) -> None:
        """
        Écrit le JSON d'une carte dans cards/ et met à jour la base SQLite.

        Le champ ``mdate`` est automatiquement rafraîchi.

        Si le champ ``doubles`` contient de nouveaux ids par rapport à
        la version précédente, la réciprocité est assurée : pour chaque
        nouvel id ``id2`` ajouté dans ``doubles`` de la carte ``id1``,
        la carte ``id2`` est mise à jour pour inclure ``id1`` dans son
        propre champ ``doubles`` (si ce n'est pas déjà le cas).
        """
        card = dict(card)  # copie défensive
        card_id = str(card["id"])

        # Détermine les nouveaux doublons ajoutés par rapport à l'existant
        old_card = self.load_json(card_id)
        old_doubles = {str(d) for d in (old_card.get("doubles") or [])}
        new_doubles = {str(d) for d in (card.get("doubles") or [])}
        added_doubles = new_doubles - old_doubles

        card["doubles"] = sorted(new_doubles)
        card["mdate"] = int(time.time())

        self.cards_dir.mkdir(parents=True, exist_ok=True)
        path = self._json_path(card_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(card, fh, ensure_ascii=False, indent=2)

        self._upsert_card(card)

        # Crée automatiquement les POIs référencés qui n'existent pas encore
        for poi_id in {str(p) for p in (card.get("poi") or [])}:
            self._ensure_poi(poi_id)

        # Assure la réciprocité pour les nouveaux doublons
        for other_id in added_doubles:
            if other_id == card_id:
                continue
            self._add_double(other_id, card_id)

    def _add_double(self, card_id: str, double_id: str) -> None:
        """
        Ajoute ``double_id`` au champ ``doubles`` de la carte ``card_id``
        (JSON + base), si ce n'est pas déjà présent.
        """
        other = self.load_json(card_id)
        other_doubles = {str(d) for d in (other.get("doubles") or [])}
        if double_id in other_doubles:
            return

        other_doubles.add(double_id)
        other["doubles"] = sorted(other_doubles)
        other["mdate"] = int(time.time())

        self.cards_dir.mkdir(parents=True, exist_ok=True)
        path = self._json_path(card_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(other, fh, ensure_ascii=False, indent=2)

        self._upsert_card(other)
        logger.info(
            "Réciprocité doublons : ajout de %s dans doubles de %s",
            double_id, card_id,
        )

    def _upsert_card(self, card: dict) -> None:
        """INSERT OR REPLACE d'une carte dans la base."""
        row = _card_to_row(card)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        sql = f"INSERT OR REPLACE INTO cards ({cols}) VALUES ({placeholders})"
        conn = self._get_conn()
        conn.execute(sql, row)
        conn.commit()

    # ------------------------------------------------------------------
    # Travels
    # ------------------------------------------------------------------

    def read_travel(self, travel_id: str) -> dict | None:
        """
        Lit un trajet depuis la base SQLite.

        Retourne None si l'id n'existe pas.
        """
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM travels WHERE id = ?", (travel_id,))
        row = cur.fetchone()
        return _row_to_travel(row) if row else None

    def list_travels(self) -> list[dict]:
        """Retourne la liste de tous les trajets."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM travels ORDER BY id")
        return [_row_to_travel(r) for r in cur.fetchall()]

    def write_travel(self, travel: dict) -> None:
        """
        Écrit (INSERT OR REPLACE) un trajet dans la base SQLite.
        """
        row = _travel_to_row(travel)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        sql = f"INSERT OR REPLACE INTO travels ({cols}) VALUES ({placeholders})"
        conn = self._get_conn()
        conn.execute(sql, row)
        conn.commit()

    def delete_travel(self, travel_id: str) -> None:
        """Supprime un trajet de la base."""
        conn = self._get_conn()
        conn.execute("DELETE FROM travels WHERE id = ?", (travel_id,))
        conn.commit()

    # ------------------------------------------------------------------
    # Synchronisation JSON → SQLite
    # ------------------------------------------------------------------

    def sync(self) -> int:
        """
        Lit les JSON présents dans cards/ et met à jour la base SQLite
        uniquement pour les cartes dont le mdate est plus récent que
        celui stocké en base.

        Retourne le nombre de cartes mises à jour.
        """
        if not self.cards_dir.exists():
            logger.warning("cards_dir introuvable : %s", self.cards_dir)
            return 0

        conn = self._get_conn()
        updated = 0

        for json_path in sorted(self.cards_dir.glob("*.json")):
            card_id = json_path.stem
            with json_path.open(encoding="utf-8") as fh:
                card = json.load(fh)

            # Vérifie si la carte est déjà à jour en base
            cur = conn.execute(
                "SELECT mdate FROM cards WHERE id = ?", (str(card_id),)
            )
            row = cur.fetchone()
            file_mdate = card.get("mdate") or 0
            db_mdate = row["mdate"] if row else -1

            if file_mdate > db_mdate:
                self._upsert_card(card)
                for poi_id in {str(p) for p in (card.get("poi") or [])}:
                    self._ensure_poi(poi_id)
                updated += 1
                logger.debug("sync : carte %s mise à jour", card_id)

        logger.info("sync : %d carte(s) mise(s) à jour", updated)
        return updated

    # ------------------------------------------------------------------
    # Génération de la base
    # ------------------------------------------------------------------

    def generate(self) -> int:
        """
        Crée une base SQLite vierge (écrase l'existante si présente)
        puis importe tous les JSON depuis cards/.

        Retourne le nombre de cartes importées.
        """
        # Supprime la base existante
        if self.db_path.exists():
            self.db_path.unlink()
            # Remet la connexion à zéro
            self.close()

        self.datadir.mkdir(parents=True, exist_ok=True)

        conn = self._get_conn()
        conn.executescript(_DDL_CARDS + _DDL_TRAVELS + _DDL_POIS + _DDL_AUTHS + _DDL_INDEXES)
        conn.commit()
        logger.info("Base créée : %s", self.db_path)

        # Import POIs from pois.json first (before cards, so _ensure_poi
        # can skip ids that are already fully described in pois.json)
        self.sync_pois()

        if not self.cards_dir.exists():
            logger.warning("cards_dir introuvable : %s", self.cards_dir)
            return 0

        count = 0
        for json_path in sorted(self.cards_dir.glob("*.json")):
            with json_path.open(encoding="utf-8") as fh:
                card = json.load(fh)
            self._upsert_card(card)
            for poi_id in {str(p) for p in (card.get("poi") or [])}:
                self._ensure_poi(poi_id)
            count += 1

        logger.info("generate : %d carte(s) importée(s)", count)
        return count

    # ------------------------------------------------------------------
    # Lecture de cartes depuis la base
    # ------------------------------------------------------------------

    def get_card(self, card_id: str | int) -> dict | None:
        """Retourne une carte depuis la base SQLite, ou None si absente."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM cards WHERE id = ?", (str(card_id),))
        row = cur.fetchone()
        return _row_to_card(row) if row else None

    def list_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """
        Liste les cartes avec filtres optionnels.

        Paramètres
        ----------
        collection : str | None
            Filtre sur la collection (recherche dans le champ JSON ``collections``).
        search : str | None
            Recherche textuelle dans title, title2, description,
            verso_text, recto_text, address.
            Recherche textuelle (insensible aux accents et à la casse)
            dans title, title2, description, verso_text, recto_text,
            address, poi.
        limit : int | None
            Nombre maximum de résultats.
        offset : int
            Décalage pour la pagination.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if collection:
            # SQLite : json_each pour chercher dans le tableau JSON
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        if search:
            like = f"%{search}%"
            conditions.append(
                "(unaccent_lower(title) LIKE unaccent_lower(?)"
                " OR unaccent_lower(title2) LIKE unaccent_lower(?)"
                " OR unaccent_lower(description) LIKE unaccent_lower(?)"
                " OR unaccent_lower(verso_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(recto_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(address) LIKE unaccent_lower(?)"
                " OR unaccent_lower(poi) LIKE unaccent_lower(?))"
            )
            params.extend([like] * 7)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        offset_clause = f"OFFSET {int(offset)}" if offset else ""

        sql = f"SELECT * FROM cards {where} ORDER BY CAST(id AS INTEGER) {limit_clause} {offset_clause}"
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return [_row_to_card(r) for r in cur.fetchall()]

    def count_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
    ) -> int:
        """Retourne le nombre de cartes (avec les mêmes filtres que list_cards)."""
        conditions: list[str] = []
        params: list[Any] = []

        if collection:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        if search:
            like = f"%{search}%"
            conditions.append(
                "(unaccent_lower(title) LIKE unaccent_lower(?)"
                " OR unaccent_lower(title2) LIKE unaccent_lower(?)"
                " OR unaccent_lower(description) LIKE unaccent_lower(?)"
                " OR unaccent_lower(verso_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(recto_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(address) LIKE unaccent_lower(?)"
                " OR unaccent_lower(poi) LIKE unaccent_lower(?))"
            )
            params.extend([like] * 7)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT COUNT(*) FROM cards {where}"
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return cur.fetchone()[0]

    def next_id(self) -> int:
        """
        Détermine le prochain id disponible en inspectant cards/.
        Retourne max(ids existants) + 1, ou 1 si cards/ est vide.
        """
        if not self.cards_dir.exists():
            return 1
        ids = []
        for p in self.cards_dir.glob("*.json"):
            try:
                ids.append(int(p.stem))
            except ValueError:
                pass
        return max(ids) + 1 if ids else 1

    # ------------------------------------------------------------------
    # Cartes uniques (exclusion des doublons)
    # ------------------------------------------------------------------

    # Condition SQL qui sélectionne, pour chaque groupe de doublons,
    # la carte la plus pertinente à afficher :
    #
    #  (A) La carte a des coordonnées GPS ET aucune autre carte du groupe
    #      n'a de GPS avec un id numérique inférieur (départage quand
    #      plusieurs membres ont des GPS).
    #
    #  (B) La carte n'a pas de coordonnées GPS ET aucun membre du groupe
    #      n'en a non plus ET elle n'est pas référencée comme doublon par
    #      une autre carte (comportement historique : carte "principale").
    #
    # "Groupe" = la carte elle-même + les cartes qui la référencent dans
    # leur champ `doubles` + les cartes qu'elle référence dans le sien.
    #
    # Note : l'alias utilisé pour la carte courante doit être `cards`
    # (pas de sous-alias) car cette condition est injectée dans un WHERE
    # sur la table principale.
    _UNIQUE_CARD_CONDITION = (
        # Parenthèses extérieures OBLIGATOIRES : cette condition contient un OR
        # interne (cas A OR cas B). Sans elles, un AND ajouté par
        # " AND ".join(conditions) serait prioritaire sur le OR interne et
        # rendrait les filtres search/collection inopérants pour le cas A
        # (toutes les cartes avec GPS passeraient quel que soit le filtre).
        "("
        # (A) carte avec GPS, préférée dans son groupe
        "  ("
        "    cards.coord_lat IS NOT NULL"
        "    AND NOT EXISTS ("
        "      SELECT 1 FROM cards AS cg"
        "      WHERE cg.coord_lat IS NOT NULL"
        "        AND CAST(cg.id AS INTEGER) < CAST(cards.id AS INTEGER)"
        "        AND ("
        "          EXISTS ("
        "            SELECT 1 FROM json_each(cards.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cg.id"
        "          )"
        "          OR EXISTS ("
        "            SELECT 1 FROM json_each(cg.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cards.id"
        "          )"
        "        )"
        "    )"
        "  )"
        "  OR"
        # (B) carte sans GPS, aucun membre du groupe n'a de GPS,
        #     et elle n'est pas référencée comme doublon par une autre
        "  ("
        "    cards.coord_lat IS NULL"
        "    AND NOT EXISTS ("
        "      SELECT 1 FROM cards AS cg"
        "      WHERE cg.coord_lat IS NOT NULL"
        "        AND ("
        "          EXISTS ("
        "            SELECT 1 FROM json_each(cards.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cg.id"
        "          )"
        "          OR EXISTS ("
        "            SELECT 1 FROM json_each(cg.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cards.id"
        "          )"
        "        )"
        "    )"
        "    AND NOT EXISTS ("
        "      SELECT 1 FROM cards AS c2, json_each(c2.doubles)"
        "      WHERE CAST(json_each.value AS TEXT) = cards.id"
        "    )"
        "  )"
        ")"
    )

    def list_unique_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """
        Liste les cartes uniques : pour chaque groupe de doublons, retourne
        la carte dont les coordonnées GPS sont renseignées en priorité.
        Si aucun membre du groupe n'a de GPS, retourne la carte "principale"
        (non référencée comme doublon par une autre carte).

        Paramètres
        ----------
        collection : str | None
            Filtre sur la collection (recherche dans le champ JSON ``collections``).
        search : str | None
            Recherche textuelle (insensible aux accents et à la casse)
            dans title, title2, description, verso_text, recto_text,
            address, poi.
        limit : int | None
            Nombre maximum de résultats.
        offset : int
            Décalage pour la pagination.
        """
        conditions: list[str] = [self._UNIQUE_CARD_CONDITION]
        params: list[Any] = []

        if collection:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        if search:
            like = f"%{search}%"
            conditions.append(
                "(unaccent_lower(title) LIKE unaccent_lower(?)"
                " OR unaccent_lower(title2) LIKE unaccent_lower(?)"
                " OR unaccent_lower(description) LIKE unaccent_lower(?)"
                " OR unaccent_lower(verso_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(recto_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(address) LIKE unaccent_lower(?)"
                " OR unaccent_lower(poi) LIKE unaccent_lower(?))"
            )
            params.extend([like] * 7)

        where = f"WHERE {' AND '.join(conditions)}"
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        offset_clause = f"OFFSET {int(offset)}" if offset else ""

        sql = (
            f"SELECT * FROM cards {where} "
            f"ORDER BY CAST(id AS INTEGER) {limit_clause} {offset_clause}"
        )
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return [_row_to_card(r) for r in cur.fetchall()]

    def list_recent_unique_cards(
        self,
        days: int,
        fallback_count: int,
        collection: str | None = None,
    ) -> list[dict]:
        """
        Liste les cartes uniques (sans doublons) ajoutées dans les
        ``days`` derniers jours (champ ``cdate``).

        Si aucune carte ne correspond à cette fenêtre, retombe sur les
        ``fallback_count`` derniers ajouts (toujours sans doublons),
        quelle que soit leur ancienneté.

        Paramètres
        ----------
        days : int
            Taille de la fenêtre récente, en jours.
        fallback_count : int
            Nombre de cartes à retourner si la fenêtre récente est vide.
        collection : str | None
            Filtre optionnel sur la collection.
        """
        conditions: list[str] = [self._UNIQUE_CARD_CONDITION]
        params: list[Any] = []

        if collection:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        base_where = " AND ".join(conditions)
        conn = self._get_conn()

        # Tentative 1 : cartes ajoutées dans les `days` derniers jours
        cutoff = int(time.time()) - days * 86400
        recent_sql = (
            f"SELECT * FROM cards WHERE {base_where} AND cdate >= ? "
            f"ORDER BY cdate DESC"
        )
        cur = conn.execute(recent_sql, params + [cutoff])
        rows = cur.fetchall()

        if rows:
            return [_row_to_card(r) for r in rows]

        # Repli : les `fallback_count` derniers ajouts, sans contrainte
        # de date (mais toujours sans doublons / avec le filtre collection)
        fallback_sql = (
            f"SELECT * FROM cards WHERE {base_where} "
            f"ORDER BY cdate DESC LIMIT {int(fallback_count)}"
        )
        cur = conn.execute(fallback_sql, params)
        return [_row_to_card(r) for r in cur.fetchall()]

    def count_unique_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
    ) -> int:
        """Retourne le nombre de cartes uniques (cf. list_unique_cards).

        Le comptage utilise la même logique de sélection : une seule carte
        par groupe de doublons, en privilégiant celle avec des coordonnées GPS.
        """
        conditions: list[str] = [self._UNIQUE_CARD_CONDITION]
        params: list[Any] = []

        if collection:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        if search:
            like = f"%{search}%"
            conditions.append(
                "(unaccent_lower(title) LIKE unaccent_lower(?)"
                " OR unaccent_lower(title2) LIKE unaccent_lower(?)"
                " OR unaccent_lower(description) LIKE unaccent_lower(?)"
                " OR unaccent_lower(verso_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(recto_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(address) LIKE unaccent_lower(?)"
                " OR unaccent_lower(poi) LIKE unaccent_lower(?))"
            )
            params.extend([like] * 7)

        where = f"WHERE {' AND '.join(conditions)}"
        sql = f"SELECT COUNT(*) FROM cards {where}"
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Suppression d'une carte
    # ------------------------------------------------------------------

    def delete_card(self, card_id: str | int) -> bool:
        """
        Supprime une carte : fichier JSON dans cards/ et ligne en base.

        Si l'id supprimé apparaît dans le champ ``doubles`` d'autres
        cartes, il en est retiré (JSON + base).

        Retourne True si la carte existait et a été supprimée,
        False si elle n'existait pas.
        """
        card_id = str(card_id)
        path = self._json_path(card_id)
        existed = path.exists()

        # Supprime le fichier JSON
        if existed:
            path.unlink()

        # Supprime la ligne en base
        conn = self._get_conn()
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        conn.commit()

        # Retire card_id du champ doubles des autres cartes
        cur = conn.execute(
            "SELECT id FROM cards WHERE EXISTS ("
            "  SELECT 1 FROM json_each(cards.doubles)"
            "  WHERE CAST(json_each.value AS TEXT) = ?"
            ")",
            (card_id,),
        )
        other_ids = [row["id"] for row in cur.fetchall()]

        for other_id in other_ids:
            self._remove_double(other_id, card_id)

        if existed or other_ids:
            logger.info(
                "delete_card : carte %s supprimée (réf. retirée de %s)",
                card_id, other_ids,
            )

        return existed

    def _remove_double(self, card_id: str, double_id: str) -> None:
        """
        Retire ``double_id`` du champ ``doubles`` de la carte ``card_id``
        (JSON + base), si présent.
        """
        other = self.load_json(card_id)
        other_doubles = {str(d) for d in (other.get("doubles") or [])}
        if double_id not in other_doubles:
            return

        other_doubles.discard(double_id)
        other["doubles"] = sorted(other_doubles)
        other["mdate"] = int(time.time())

        self.cards_dir.mkdir(parents=True, exist_ok=True)
        path = self._json_path(card_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(other, fh, ensure_ascii=False, indent=2)

        self._upsert_card(other)

    def _read_pois_json(self) -> dict:
        """Read pois.json and return the dict {poi_id: {...}}.

        Returns an empty dict if the file is absent or unreadable.
        """
        try:
            with self.pois_json.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _write_pois_json(self, pois: dict) -> None:
        """Atomically write {poi_id: {...}} to pois.json."""
        self.datadir.mkdir(parents=True, exist_ok=True)
        tmp = self.pois_json.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(pois, fh, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.pois_json)

    def _ensure_poi(self, poi_id: str) -> None:
        """Create a skeleton POI entry if it doesn't exist yet."""
        conn = self._get_conn()
        cur = conn.execute("SELECT 1 FROM pois WHERE id = ?", (poi_id,))
        if cur.fetchone() is not None:
            return
        self.write_poi({"id": poi_id, "description": None, "coord": None})
        logger.info("Nouveau POI créé automatiquement : %s", poi_id)

    # ------------------------------------------------------------------
    # POIs
    # ------------------------------------------------------------------

    def get_poi(self, poi_id: str) -> dict | None:
        """Return a POI from SQLite, or None if absent."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM pois WHERE id = ?", (poi_id,))
        row = cur.fetchone()
        return _row_to_poi(row) if row else None

    def list_pois(self) -> list[dict]:
        """Return the list of all POIs (from SQLite)."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM pois ORDER BY id")
        return [_row_to_poi(r) for r in cur.fetchall()]

    def write_poi(self, poi: dict) -> None:
        """Write (INSERT OR REPLACE) a POI in SQLite and in pois.json.

        The JSON file is updated atomically after the SQLite write.
        """
        row = _poi_to_row(poi)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        sql = f"INSERT OR REPLACE INTO pois ({cols}) VALUES ({placeholders})"
        conn = self._get_conn()
        conn.execute(sql, row)
        conn.commit()

        # Update pois.json
        pois = self._read_pois_json()
        pois[str(poi["id"])] = _row_to_poi(conn.execute(
            "SELECT * FROM pois WHERE id = ?", (str(poi["id"]),)
        ).fetchone())
        self._write_pois_json(pois)

    def delete_poi(self, poi_id: str) -> bool:
        """Delete a POI from SQLite and from pois.json.

        Returns True if the POI existed.
        """
        conn = self._get_conn()
        cur = conn.execute("SELECT 1 FROM pois WHERE id = ?", (poi_id,))
        existed = cur.fetchone() is not None
        conn.execute("DELETE FROM pois WHERE id = ?", (poi_id,))
        conn.commit()

        # Update pois.json
        pois = self._read_pois_json()
        if poi_id in pois:
            del pois[poi_id]
            self._write_pois_json(pois)

        return existed

    def sync_pois(self) -> int:
        """Synchronise pois.json → SQLite.

        Inserts or updates every POI present in pois.json whose entry in
        SQLite is absent or older (based on presence, not mdate — POIs
        have no mdate). Returns the number of POIs written.
        """
        pois = self._read_pois_json()
        if not pois:
            return 0
        conn = self._get_conn()
        count = 0
        for poi_id, poi_data in pois.items():
            poi_data["id"] = poi_id
            row = _poi_to_row(poi_data)
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            conn.execute(
                f"INSERT OR REPLACE INTO pois ({cols}) VALUES ({placeholders})",
                row,
            )
            count += 1
        conn.commit()
        logger.info("sync_pois : %d POI(s) synchronisé(s)", count)
        return count

    # ------------------------------------------------------------------
    # Auths
    # ------------------------------------------------------------------

    def get_auth(self, email: str) -> dict | None:
        """Return an auth entry from SQLite, or None if absent.

        The ``auth`` field contains the PBKDF2 hash, not the original
        password. Use :meth:`check_auth` to verify a plain-text password.
        """
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM auths WHERE email = ?", (email,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_auths(self) -> list[dict]:
        """Return all auth entries (email only — hash not exposed)."""
        conn = self._get_conn()
        cur = conn.execute("SELECT email FROM auths ORDER BY email")
        return [{"email": row["email"]} for row in cur.fetchall()]

    def write_auth(self, email: str, password: str) -> None:
        """Hash *password* with PBKDF2 and store it for *email*.

        Raises :class:`ValueError` if *password* is empty.
        """
        if not password:
            raise ValueError("Password must not be empty")
        hashed = _hash_password(password)
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO auths (email, auth) VALUES (?, ?)",
            (email, hashed),
        )
        conn.commit()
        logger.info("write_auth : entrée mise à jour pour %s", email)

    def check_auth(self, email: str, password: str) -> bool:
        """Return True if *password* matches the stored hash for *email*.

        Always returns False if *password* is empty, if *email* does not
        exist, or if the stored value cannot be parsed as a PBKDF2 hash.
        """
        if not password:
            return False
        entry = self.get_auth(email)
        if entry is None:
            return False
        return _verify_password(password, entry.get("auth") or "")

    def delete_auth(self, email: str) -> bool:
        """Delete an auth entry. Returns True if it existed."""
        conn = self._get_conn()
        cur = conn.execute("SELECT 1 FROM auths WHERE email = ?", (email,))
        existed = cur.fetchone() is not None
        conn.execute("DELETE FROM auths WHERE email = ?", (email,))
        conn.commit()
        return existed

    # ------------------------------------------------------------------
    # Updates (updates.json)
    # ------------------------------------------------------------------

    def read_updates(self) -> list[dict]:
        """Read updates.json and return the list of update entries.

        Each entry has the shape:
          {"email": str, "password": str, "card_id": str,
           "lat": float, "lon": float}

        Returns an empty list if the file is absent, unreadable or invalid.
        Empty-password entries are silently skipped (authentication would
        always fail for them).
        """
        try:
            with self.updates_json.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                return []
            return [e for e in data if isinstance(e, dict) and e.get("password")]
        except (OSError, json.JSONDecodeError):
            return []

    def _write_updates(self, entries: list[dict]) -> None:
        """Atomically write the updates list back to updates.json."""
        tmp = self.updates_json.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.updates_json)

    def updates_for_card(self, card_id: str | int) -> list[dict]:
        """Return all update entries for a given card id.

        Only entries whose password matches a stored auth are returned;
        entries with an unknown email or a wrong password are silently
        ignored.
        """
        cid = str(card_id)
        result = []
        for entry in self.read_updates():
            if str(entry.get("card_id")) != cid:
                continue
            if not self.check_auth(entry.get("email", ""), entry.get("password", "")):
                continue
            result.append(entry)
        return result

    def apply_update_gps(self, entry: dict) -> bool:
        """Apply the GPS coordinates from an update entry to the card.

        Updates both the JSON file and the SQLite database.
        Returns True if the card was found and updated.
        """
        card_id = str(entry.get("card_id", ""))
        lat = entry.get("lat")
        lon = entry.get("lon")
        if not card_id or lat is None or lon is None:
            return False
        card = self.load_json(card_id)
        card["coord"] = [float(lat), float(lon)]
        self.write_json(card)
        logger.info("apply_update_gps : carte %s → [%s, %s]", card_id, lat, lon)
        return True

    def delete_update(self, email: str, card_id: str | int) -> bool:
        """Remove all entries matching (email, card_id) from updates.json.

        Returns True if at least one entry was removed.
        """
        cid = str(card_id)
        entries = self.read_updates()
        filtered = [e for e in entries
                    if not (str(e.get("card_id")) == cid and e.get("email") == email)]
        removed = len(entries) - len(filtered)
        if removed:
            if filtered:
                self._write_updates(filtered)
            else:
                # Empty list: remove the file entirely to keep things clean
                try:
                    self.updates_json.unlink()
                except OSError:
                    pass
            logger.info("delete_update : %d entrée(s) supprimée(s) pour carte %s / %s",
                        removed, cid, email)
        return bool(removed)
