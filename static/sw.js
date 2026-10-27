// Service worker do XHenry, servido em /sw.js. Só cuida das notificações (Web Push e as que a
// página pede com showNotification); não guarda nada em cache, o painel sempre vem do servidor.
const ICON = "/static/img/icon-192.png";
const BADGE = "/static/img/badge-96.png";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data ? event.data.text() : "" }; }
  const title = data.title || "XHenry";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    icon: data.icon || ICON,
    badge: data.badge || BADGE,
    tag: data.tag || undefined,
    renotify: Boolean(data.tag),
    // Fica na tela (Windows) até ser fechada, em vez de sumir para a central; cada alerta tem tag
    // própria, então eles se acumulam.
    requireInteraction: true,
    vibrate: [120, 60, 120],
    data: { url: data.url || "/" }
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil((async () => {
    if (url === "/") {
      const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const mine = open.find((client) => new URL(client.url).origin === self.location.origin);
      if (mine) return mine.focus();
    }
    return self.clients.openWindow(url);
  })());
});
