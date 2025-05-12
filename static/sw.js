
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

self.addEventListener('push', (event) => {
  const options = {
    body: event.data.text(),
    icon: '/static/icon.png',
    badge: '/static/badge.png',
    vibrate: [200, 100, 200]
  };
  
  event.waitUntil(
    self.registration.showNotification('WBGT Tracker', options)
  );
});
