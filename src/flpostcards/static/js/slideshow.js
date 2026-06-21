/**
 * Diaporama (page d'accueil et blueprint slideshow) :
 *  - récupère une liste de cartes via l'URL configurée (config.cardsUrl)
 *  - si config.shuffle === false : parcourt les cartes dans l'ordre
 *    reçu du serveur (cdate descendant pour l'accueil) puis reboucle
 *  - sinon (défaut) : shuffle bag — toutes les cartes vues une fois
 *    avant répétition (Fisher-Yates)
 *  - affiche le recto en fond plein écran avec un fondu croisé
 *  - affiche le verso dans une vignette PiP en bas à droite
 *  - affiche la date d'ajout de la carte courante (si l'élément
 *    #slideshow-cdate est présent dans la page)
 *  - alterne toutes les `intervalSeconds`
 *  - un clic ouvre la fiche détaillée de la carte affichée
 */
(function () {
    "use strict";

    var config = window.SLIDESHOW_CONFIG || {};
    var intervalMs = (config.intervalSeconds || 8) * 1000;

    var layerA = document.getElementById("layer-a");
    var layerB = document.getElementById("layer-b");
    var pip = document.getElementById("pip");
    var pipImg = document.getElementById("pip-img");
    var slideshow = document.getElementById("slideshow");
    var cdateEl = document.getElementById("slideshow-cdate");

    var layers = [layerA, layerB];
    var activeIndex = 0; // index dans `layers` du calque actuellement visible
    var currentCard = null;
    var timer = null;

    // Le "sac" de cartes restant à montrer dans le tour courant (mode shuffle).
    var bag = [];
    // L'ensemble complet des cartes chargées depuis le serveur.
    var allCards = [];
    // Index courant pour le mode séquentiel (config.shuffle === false).
    var seqIndex = 0;

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
        var url = config.cardsUrl;
        if (config.collection) {
            url += "?collection=" + encodeURIComponent(config.collection);
        }
        return fetch(url)
            .then(function (resp) {
                if (!resp.ok) {
                    throw new Error("no cards");
                }
                return resp.json();
            })
            .then(function (data) {
                return data.cards || [];
            });
    }

    // Mélange de Fisher-Yates : chaque permutation est équiprobable,
    // contrairement à des approches naïves (tri par clé aléatoire, etc.)
    function shuffle(array) {
        var result = array.slice();
        for (var i = result.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1));
            var tmp = result[i];
            result[i] = result[j];
            result[j] = tmp;
        }
        return result;
    }

    function refillBag() {
        bag = shuffle(allCards);
    }

    function drawNextCard() {
        if (!allCards.length) {
            return null;
        }
        // Mode séquentiel (config.shuffle === false) : parcours dans l'ordre
        // du serveur (cdate desc pour l'accueil), reboucle après la dernière.
        if (config.shuffle === false) {
            var card = allCards[seqIndex % allCards.length];
            seqIndex++;
            return card;
        }
        // Mode shuffle bag (défaut) : toutes les cartes vues avant répétition.
        if (!bag.length) {
            refillBag();
        }
        return bag.pop();
    }

    function formatDate(timestamp) {
        if (!timestamp) {
            return "";
        }
        var date = new Date(timestamp * 1000);
        try {
            return date.toLocaleDateString(document.documentElement.lang || undefined, {
                year: "numeric",
                month: "long",
                day: "numeric"
            });
        } catch (e) {
            return date.toLocaleDateString();
        }
    }

    function showCard(card) {
        var rectoUrl = imageUrl(card.recto);
        var versoUrl = imageUrl(card.verso_small);

        return Promise.all([preload(rectoUrl), preload(versoUrl)]).then(function () {
            // -- Fondu croisé du recto ------------------------------------------------
            var nextIndex = 1 - activeIndex;
            var nextLayer = layers[nextIndex];
            var currentLayer = layers[activeIndex];

            nextLayer.style.backgroundImage = "url('" + rectoUrl + "')";

            // Force le navigateur à appliquer le style avant de déclencher la transition
            requestAnimationFrame(function () {
                nextLayer.classList.add("visible");
                nextLayer.classList.remove("hidden-layer");
                currentLayer.classList.remove("visible");
                currentLayer.classList.add("hidden-layer");
            });

            activeIndex = nextIndex;

            // -- Vignette PiP (verso) ---------------------------------------------------
            if (pip.classList.contains("visible")) {
                pipImg.classList.add("fading");
                setTimeout(function () {
                    pipImg.src = versoUrl;
                    pipImg.classList.remove("fading");
                }, 300);
            } else {
                pipImg.src = versoUrl;
                pip.classList.add("visible");
            }

            // -- Date d'ajout -------------------------------------------------------
            if (cdateEl) {
                var formatted = formatDate(card.cdate);
                if (formatted) {
                    cdateEl.textContent = (config.addedOnLabel || "") + " " + formatted;
                    cdateEl.classList.add("visible");
                } else {
                    cdateEl.classList.remove("visible");
                }
            }

            currentCard = card;
        });
    }

    function nextSlide() {
        var card = drawNextCard();
        if (!card) {
            return;
        }
        showCard(card);
    }

    function startLoop() {
        if (timer) {
            clearInterval(timer);
        }
        timer = setInterval(nextSlide, intervalMs);
    }

    slideshow.addEventListener("click", function (event) {
        if (event.target.closest(".page-header")) {
            return;
        }
        if (!currentCard) {
            return;
        }
        var url = config.cardDetailUrlBase.replace("__ID__", currentCard.id);
        window.location.href = url;
    });

    fetchCards()
        .then(function (cards) {
            allCards = cards;
            if (config.shuffle !== false) {
                refillBag();
            }
            nextSlide();
            startLoop();
        })
        .catch(function () {
            // Pas de carte disponible : rien à afficher pour le moment
        });
})();
