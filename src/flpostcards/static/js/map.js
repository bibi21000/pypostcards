/**
 * Page carte (blueprint map) :
 *  - charge les cartes géolocalisées d'une collection (cardsUrl), ou
 *    les points d'intérêt (poisUrl) si la pseudo-collection "Points
 *    d'intérêt" est sélectionnée dans le filtre
 *  - affiche un marqueur par carte ou POI, la vue est ajustée pour voir
 *    tous les marqueurs affichés (fitBounds)
 *  - le survol d'un marqueur de carte affiche un aperçu du recto ; le
 *    survol d'un marqueur de POI affiche sa description
 *  - un clic sur un marqueur de carte ouvre la fiche détaillée de la
 *    carte (les POI n'ont pas de fiche dédiée, pas d'action au clic)
 */
(function () {
    "use strict";

    var POI_PSEUDO_COLLECTION = "__pois__";

    var config = window.MAP_CONFIG || {};

    var mapEl = document.getElementById("cards-map");
    if (!mapEl || typeof L === "undefined") {
        return;
    }

    var map = L.map(mapEl, {
        zoomControl: false
    });

    // Les contrôles de zoom par défaut (haut-gauche) sont masqués par le
    // bandeau d'en-tête (.map-page-header, plein largeur en haut). On les
    // repositionne donc en bas à droite, hors de cette zone.
    L.control.zoom({ position: "bottomright" }).addTo(map);

    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap"
    }).addTo(map);

    function imageUrl(relativePath) {
        return config.imageBaseUrl + relativePath;
    }

    function cardDetailUrl(cardId) {
        return config.cardDetailUrlBase.replace("__ID__", cardId);
    }

    function showCards() {
        var url = config.cardsUrl;
        if (config.currentCollection) {
            url += "?collection=" + encodeURIComponent(config.currentCollection);
        }

        fetch(url)
            .then(function (resp) {
                if (!resp.ok) {
                    throw new Error("no map data");
                }
                return resp.json();
            })
            .then(function (data) {
                var cards = data.cards || [];
                if (!cards.length) {
                    map.setView([0, 0], 2);
                    return;
                }

                var bounds = [];

                cards.forEach(function (card) {
                    var lat = card.coord[0];
                    var lon = card.coord[1];
                    bounds.push([lat, lon]);

                    var marker = L.circleMarker([lat, lon], {
                        radius: 7,
                        color: "#fff",
                        weight: 2,
                        fillColor: "#2a6df4",
                        fillOpacity: 0.9
                    }).addTo(map);

                    var popupHtml =
                        '<div class="map-popup">' +
                        '<img src="' + imageUrl(card.recto) + '" alt="' +
                        (card.title ? card.title.replace(/"/g, "&quot;") : "") + '">' +
                        (card.title ? '<div class="map-popup-title">' + card.title + '</div>' : '') +
                        '</div>';

                    marker.bindPopup(popupHtml, {
                        closeButton: false,
                        className: "map-popup-wrapper"
                    });

                    marker.on("mouseover", function () {
                        marker.openPopup();
                        marker.setStyle({ fillColor: "#e63946", radius: 9 });
                    });

                    marker.on("mouseout", function () {
                        marker.closePopup();
                        marker.setStyle({ fillColor: "#2a6df4", radius: 7 });
                    });

                    marker.on("click", function () {
                        window.location.href = cardDetailUrl(card.id);
                    });
                });

                map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
            })
            .catch(function () {
                map.setView([0, 0], 2);
            });
    }

    function showPois() {
        fetch(config.poisUrl)
            .then(function (resp) {
                if (!resp.ok) {
                    throw new Error("no poi data");
                }
                return resp.json();
            })
            .then(function (data) {
                var pois = data.pois || [];
                if (!pois.length) {
                    map.setView([0, 0], 2);
                    return;
                }

                var bounds = [];

                pois.forEach(function (poi) {
                    var lat = poi.coord[0];
                    var lon = poi.coord[1];
                    bounds.push([lat, lon]);

                    var marker = L.circleMarker([lat, lon], {
                        radius: 6,
                        color: "#fff",
                        weight: 2,
                        fillColor: "#2ecc71",
                        fillOpacity: 0.9
                    }).addTo(map);

                    var label = poi.description || poi.id;
                    var popupHtml =
                        '<div class="map-popup map-popup-poi">' +
                        '<div class="map-popup-title">' +
                        (label ? String(label).replace(/</g, "&lt;") : "") +
                        '</div></div>';

                    marker.bindPopup(popupHtml, {
                        closeButton: false,
                        className: "map-popup-wrapper"
                    });

                    marker.on("mouseover", function () {
                        marker.openPopup();
                        marker.setStyle({ fillColor: "#27ae60", radius: 8 });
                    });

                    marker.on("mouseout", function () {
                        marker.closePopup();
                        marker.setStyle({ fillColor: "#2ecc71", radius: 6 });
                    });
                });

                map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
            })
            .catch(function () {
                map.setView([0, 0], 2);
            });
    }

    if (config.currentCollection === POI_PSEUDO_COLLECTION) {
        showPois();
    } else {
        showCards();
    }
})();
