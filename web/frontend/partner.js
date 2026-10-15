/* Брендированная страница конструктора для типографии-партнёра.
 *
 *   https://bannerbot.ru/?partner=reklamacity
 *
 * Типография ставит редирект со своего домена на такую ссылку. Скрипт берёт код из адреса,
 * запоминает его на время вкладки (sessionStorage), убирает ?partner из адресной строки,
 * запрашивает GET /api/partner/{code} и подменяет в шапке логотип и цвет.
 *
 * Правило безопасности: заказ помечается партнёром (Partner.orderCode()) только после того, как
 * брендинг получен и показан. Макет уходит на почту типографии, и клиент должен видеть это на экране
 * проверки макета. Нет ответа, 404 или сбой сети: страница обычная, заказ без партнёра.
 *
 * Все данные партнёра вставляются через textContent / атрибуты, никогда через innerHTML.
 * Чистые функции (extractCode, stripParam, shortOrderId) проверяет Node: tests/test_partner_frontend.py.
 */
(function (root) {
  "use strict";

  var CODE_RE = /^[a-z0-9][a-z0-9-]{1,31}$/;
  var ACCENT_RE = /^#[0-9a-fA-F]{6}$/;
  var STORAGE_KEY = "bb_partner";
  var PENDING_CLASS = "partner-pending";
  var FETCH_TIMEOUT_MS = 4000;
  var PARAM = "partner";

  function normalize(value) {
    var code = String(value == null ? "" : value).trim().toLowerCase();
    return CODE_RE.test(code) ? code : null;
  }

  /** Код партнёра из строки запроса («?partner=ReklamaCity» → «reklamacity») или null. */
  function extractCode(search) {
    return normalize(new URLSearchParams(search).get(PARAM));
  }

  /** Строка запроса без ?partner; остальные параметры (предзаполнение ?size=…) сохраняются. */
  function stripParam(search) {
    var p = new URLSearchParams(search);
    p.delete(PARAM);
    var s = p.toString();
    return s ? "?" + s : "";
  }

  /** Номер заказа для клиента и типографии: первые 8 символов (как в письме и имени вложения). */
  function shortOrderId(orderId) {
    return String(orderId || "").slice(0, 8);
  }

  var api = {
    code: null,     // код из ссылки или вкладки; пока бренд не получен, в заказ не попадает
    brand: null,    // ответ /api/partner/{code}
    orderCode: function () {
      return api.code && api.brand ? api.code : undefined;
    },
    decorateSuccess: function () {},
    extractCode: extractCode,
    stripParam: stripParam,
    shortOrderId: shortOrderId,
  };

  root.Partner = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof document === "undefined" || typeof window === "undefined") return;

  /* ---------------------------------------------------------------- браузер */
  function storageGet() {
    try { return window.sessionStorage.getItem(STORAGE_KEY); } catch (_) { return null; }
  }
  function storageSet(code) {
    try { window.sessionStorage.setItem(STORAGE_KEY, code); } catch (_) { /* приватный режим: не страшно */ }
  }
  function storageClear() {
    try { window.sessionStorage.removeItem(STORAGE_KEY); } catch (_) { /* noop */ }
  }

  function whenDomReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function renderLogo(brand) {
    var holder = document.getElementById("site-logo");
    if (!holder) return;
    while (holder.firstChild) holder.removeChild(holder.firstChild);
    holder.classList.add("logo--partner");

    function showName() {
      while (holder.firstChild) holder.removeChild(holder.firstChild);
      holder.appendChild(el("span", "logo-name", brand.name));
    }

    if (brand.logo_url) {
      var img = document.createElement("img");
      img.alt = brand.name;
      img.addEventListener("error", showName);  // битая ссылка на логотип: показываем название текстом
      img.src = brand.logo_url;
      holder.appendChild(img);
    } else {
      showName();
    }
  }

  function renderStrip(brand) {
    var strip = document.getElementById("partner-strip");
    if (!strip) return;
    while (strip.firstChild) strip.removeChild(strip.firstChild);
    strip.appendChild(document.createTextNode("Конструктор макетов для «" + brand.name + "». Оплата и поддержка: BannerBot, "));
    var link = el("a", null, "реквизиты");
    link.href = "/requisites.html";
    strip.appendChild(link);
    strip.classList.remove("hidden");
  }

  function renderConfirmNotice(brand) {
    var box = document.getElementById("confirm-partner");
    if (!box || !brand.delivers) return;
    box.textContent = "Готовый макет будет также отправлен в «" + brand.name + "» для печати. Другие данные мы типографии не передаём.";
    box.classList.remove("hidden");
  }

  function applyBranding(brand) {
    if (brand.accent && ACCENT_RE.test(brand.accent)) {
      document.documentElement.style.setProperty("--accent2", brand.accent);
    }
    renderLogo(brand);
    renderStrip(brand);
    renderConfirmNotice(brand);

    api.decorateSuccess = function (orderId) {
      var node = document.getElementById("success-text");
      if (!node || !brand.delivers) return;
      var text = "PDF-файл скачивается. Копия макета отправляется в «" + brand.name + "».";
      var short = shortOrderId(orderId);
      if (short) text += " Номер заказа: " + short + ", назовите его в типографии.";
      node.textContent = text;
    };
  }

  function fetchBrand(code) {
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS) : null;
    return fetch("/api/partner/" + encodeURIComponent(code), {
      headers: { Accept: "application/json" },
      signal: controller ? controller.signal : undefined,
    }).then(function (resp) {
      if (timer) clearTimeout(timer);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    }, function (err) {
      if (timer) clearTimeout(timer);
      throw err;
    });
  }

  function init() {
    var fromUrl = extractCode(window.location.search);
    var code = fromUrl || normalize(storageGet());

    if (fromUrl) {
      storageSet(fromUrl);
      try {
        window.history.replaceState(null, "", window.location.pathname + stripParam(window.location.search) + window.location.hash);
      } catch (_) { /* адрес останется с ?partner, на работу не влияет */ }
    }
    if (!code) return;

    api.code = code;
    // Пока ждём ответ, прячем логотип BannerBot, чтобы он не мигал перед логотипом типографии
    document.documentElement.classList.add(PENDING_CLASS);

    fetchBrand(code).then(function (brand) {
      if (!brand || brand.code !== code || !brand.name) throw new Error("bad brand");
      api.brand = brand;
      whenDomReady(function () { applyBranding(brand); });
    }).catch(function () {
      // Партнёра нет, он отключён или сеть недоступна: обычная страница, заказ без партнёра
      api.code = null;
      api.brand = null;
      storageClear();
    }).then(function () {
      whenDomReady(function () { document.documentElement.classList.remove(PENDING_CLASS); });
    });
  }

  init();
})(typeof window !== "undefined" ? window : this);
