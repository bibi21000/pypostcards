"""
Blueprint home : page d'accueil avec recto/verso aléatoires en diaporama
et fiche détaillée d'une carte postale.
"""

from __future__ import annotations

import random
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from flpostcards.images import SIZE_SMALL, ALLOWED_SIZE_DIRS, card_images, image_dimensions
from flpostcards.icon_generator import get_or_generate_icon

bp = Blueprint("home", __name__, template_folder="../../templates")


def _no_cache(response, status: int | None = None):
    """
    Ajoute les en-têtes empêchant la mise en cache (navigateur, proxy nginx).

    Utilisé pour /api/random-card, qui doit toujours renvoyer une carte
    différente et ne doit donc jamais être servi depuis un cache.
    """
    if status is not None:
        response.status_code = status
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@bp.route("/")
def index():
    """Page d'accueil : diaporama recto/verso aléatoire, filtrable par collection."""
    model = current_app.model

    collections = current_app.config.get("COLLECTIONS", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    from flask_babel import gettext

    if collection:
        page_title = gettext(
            "Ma collection de cartes postales - %(collection)s",
            collection=collection,
        )
    else:
        page_title = gettext("Ma collection de cartes postales")

    # Carte "vedette" pour l'image Open Graph (statique, cohérente avec le filtre)
    og_image_url = None
    og_image_width = None
    og_image_height = None
    total = model.count_unique_cards(collection=collection or None)
    if total:
        offset = random.randint(0, total - 1)
        featured = model.list_unique_cards(
            collection=collection or None, limit=1, offset=offset
        )
        if featured:
            featured_recto = card_images(featured[0]["id"])["recto"]
            og_image_url = url_for(
                "home.images",
                filename=featured_recto,
                _external=True,
            )
            dims = image_dimensions(current_app.config["DATADIR"], featured_recto)
            if dims:
                og_image_width, og_image_height = dims

    return render_template(
        "home/index.html",
        page_title=page_title,
        collections=collections,
        current_collection=collection,
        og_title=page_title,
        og_description=gettext(
            "Découvrez ma collection de cartes postales anciennes."
        ),
        og_image=og_image_url,
        og_image_width=og_image_width,
        og_image_height=og_image_height,
        og_type="website",
    )


@bp.route("/images/<path:filename>")
def images(filename: str):
    """
    Sert les images PNG depuis datadir (size_div3, size_div10, size_div20).

    N'autorise que les sous-répertoires size_divX, conformément aux
    contraintes du projet (les images de cards/ ne sont pas exposées).
    """
    allowed_dirs = ALLOWED_SIZE_DIRS
    parts = filename.split("/", 1)
    if len(parts) != 2 or parts[0] not in allowed_dirs:
        abort(404)

    datadir = current_app.config["DATADIR"]
    return send_from_directory(datadir, filename)


@bp.route("/favicon.ico")
@bp.route("/icon.png")
def icon():
    """
    Sert l'icône du site : static/icon.(png|jpg|jpeg) si présent,
    sinon un logo généré à partir du paramètre de config [flask] icon.
    """
    from flask import send_file

    static_dir = Path(current_app.static_folder)
    icon_config = current_app.config.get("ICON")

    icon_path = get_or_generate_icon(
        current_app.config["DATADIR"], static_dir, icon_config
    )
    if icon_path is None:
        abort(404)

    return send_file(icon_path, mimetype="image/png", max_age=86400)


@bp.route("/api/recent-cards")
def api_recent_cards():
    """
    Retourne la liste des cartes "récentes" à présenter dans le
    diaporama de la page d'accueil : celles ajoutées dans la fenêtre
    de RECENT_DAYS jours (cdate), ou à défaut les RECENT_FALLBACK_COUNT
    derniers ajouts si la fenêtre est vide.

    Le mélange et le parcours sans répétition sont effectués côté
    client (JS), à partir de cette liste complète : c'est ce qui
    permet de garantir que toutes les cartes sont vues avant qu'aucune
    ne soit répétée (un tirage aléatoire à chaque appel ne le garantit
    pas, certaines cartes pouvant alors apparaître plusieurs fois
    pendant que d'autres n'apparaissent jamais).
    """
    model = current_app.model

    collections = current_app.config.get("COLLECTIONS", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    days = current_app.config.get("RECENT_DAYS", 30)
    fallback_count = current_app.config.get("RECENT_FALLBACK_COUNT", 20)

    cards = model.list_recent_unique_cards(
        days=days, fallback_count=fallback_count, collection=collection or None
    )

    items = []
    for card in cards:
        images = card_images(card["id"])
        images_small = card_images(card["id"], SIZE_SMALL)
        items.append(
            {
                "id": card["id"],
                "title": card.get("title"),
                "title2": card.get("title2"),
                "recto": images["recto"],
                "verso": images["verso"],
                "verso_small": images_small["verso"],
                "cdate": card.get("cdate"),
            }
        )

    return _no_cache(jsonify({"cards": items}))


@bp.route("/robots.txt")
def robots():
    """robots.txt minimal, pointant vers le sitemap pour faciliter sa découverte."""
    from flask import Response

    lines = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {url_for('home.sitemap', _external=True)}",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@bp.route("/sitemap.xml")
def sitemap():
    """
    Sitemap XML listant les pages principales, toutes les fiches cartes
    (avec lastmod basé sur mdate) et les parcours.

    Les doublons ne sont pas listés séparément : ils ne possèdent pas
    de fiche propre destinée à être indexée (cf. list_unique_cards).
    """
    from flask import Response

    model = current_app.model

    urls = []

    def add(loc: str, lastmod: int | None = None, changefreq: str | None = None):
        urls.append({"loc": loc, "lastmod": lastmod, "changefreq": changefreq})

    add(url_for("home.index", _external=True), changefreq="daily")
    add(url_for("slideshow.index", _external=True), changefreq="daily")
    add(url_for("gallery.index", _external=True), changefreq="weekly")
    add(url_for("travel.index", _external=True), changefreq="weekly")
    add(url_for("map.index", _external=True), changefreq="weekly")

    for card in model.list_unique_cards():
        add(
            url_for("home.card_detail", card_id=card["id"], _external=True),
            lastmod=card.get("mdate"),
            changefreq="monthly",
        )

    for travel in model.list_travels():
        add(
            url_for("travel.detail", travel_id=travel["id"], _external=True),
            changefreq="monthly",
        )

    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml_parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for entry in urls:
        xml_parts.append("  <url>")
        xml_parts.append(f"    <loc>{entry['loc']}</loc>")
        if entry["lastmod"]:
            from datetime import datetime, timezone

            lastmod_str = datetime.fromtimestamp(
                entry["lastmod"], tz=timezone.utc
            ).strftime("%Y-%m-%d")
            xml_parts.append(f"    <lastmod>{lastmod_str}</lastmod>")
        if entry["changefreq"]:
            xml_parts.append(f"    <changefreq>{entry['changefreq']}</changefreq>")
        xml_parts.append("  </url>")
    xml_parts.append("</urlset>")

    return Response("\n".join(xml_parts), mimetype="application/xml")


@bp.route("/card/<card_id>")
def card_detail(card_id: str):
    """Fiche détaillée d'une carte postale (recto/verso, métadonnées)."""
    model = current_app.model
    card = model.get_card(card_id)
    if card is None:
        abort(404)

    from flask_babel import gettext

    images = card_images(card["id"])
    images_small = card_images(card["id"], SIZE_SMALL)

    card_title = card.get("title") or gettext("Carte #%(id)s", id=card["id"])
    og_description = card.get("description") or card.get("title2") or card_title

    # Lien de retour contextuel (ex: vers la galerie, page/filtres conservés).
    # On n'accepte que des chemins locaux (commençant par '/' et pas '//'
    # pour éviter toute redirection ouverte vers un autre domaine).
    back_url = request.args.get("back") or ""
    if not back_url.startswith("/") or back_url.startswith("//"):
        back_url = ""

    back_label = None
    if back_url.startswith("/gallery/"):
        back_label = gettext("Retour à la galerie")
    elif back_url.startswith("/travel/"):
        back_label = gettext("Retour au parcours")
    elif back_url.startswith("/map/") or back_url.startswith("/map?"):
        back_label = gettext("Retour à la carte")
    elif back_url.startswith("/slideshow/") or back_url.startswith("/slideshow?"):
        back_label = gettext("Retour au diaporama")

    dims = image_dimensions(current_app.config["DATADIR"], images["recto"])
    og_image_width, og_image_height = dims if dims else (None, None)

    return render_template(
        "card/detail.html",
        card=card,
        images=images,
        images_small=images_small,
        back_url=back_url,
        back_label=back_label,
        og_title=card_title,
        og_description=og_description,
        og_image=url_for("home.images", filename=images["recto"], _external=True),
        og_image_width=og_image_width,
        og_image_height=og_image_height,
        og_type="article",
    )
