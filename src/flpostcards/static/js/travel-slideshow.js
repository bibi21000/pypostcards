/**
 * Diaporama d'un parcours (blueprint travel) :
 *  - charge la liste ordonnée des cartes du parcours (cardsUrl)
 *  - affiche le recto en fond plein écran avec un fondu croisé
 *  - affiche une mini-carte OpenStreetMap en PiP, centrée sur la carte
 *    courante et marquant sa position parmi celles du parcours
 *  - affiche le titre de la carte courante, et celui de la carte
 *    suivante en plus petit
 *  - avance séquentiellement (et boucle) toutes les `intervalSeconds`
 *  - un clic ouvre la fiche détaillée de la carte affichée, avec un
 *    lien de retour vers le parcours
 */
(function () {
    "use strict";

    var config = window.TRAVEL_CONFIG || {};
    var intervalMs = (config.intervalSeconds || 8) * 1000;

    // Niveau de zoom maximal disponible sur les tuiles OpenStreetMap standard
    var TILE_MAX_ZOOM = 19;
    // Niveau de zoom utilisé pour centrer la vignette sur la carte courante
    // (un cran en-dessous du maximum, pour un contexte un peu plus large)
    var MAP_DISPLAY_ZOOM = 18;

    var layerA = document.getElementById("layer-a");
    var layerB = document.getElementById("layer-b");
    var pip = document.getElementById("pip");
    var pipMapEl = document.getElementById("pip-map");
    var slideshow = document.getElementById("slideshow");
    var captionCurrent = document.getElementById("caption-current");
    var captionNext = document.getElementById("caption-next");
    var pauseBtn = document.getElementById("pause-btn");
    var pauseIcon = document.getElementById("pause-icon");

    var layers = [layerA, layerB];
    var activeIndex = 0; // index dans `layers` du calque actuellement visible
    var cards = [];
    var currentIndex = -1;
    var timer = null;
    var isPaused = false;

    var map = null;
    var markers = [];

    function imageUrl(relativePath) {
        return config.imageBaseUrl + relativePath;
    }

    function preload(url) {
        return new Promise(function (resolve) {
            var img = new Image();
            img.onload = function () { resolve(true); };
            img.onerror = function () { resolve(false); };
            img.src = url;
        });
    }

    function fetchCards() {
        return fetch(config.cardsUrl)
            .then(function (resp) {
                if (!resp.ok) {
                    throw new Error("no travel data");
                }
                return resp.json();
            })
            .then(function (data) {
                return data.cards || [];
            });
    }

    function getStartIndex(count) {
        var params = new URLSearchParams(window.location.search);
        var at = parseInt(params.get("at"), 10);
        if (isNaN(at) || at < 0 || at >= count) {
            return 0;
        }
        return at;
    }

    function initMap() {
        if (typeof L === "undefined" || !pipMapEl) {
            return;
        }

        map = L.map(pipMapEl, {
            zoomControl: false,
            attributionControl: true,
            dragging: false,
            scrollWheelZoom: false,
            doubleClickZoom: false,
            touchZoom: false,
            keyboard: false
        });

        L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: TILE_MAX_ZOOM,
            attribution: "&copy; OpenStreetMap"
        }).addTo(map);

        // Un marqueur par carte ayant des coordonnées connues
        markers = cards.map(function (card) {
            if (!card.coord || card.coord[0] == null || card.coord[1] == null) {
                return null;
            }
            return L.circleMarker([card.coord[0], card.coord[1]], {
                radius: 5,
                color: "#fff",
                weight: 2,
                fillColor: "#888",
                fillOpacity: 0.9
            }).addTo(map);
        });

        var initialCoord = (cards[0] && cards[0].coord) || null;
        if (initialCoord) {
            map.setView([initialCoord[0], initialCoord[1]], MAP_DISPLAY_ZOOM);
        } else {
            map.setView([0, 0], 2);
        }
    }

    function updateMap(index) {
        if (!map) {
            return;
        }

        var card = cards[index];

        // Met en évidence le marqueur de la carte en cours
        markers.forEach(function (marker, i) {
            if (!marker) {
                return;
            }
            if (i === index) {
                marker.setStyle({
                    radius: 8,
                    color: "#fff",
                    weight: 3,
                    fillColor: "#e63946",
                    fillOpacity: 1
                });
                marker.bringToFront();
            } else {
                marker.setStyle({
                    radius: 5,
                    color: "#fff",
                    weight: 2,
                    fillColor: "#888",
                    fillOpacity: 0.9
                });
            }
        });

        if (card.coord && card.coord[0] != null && card.coord[1] != null) {
            map.flyTo([card.coord[0], card.coord[1]], MAP_DISPLAY_ZOOM, {
                duration: 1.2
            });
        }
    }

    function showCard(index) {
        var card = cards[index];
        var nextCard = cards[(index + 1) % cards.length];

        var rectoUrl = imageUrl(card.recto);

        return preload(rectoUrl).then(function () {
            // -- Fondu croisé du recto ------------------------------------------------
            var nextIndex = 1 - activeIndex;
            var nextLayer = layers[nextIndex];
            var currentLayer = layers[activeIndex];

            nextLayer.style.backgroundImage = "url('" + rectoUrl + "')";

            requestAnimationFrame(function () {
                nextLayer.classList.add("visible");
                nextLayer.classList.remove("hidden-layer");
                currentLayer.classList.remove("visible");
                currentLayer.classList.add("hidden-layer");
            });

            activeIndex = nextIndex;

            // -- Carte OSM (position de la carte courante) -----------------------------
            if (!pip.classList.contains("visible")) {
                pip.classList.add("visible");
                if (!map) {
                    // La carte doit être visible (dimensions non nulles) pour
                    // que Leaflet calcule correctement sa taille.
                    requestAnimationFrame(function () {
                        initMap();
                        updateMap(index);
                    });
                }
            }
            if (map) {
                updateMap(index);
            }

            // -- Titres -----------------------------------------------------------------
            captionCurrent.textContent = card.title || "";
            if (nextCard && nextCard !== card) {
                captionNext.textContent = (config.nextLabel || "") + " : " + (nextCard.title || "");
                captionNext.classList.add("visible");
            } else {
                captionNext.classList.remove("visible");
            }

            currentIndex = index;
        });
    }

    function nextSlide() {
        if (!cards.length) {
            return;
        }
        var nextIndex = (currentIndex + 1) % cards.length;
        showCard(nextIndex);
    }

    function startLoop() {
        if (timer) {
            clearInterval(timer);
        }
        timer = setInterval(nextSlide, intervalMs);
    }

    function stopLoop() {
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
    }

    function setPaused(paused) {
        isPaused = paused;

        if (isPaused) {
            stopLoop();
        } else {
            startLoop();
        }

        if (pauseBtn) {
            pauseBtn.classList.toggle("paused", isPaused);
            var label = isPaused
                ? (config.resumeLabel || "")
                : (config.pauseLabel || "");
            pauseBtn.setAttribute("aria-label", label);
            pauseBtn.setAttribute("title", label);
        }
        if (pauseIcon) {
            // Pause (deux barres) <-> Lecture (triangle)
            pauseIcon.innerHTML = isPaused ? "&#9654;" : "&#10074;&#10074;";
        }
    }

    function togglePause() {
        setPaused(!isPaused);
    }

    if (pauseBtn) {
        pauseBtn.addEventListener("click", function (event) {
            event.stopPropagation();
            togglePause();
        });
    }

    slideshow.addEventListener("click", function (event) {
        if (event.target.closest(".page-header")) {
            return;
        }
        if (currentIndex < 0 || !cards.length) {
            return;
        }
        var card = cards[currentIndex];
        var back = config.travelUrl + "?at=" + currentIndex;
        var url = config.cardDetailUrlBase.replace("__ID__", card.id)
            + "?back=" + encodeURIComponent(back);
        window.location.href = url;
    });

    fetchCards().then(function (loadedCards) {
        cards = loadedCards;
        if (!cards.length) {
            return;
        }
        var startIndex = getStartIndex(cards.length);
        // showCard avance currentIndex avant le premier affichage
        currentIndex = startIndex - 1;
        if (currentIndex < 0) {
            currentIndex = cards.length - 1;
        }
        nextSlide();
        startLoop();
    });
})();
