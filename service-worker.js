const CACHE_NAME = 'creative-report-shell-v2';

self.addEventListener('install', event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => Promise.all(
      cacheNames
        .filter(cacheName => cacheName !== CACHE_NAME)
        .map(cacheName => caches.delete(cacheName))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const requestUrl = new URL(request.url);

  if (
    request.method !== 'GET' ||
    requestUrl.origin !== self.location.origin ||
    requestUrl.pathname.startsWith('/api/')
  ) {
    return;
  }

  event.respondWith(
    fetch(request).then(response => {
      if (response.ok && response.type === 'basic') {
        const responseCopy = response.clone();
        event.waitUntil(
          caches.open(CACHE_NAME).then(cache => cache.put(request, responseCopy))
        );
      }
      return response;
    }).catch(async error => {
      const cachedResponse = await caches.match(request);
      if (cachedResponse) return cachedResponse;

      if (request.mode === 'navigate') {
        const cachedIndex = await caches.match('/index.html');
        if (cachedIndex) return cachedIndex;
      }

      throw error;
    })
  );
});
