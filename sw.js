// Service worker של Bake & Party: מקבל התראות ופותח את האתר בלחיצה. בכוונה בלי מטמון, כדי שלא יוצג תוכן ישן.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(d.title || 'Bake & Party', {
    body: d.body || '', icon: '/icon-192.png', badge: '/icon-192.png', dir: 'rtl', lang: 'he',
    data: { url: d.url || '/' }, tag: d.tag || 'bp-news', renotify: false
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
