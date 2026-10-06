const CACHE_NAME = 'creative-report-shell-v3';

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

self.addEventListener('push', event => {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (error) {
      console.error('Push payload was not valid JSON:', error);
      payload = { title: 'Creative report update', body: event.data.text() };
    }
  }

  const title = payload.title || 'Creative report update';
  const options = {
    body: payload.body || 'The spreadsheet has new updates.',
    icon: '/logo.png',
    badge: '/logo.png',
    tag: payload.tag || `creative-report-${Date.now()}`,
    data: { etName: payload.etName || null },
    renotify: true
  };
  const delaySeconds = Math.max(0, Math.min(Number(payload.delaySeconds) || 0, 30));
  event.waitUntil(new Promise(resolve => setTimeout(resolve, delaySeconds * 1000))
    .then(() => self.registration.showNotification(title, options)));
});

self.addEventListener('notificationclick', event => {
  const etName = event.notification.data?.etName || null;
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of windows) {
      if ('focus' in client) {
        await client.focus();
        if (etName) client.postMessage({ type: 'OPEN_ET_GROUP', etName });
        return;
      }
    }
    return self.clients.openWindow('/');
  })());
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
