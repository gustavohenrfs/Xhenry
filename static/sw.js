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
  // Marca o ícone do app instalado (barra de tarefas, tela de início) mesmo com o app fechado; ao
  // abrir, a página troca a marca pelo número de alertas não vistos.
  if (self.navigator && self.navigator.setAppBadge) self.navigator.setAppBadge().catch(() => {});
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

// O navegador trocou a inscrição sozinho (expirou, renovou): inscreve de novo com a mesma chave e
// avisa o servidor, sem esperar o painel ser aberto. Sem a inscrição antiga não dá para provar quem é;
// aí fica para a próxima vez que o painel abrir (enablePush no app.js).
self.addEventListener("pushsubscriptionchange", (event) => {
  const old = event.oldSubscription;
  if (!old) return;
  event.waitUntil((async () => {
    const key = old.options && old.options.applicationServerKey;
    const sub = event.newSubscription
      || (key && await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }));
    if (!sub) return;
    const json = sub.toJSON();
    await fetch("/api/push/renew", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_endpoint: old.endpoint, endpoint: json.endpoint, keys: json.keys })
    });
  })().catch(() => {}));
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
