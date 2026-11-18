// Service worker של Bake & Party: התראות, ודף "אין חיבור" כשאין אינטרנט.
// בכוונה בלי מטמון לתוכן, כדי שלא יוצגו מחירים או מלאי ישנים.
const OFFLINE_HTML = `<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>אין חיבור | Bake & Party</title><body style="font-family:Arial,sans-serif;background:#FFF9F5;color:#4A2E35;text-align:center;padding:70px 24px">
<div style="font-size:56px">🧁</div><h2 style="color:#E05276">אין כרגע חיבור לאינטרנט</h2>
<p>ברגע שהחיבור יחזור, האתר ייטען כרגיל.</p>
<button onclick="location.reload()" style="background:#E05276;color:#fff;border:none;border-radius:20px;padding:12px 26px;font-size:16px;font-weight:bold">לנסות שוב</button></body></html>`;
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  if (event.request.mode !== 'navigate') return;      // רק טעינת דף; שאר הבקשות עוברות כרגיל
  event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
});
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
