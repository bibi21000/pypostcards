/**
 * Plein écran pour les diaporamas (slideshow et travel).
 *
 * Ajoute un bouton ⛶ (entrer plein écran) / ✕ (quitter) sur l'élément
 * #slideshow. En mode plein écran :
 *  - le header (.page-header) est masqué (menu, titre, sélecteur)
 *  - le wake lock est (ré)acquis : sur Android, le plein écran via
 *    l'API Fullscreen maintient l'écran allumé de façon beaucoup plus
 *    fiable que le Screen Wake Lock seul, qui peut être refusé
 *    silencieusement par certains navigateurs en mode fenêtré
 *
 * L'API Fullscreen est supportée sur Chrome Android (préfixe webkit),
 * Safari iOS (webkit), et tous les navigateurs de bureau récents.
 * Sur les navigateurs non supportés, le bouton n'est pas affiché.
 */
(function () {
    "use strict";

    var slideshow = document.getElementById("slideshow");
    if (!slideshow) {
        return;
    }

    // Détection du support de l'API Fullscreen (avec préfixes)
    var requestFS = (
        slideshow.requestFullscreen ||
        slideshow.webkitRequestFullscreen ||
        slideshow.mozRequestFullScreen ||
        slideshow.msRequestFullscreen
    );

    var exitFS = (
        document.exitFullscreen ||
        document.webkitExitFullscreen ||
        document.mozCancelFullScreen ||
        document.msExitFullscreen
    );

    var fullscreenElement = function () {
        return (
            document.fullscreenElement ||
            document.webkitFullscreenElement ||
            document.mozFullScreenElement ||
            document.msFullscreenElement ||
            null
        );
    };

    if (!requestFS) {
        // API non supportée, abandon silencieux
        return;
    }

    // --- Bouton plein écran ------------------------------------------------

    var btn = document.createElement("button");
    btn.type = "button";
    btn.id = "fullscreen-btn";
    btn.className = "fullscreen-btn";
    btn.setAttribute("aria-label", "Plein écran");
    btn.setAttribute("title", "Plein écran");
    btn.innerHTML = "&#x26F6;"; // ⛶
    slideshow.appendChild(btn);

    // --- Wake lock ---------------------------------------------------------

    var wakeLock = null;

    function acquireWakeLock() {
        if (!("wakeLock" in navigator)) {
            return;
        }
        navigator.wakeLock
            .request("screen")
            .then(function (lock) { wakeLock = lock; })
            .catch(function () { wakeLock = null; });
    }

    function releaseWakeLock() {
        if (wakeLock) {
            wakeLock.release();
            wakeLock = null;
        }
    }

    // --- Mise à jour de l'UI selon l'état plein écran ---------------------

    function onFullscreenChange() {
        var isFs = Boolean(fullscreenElement());
        slideshow.classList.toggle("is-fullscreen", isFs);

        if (isFs) {
            btn.innerHTML = "&#x2715;"; // ✕
            btn.setAttribute("aria-label", "Quitter le plein écran");
            btn.setAttribute("title", "Quitter le plein écran");
            acquireWakeLock();
        } else {
            btn.innerHTML = "&#x26F6;"; // ⛶
            btn.setAttribute("aria-label", "Plein écran");
            btn.setAttribute("title", "Plein écran");
            releaseWakeLock();
        }
    }

    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange);
    document.addEventListener("mozfullscreenchange", onFullscreenChange);
    document.addEventListener("MSFullscreenChange", onFullscreenChange);

    // --- Clic sur le bouton ------------------------------------------------

    btn.addEventListener("click", function (event) {
        event.stopPropagation();
        if (fullscreenElement()) {
            exitFS.call(document);
        } else {
            requestFS.call(slideshow);
        }
    });
})();
