"""
flpostcards - Application Flask de consultation des cartes postales.
"""

from __future__ import annotations

import configparser
from pathlib import Path

from flask import Flask, request
from flask_babel import Babel

from libpostcards.model import Model

# Langues disponibles pour l'application Flask
LANGUAGES = ["fr", "en"]


def select_locale() -> str:
    """Détermine la langue à utiliser pour la requête courante."""
    return request.accept_languages.best_match(LANGUAGES) or "fr"


def load_config(app: Flask, config_path: str | Path = "postcards.conf") -> None:
    """Charge postcards.conf (section DEFAULT + section [flask])."""
    parser = configparser.ConfigParser()
    parser.read(config_path)

    datadir = parser.get("DEFAULT", "datadir", fallback="datadir")
    app.config["DATADIR"] = Path(datadir).resolve()

    # Paramètres de verrouillage fichier (lockfile) pour updates.json
    app.config["LOCK_SUFFIX"] = parser.get(
        "DEFAULT", "lock_suffix", fallback=".lck"
    ).strip()
    app.config["LOCK_POLL_INTERVAL"] = parser.getfloat(
        "DEFAULT", "lock_poll_interval", fallback=2.0
    )
    app.config["LOCK_TIMEOUT"] = parser.getfloat(
        "DEFAULT", "lock_timeout", fallback=60.0
    )

    # Liste des collections connues (définie dans [DEFAULT], accessible
    # depuis [flask] via l'héritage configparser)
    collections_raw = parser.get("DEFAULT", "collections", fallback="")
    app.config["COLLECTIONS"] = [
        c.strip() for c in collections_raw.split(",") if c.strip()
    ]

    # Sous-ensemble des collections proposées comme filtre sur la carte
    # (/map/) ; à défaut, retombe sur la liste complète des collections.
    # Accepte aussi "collections_maps" (variante avec 's') par tolérance.
    collections_map_raw = parser.get(
        "DEFAULT", "collections_map", fallback=""
    ) or parser.get("DEFAULT", "collections_maps", fallback="")
    if collections_map_raw.strip():
        app.config["COLLECTIONS_MAP"] = [
            c.strip() for c in collections_map_raw.split(",") if c.strip()
        ]
    else:
        app.config["COLLECTIONS_MAP"] = app.config["COLLECTIONS"]

    if parser.has_section("flask"):
        defaults = set(parser.defaults().keys())
        for key, value in parser.items("flask"):
            if key in defaults:
                # Clé héritée de [DEFAULT] (ex: datadir), déjà traitée
                continue
            if key == "debug":
                app.config["DEBUG"] = parser.getboolean("flask", "debug")
            elif key == "port":
                app.config["PORT"] = parser.getint("flask", "port")
            elif key == "secret_key":
                app.config["SECRET_KEY"] = value
            elif key == "recent_days":
                app.config["RECENT_DAYS"] = parser.getint("flask", "recent_days")
            elif key == "recent_fallback_count":
                app.config["RECENT_FALLBACK_COUNT"] = parser.getint(
                    "flask", "recent_fallback_count"
                )
            else:
                app.config[key.upper()] = value

    app.config.setdefault("RECENT_DAYS", 30)
    app.config.setdefault("RECENT_FALLBACK_COUNT", 20)


def create_app(config_path: str | Path = "postcards.conf") -> Flask:
    app = Flask(__name__)
    load_config(app, config_path)

    app.config.setdefault("LANGUAGES", LANGUAGES)
    app.config.setdefault("BABEL_DEFAULT_LOCALE", "fr")
    app.config.setdefault("BABEL_TRANSLATION_DIRECTORIES", "translations")
    app.config.setdefault("BABEL_DOMAIN", "flpostcards")

    babel = Babel(app, locale_selector=select_locale)

    @app.context_processor
    def inject_locale():
        from flask_babel import get_locale
        return {"get_locale": get_locale}

    @app.context_processor
    def inject_current_path():
        """Chemin courant (avec query string) sans le '?' final superflu."""
        path = request.full_path
        if path.endswith("?"):
            path = path[:-1]
        return {"current_path": path}

    # Modèle partagé (lecture uniquement côté Flask)
    app.model = Model(app.config["DATADIR"])

    from flpostcards.blueprints.home import bp as home_bp
    app.register_blueprint(home_bp)

    from flpostcards.blueprints.gallery import bp as gallery_bp
    app.register_blueprint(gallery_bp)

    from flpostcards.blueprints.travel import bp as travel_bp
    app.register_blueprint(travel_bp)

    from flpostcards.blueprints.map import bp as map_bp
    app.register_blueprint(map_bp)

    from flpostcards.blueprints.slideshow import bp as slideshow_bp
    app.register_blueprint(slideshow_bp)

    from flpostcards.blueprints.api import bp as api_bp
    app.register_blueprint(api_bp)

    return app
