/**
 * Isolamento cross-origin dove gli header non si possono impostare.
 *
 * GitHub Pages non lascia aggiungere `Cross-Origin-Opener-Policy` e
 * `Cross-Origin-Embedder-Policy`, e senza quelli `SharedArrayBuffer` è negato e
 * il WebContainer non parte. Un service worker però intercetta le risposte
 * della propria origine e può riscriverle: gli header glieli mette lui.
 *
 * Lo stesso file fa due mestieri, distinti da chi lo esegue:
 *
 *   - caricato dalla pagina, si registra come service worker e ricarica una
 *     volta sola, perché la prima navigazione non è ancora sotto il suo
 *     controllo e quindi non è passata di qui;
 *   - eseguito *come* service worker, aggiunge i due header a ogni risposta.
 *
 * Vincoli suoi, non aggirabili: deve stare in un file a parte, deve arrivare
 * dalla tua stessa origine (non da un CDN) e la pagina dev'essere in HTTPS o su
 * localhost. Serve solo in hosting statico: se il server manda già gli header
 * — come fa `serve.mjs` — questo codice si accorge che la pagina è già isolata
 * e non fa niente.
 */

if (typeof window === "undefined") {
  // ------------------------------------------------ dentro il service worker
  self.addEventListener("install", () => self.skipWaiting());
  self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

  self.addEventListener("fetch", (event) => {
    const request = event.request;

    // Solo la nostra origine. Gli header dell'isolamento servono sulla pagina e
    // sui suoi file, non altrove — e soprattutto: le chiamate ad AWS non devono
    // passare da qui. Non intercettarle significa che nessuno le tocca, e che
    // nell'audit di rete compaiono per quello che sono.
    if (new URL(request.url).origin !== self.location.origin) return;

    event.respondWith((async () => {
      const response = await fetch(request);
      const headers = new Headers(response.headers);
      headers.set("Cross-Origin-Embedder-Policy", "require-corp");
      headers.set("Cross-Origin-Opener-Policy", "same-origin");
      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers,
      });
    })());
  });
} else {
  // ------------------------------------------------------- dentro la pagina
  // `currentScript` va letto subito: dentro la funzione asincrona è già null
  const source = document.currentScript.src;

  /**
   * Risolve a `true` se sta per arrivare un reload, così la pagina può dirlo
   * invece di mostrare un errore che fra un istante non sarà più vero.
   */
  window.coiPending = (async () => {
    if (window.crossOriginIsolated) return false;      // il server fa già il suo
    if (!navigator.serviceWorker) return false;        // niente da fare qui

    // guardia contro il ciclo: se dopo un giro non siamo isolati, il problema
    // è un altro e ricaricare all'infinito non lo risolve
    const KEY = "coi-service-worker-reloaded";
    if (sessionStorage.getItem(KEY)) return false;

    try {
      const registration = await navigator.serviceWorker.register(source);
      if (registration.active && !navigator.serviceWorker.controller) {
        sessionStorage.setItem(KEY, "1");
        window.location.reload();
        return true;
      }
      // appena il worker si attiva, la navigazione successiva passa di lì
      registration.addEventListener("updatefound", () => {
        sessionStorage.setItem(KEY, "1");
        window.location.reload();
      });
      return true;
    } catch (error) {
      console.error("coi-serviceworker:", error);
      return false;
    }
  })();
}
