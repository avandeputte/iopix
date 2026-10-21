/** The entry point: the modules, then the service worker that keeps the app shell available (installable, loads offline). */
import './js/main.js';
if ('serviceWorker' in navigator && location.protocol !== 'file:') navigator.serviceWorker.register('sw.js').catch(() => {});
