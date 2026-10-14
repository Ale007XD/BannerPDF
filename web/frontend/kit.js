/* Собственный счётчик событий: без cookie и идентификаторов посетителя.
 *
 * Отправляет на /api/beat только название события, страницу, категорию источника
 * (yandex / google / direct / internal / other) и короткую метку (id примера, канал связи,
 * страница-источник). Сервер лишь увеличивает суммарный счётчик за день: IP и User-Agent
 * не сохраняются, связать события одного посетителя нельзя.
 */
(function (w, d) {
  "use strict";

  var ENDPOINT = "/api/beat";
  var FUNNEL = { preview_generated: 1, start_order: 1, payment_started: 1, download_success: 1, prefill: 1 };
  var SAFE = /^[A-Za-z0-9\/._-]{0,64}$/;

  function classify() {
    var ref = d.referrer;
    if (!ref) return { source: "direct", entry: "direct" };
    var url;
    try { url = new URL(ref); } catch (e) { return { source: "other", entry: "other" }; }
    var host = url.hostname;
    if (host === w.location.hostname) {
      var path = url.pathname;
      return { source: "internal", entry: SAFE.test(path) ? path : "internal" };
    }
    if (/(^|\.)(yandex|ya)\.(ru|com|by|kz|uz)$/.test(host)) return { source: "yandex", entry: "yandex" };
    if (/(^|\.)google\./.test(host)) return { source: "google", entry: "google" };
    return { source: "other", entry: "other" };
  }

  var origin = classify();

  // Не считаем роботов и автоматизацию, а также тех, кто включил «Не отслеживать» (DNT) или
  // Global Privacy Control. Владелец отключает счётчик у себя, открыв любую страницу с ?notrack=1:
  // в localStorage остаётся флаг nt (только он, больше счётчик ничего не сохраняет).
  var ROBOT = /bot|crawl|spider|slurp|headless|lighthouse|pagespeed|inspectiontool|googleother|facebookexternalhit|preview/i;

  function optedOut() {
    try {
      if (/[?&]notrack=1(&|$)/.test(w.location.search)) w.localStorage.setItem("nt", "1");
      return w.localStorage.getItem("nt") === "1";
    } catch (e) { return false; }
  }

  var nav = w.navigator || {};
  var disabled = ROBOT.test(nav.userAgent || "") || nav.webdriver === true ||
    nav.doNotTrack === "1" || w.doNotTrack === "1" || nav.globalPrivacyControl === true || optedOut();

  function track(event, detail) {
    if (disabled) return;
    try {
      if (detail === undefined) detail = FUNNEL[event] ? origin.entry : "";
      var body = JSON.stringify({ event: event, page: w.location.pathname, source: origin.source, detail: String(detail) });
      if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "application/json" }));
      } else {
        fetch(ENDPOINT, { method: "POST", headers: { "Content-Type": "application/json" }, body: body, keepalive: true });
      }
    } catch (e) { /* статистика не должна ломать страницу */ }
  }

  w.track = track;
  // Метка входа уходит вместе с заказом, чтобы сервер сам посчитал оплату и выдачу файла.
  w.trackEntry = function () { return disabled ? "" : origin.entry; };

  d.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-track]");
    if (el) track(el.getAttribute("data-track"), el.getAttribute("data-track-detail") || "");
  });

  track("pageview", "");
})(window, document);
