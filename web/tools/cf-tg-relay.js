/**
 * cf-tg-relay.js — Cloudflare Worker: двусторонний релей Telegram ⇄ bannerbot.ru.
 *
 * Зачем: хостинг сайта не пропускает трафик Telegram ни в одну сторону
 * (с VPS не открывается api.telegram.org; запросы Telegram на сайт обрываются по таймауту).
 * Worker стоит на адресе, до которого доходят обе стороны, и делает два дела:
 *
 *   1. ИСХОДЯЩИЕ (сайт → Telegram): прозрачный прокси Bot API.
 *        POST /<RELAY_SECRET>/bot<TOKEN>/<method>
 *      На сайте: TG_API_BASE=https://<имя>.<аккаунт>.workers.dev/<RELAY_SECRET>
 *      Код сайта не меняется: ответ Telegram (в том числе message_id) возвращается синхронно.
 *
 *   2. ВХОДЯЩИЕ (Telegram → сайт): приём webhook кнопки «Выдать PDF».
 *        POST /<RELAY_SECRET>/tg-hook  →  POST https://bannerbot.ru/api/tg/callback
 *      Webhook регистрируется на  <TG_API_BASE>/tg-hook  (см. README). Тело и заголовок
 *      X-Telegram-Bot-Api-Secret-Token пересылаются как есть, секрет проверяет сайт (TG_WEBHOOK_SECRET).
 *      Статус ответа сайта возвращается Telegram: при 5xx/таймауте Telegram повторит доставку.
 *
 * Развёртывание (Cloudflare → Workers & Pages → Create → Hello World → Edit code → вставить файл → Deploy):
 *   Settings → Variables and Secrets → добавить Secret  RELAY_SECRET  = случайная строка (например, openssl rand -hex 16)
 *
 * Защита: без секретного первого сегмента пути Worker отвечает 404; на Telegram пропускаются только методы
 * из ALLOWED_METHODS (нельзя читать апдейты, удалять webhook, слать файлы и пользоваться как открытым прокси);
 * на сайт уходит только один фиксированный адрес — SITE_CALLBACK.
 * getWebhookInfo/setWebhook нужны для настройки кнопки; после настройки их можно убрать из списка.
 */

const TELEGRAM = "https://api.telegram.org";
const SITE_CALLBACK = "https://bannerbot.ru/api/tg/callback";
const SITE_TIMEOUT_MS = 20000;
// sendMessage / editMessageText / editMessageReplyMarkup / answerCallbackQuery — то, чем пользуется сайт;
// getWebhookInfo / setWebhook — чтобы проверить и перерегистрировать webhook кнопки прямо с VPS (секреты не покидают сервер)
const ALLOWED_METHODS = new Set([
  "sendMessage", "editMessageText", "editMessageReplyMarkup", "answerCallbackQuery",
  "getWebhookInfo", "setWebhook",
]);

// Сравнение за постоянное время: секрет не угадывается по задержке ответа
function safeEqual(a, b) {
  const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

const notFound = () => new Response("not found", { status: 404 });
const json = (status, obj) => new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

// Telegram → сайт
async function forwardUpdate(request) {
  const headers = { "content-type": request.headers.get("content-type") || "application/json" };
  const hookSecret = request.headers.get("x-telegram-bot-api-secret-token");
  if (hookSecret) headers["x-telegram-bot-api-secret-token"] = hookSecret;
  let upstream;
  try {
    upstream = await fetch(SITE_CALLBACK, {
      method: "POST", headers, body: await request.text(), signal: AbortSignal.timeout(SITE_TIMEOUT_MS),
    });
  } catch (e) {
    const timeout = e?.name === "TimeoutError" || e?.name === "AbortError";
    return json(timeout ? 504 : 502, { ok: false, description: "relay: сайт недоступен" });
  }
  return new Response(await upstream.text(), {
    status: upstream.status, headers: { "content-type": upstream.headers.get("content-type") || "application/json" },
  });
}

// сайт → Telegram
async function proxyBotApi(request, bot, method) {
  let upstream;
  try {
    upstream = await fetch(`${TELEGRAM}/${bot}/${method}`, {
      method: "POST",
      headers: { "content-type": request.headers.get("content-type") || "application/json" },
      body: await request.text(),
    });
  } catch {
    return json(502, { ok: false, description: "relay: Telegram недоступен" });
  }
  // Ответ Telegram отдаём как есть (статус и тело): сайт разбирает ok/result/description сам
  return new Response(await upstream.text(), {
    status: upstream.status, headers: { "content-type": "application/json" },
  });
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST" || !env.RELAY_SECRET) return notFound();
    const path = new URL(request.url).pathname;

    // /<RELAY_SECRET>/tg-hook
    const h = path.match(/^\/([^/]+)\/tg-hook$/);
    if (h) return safeEqual(h[1], env.RELAY_SECRET) ? forwardUpdate(request) : notFound();

    // /<RELAY_SECRET>/bot<TOKEN>/<method>
    const m = path.match(/^\/([^/]+)\/(bot[0-9]+:[A-Za-z0-9_-]+)\/([A-Za-z]+)$/);
    if (!m) return notFound();
    const [, secret, bot, method] = m;
    if (!safeEqual(secret, env.RELAY_SECRET) || !ALLOWED_METHODS.has(method)) return notFound();
    return proxyBotApi(request, bot, method);
  },
};
