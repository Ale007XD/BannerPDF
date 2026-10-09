/* Service worker админки BannerBot: только Web Push, без кэширования (админка всегда живая).
   Область действия — /admin/ (файл лежит в этой папке). */
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// Обработчик нужен старым Chrome для установки PWA; запросы идут в сеть как обычно.
self.addEventListener("fetch", () => {});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }

  const options = {
    body: data.body || "",
    icon: "/admin/icons/icon-192.png",
    data: { url: typeof data.url === "string" ? data.url : "/admin/" },
  };
  // tag заменяет прежнее уведомление по тому же заказу («Новый заказ» → «Оплачено»);
  // renotify без tag бросает TypeError, поэтому ставим только вместе
  if (data.tag) {
    options.tag = String(data.tag);
    options.renotify = true;
  }

  // userVisibleOnly: на каждый push обязано быть видимое уведомление
  event.waitUntil(self.registration.showNotification(data.title || "BannerBot", options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  // Открываем только страницы нашей админки, что бы ни пришло в payload
  let target = new URL("/admin/", self.location.origin);
  try {
    const wanted = new URL(event.notification.data && event.notification.data.url, self.location.origin);
    if (wanted.origin === self.location.origin && wanted.pathname.startsWith("/admin/")) target = wanted;
  } catch { /* битый url — открываем /admin/ */ }

  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).pathname.startsWith("/admin/")) {
        await client.focus().catch(() => {}); // на части платформ focus() отклоняется — всё равно навигируем
        if ("navigate" in client) await client.navigate(target.href).catch(() => {});
        return;
      }
    }
    await self.clients.openWindow(target.href);
  })());
});
