"""
libpostcards/model.py - Accès centralisé aux données : JSON, SQLite

Gère les cartes postales (cards) et les trajets (travels).
Pas de dépendance externe hormis la bibliothèque standard.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connexion SQLite
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Retourne (et ouvre si nécessaire) la connexion SQLite."""
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
        return self._conn

    def close(self) -> None:
        """Ferme la connexion SQLite."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

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
        conn.executescript(_DDL_CARDS + _DDL_TRAVELS + _DDL_INDEXES)
        conn.commit()
        logger.info("Base créée : %s", self.db_path)

        if not self.cards_dir.exists():
            logger.warning("cards_dir introuvable : %s", self.cards_dir)
            return 0

        count = 0
        for json_path in sorted(self.cards_dir.glob("*.json")):
            with json_path.open(encoding="utf-8") as fh:
                card = json.load(fh)
            self._upsert_card(card)
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
                "(title LIKE ? OR title2 LIKE ? OR description LIKE ?"
                " OR verso_text LIKE ? OR recto_text LIKE ? OR address LIKE ?)"
            )
            params.extend([like] * 6)

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
                "(title LIKE ? OR title2 LIKE ? OR description LIKE ?"
                " OR verso_text LIKE ? OR recto_text LIKE ? OR address LIKE ?)"
            )
            params.extend([like] * 6)

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

    def list_unique_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """
        Liste les cartes en excluant celles référencées comme doublons
        par une autre carte (champ ``doubles``).

        Une carte est exclue si son id apparaît dans le champ
        ``doubles`` d'une autre carte.

        Paramètres
        ----------
        collection : str | None
            Filtre sur la collection (recherche dans le champ JSON ``collections``).
        search : str | None
            Recherche textuelle dans title, title2, description,
            verso_text, recto_text, address.
        limit : int | None
            Nombre maximum de résultats.
        offset : int
            Décalage pour la pagination.
        """
        conditions: list[str] = [
            "NOT EXISTS ("
            "  SELECT 1 FROM cards AS c2, json_each(c2.doubles)"
            "  WHERE json_each.value = cards.id"
            ")"
        ]
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
                "(title LIKE ? OR title2 LIKE ? OR description LIKE ?"
                " OR verso_text LIKE ? OR recto_text LIKE ? OR address LIKE ?)"
            )
            params.extend([like] * 6)

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

    def count_unique_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
    ) -> int:
        """Retourne le nombre de cartes uniques (cf. list_unique_cards)."""
        conditions: list[str] = [
            "NOT EXISTS ("
            "  SELECT 1 FROM cards AS c2, json_each(c2.doubles)"
            "  WHERE json_each.value = cards.id"
            ")"
        ]
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
                "(title LIKE ? OR title2 LIKE ? OR description LIKE ?"
                " OR verso_text LIKE ? OR recto_text LIKE ? OR address LIKE ?)"
            )
            params.extend([like] * 6)

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
            "  WHERE json_each.value = ?"
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
