self.addEventListener('install', event => {
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then(cachedResponse => {
      if (cachedResponse) {
        return cachedResponse;
      }

      return fetch(event.request).then(response => {
        const responseClone = response.clone();
        caches.open('creative-report-cache').then(cache => {
          cache.put(event.request, responseClone);
        });
        return response;
      }).catch(() => caches.match('/index.html'));
    })
  );
});
