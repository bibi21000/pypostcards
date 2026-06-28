/**
 * Empêche la mise en veille de l'écran pendant les diaporamas.
 *
 * Utilise NoSleep.js qui combine automatiquement :
 *  - l'API Screen Wake Lock (Chrome desktop, navigateurs modernes)
 *  - une balise <video> en lecture silencieuse en boucle (Chrome Android,
 *    Safari iOS) — technique utilisée par YouTube/Netflix, très fiable
 *
 * Le wake lock est activé au premier clic/toucher sur la page (requis
 * par les navigateurs mobiles : l'activation doit être déclenchée par
 * un geste utilisateur). Il est automatiquement désactivé quand l'onglet
 * passe en arrière-plan, et réactivé quand il redevient visible.
 */
(function () {
    "use strict";

    if (typeof NoSleep === "undefined") {
        return;
    }

    var noSleep = new NoSleep();
    var enabled = false;

    function enable() {
        if (enabled) {
            return;
        }
        noSleep.enable();
        enabled = true;
    }

    function disable() {
        if (!enabled) {
            return;
        }
        noSleep.disable();
        enabled = false;
    }

    // Activation au premier geste utilisateur (obligatoire sur mobile)
    document.addEventListener("click", function handler() {
        enable();
        document.removeEventListener("click", handler);
    }, { once: true });

    document.addEventListener("touchstart", function handler() {
        enable();
        document.removeEventListener("touchstart", handler);
    }, { once: true });

    // Pause/reprise selon la visibilité de l'onglet
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") {
            enable();
        } else {
            disable();
        }
    });
})();
