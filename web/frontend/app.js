/**
 * app.js — BannerPrint конструктор
 *
 * Флоу:
 *   init() → GET /api/templates → renderSizes / renderColors / renderFonts →
 *   настройка → дебаунс превью (500 мс) → кнопка «Получить PDF» →
 *   POST /api/order → переход к оплате → поллинг статуса →
 *   GET /api/download/{token} → скачивание файла
 */

"use strict";

/* =====================================================================
   КОНФИГУРАЦИЯ
   ===================================================================== */
const API = {
  templates: "/api/templates",
  preview:   "/api/preview",
  order:     "/api/order",
  status:    (id) => `/api/payment/status/${id}`,
  download:  (token) => `/api/download/${token}`,
  refStats:  (code) => `/api/referral/stats/${code}`,
};

const PREVIEW_DEBOUNCE_MS = 500;
const POLL_INTERVAL_MS    = 2500;
const POLL_MAX_ATTEMPTS   = 80;   // ~200 сек максимум

/* =====================================================================
   СОСТОЯНИЕ
   ===================================================================== */
const state = {
  sizeKey:   null,   // задаётся после загрузки шаблонов (первый размер)
  bgColor:   null,   // задаётся после загрузки шаблонов (первый цвет)
  textColor: null,   // задаётся после загрузки шаблонов (второй цвет)
  font:      null,   // задаётся после загрузки шаблонов (первый шрифт)
  lines:     ["", ""],   // до max_lines строк
  maxLines:  6,          // обновляется из шаблонов
  refCode:   "",

  // Оплата
  orderId:   null,
  payUrl:    null,

  // Список имён цветов из шаблона (для защиты от совпадения)
  colorNames: [],
};

/* =====================================================================
   DOM-ССЫЛКИ
   ===================================================================== */
const $ = (id) => document.getElementById(id);

const el = {
  previewImg:         $("preview-img"),
  previewPlaceholder: $("preview-placeholder"),
  previewLoader:      $("preview-loader"),
  previewMeta:        $("preview-meta"),

  sizeGrid:    $("size-grid"),
  bgSwatches:  $("bg-swatches"),
  txtSwatches: $("text-swatches"),
  textLines:   $("text-lines"),
  addLineBtn:  $("add-line-btn"),
  fontList:    $("font-list"),
  refInput:    $("ref-input"),
  refStatus:   $("ref-status"),
  buyBtn:      $("buy-btn"),

  // Модалки
  modalError:   $("modal-error"),
  errorTitle:   $("error-title"),
  errorText:    $("error-text"),
  errorClose:   $("error-close"),

  modalPayment: $("modal-payment"),
  payBtn:       $("pay-btn"),
  payCancel:    $("pay-cancel"),

  modalWait:    $("modal-wait"),
  waitText:     $("wait-text"),
  waitBar:      $("wait-bar"),

  modalSuccess: $("modal-success"),
  successClose: $("success-close"),
};

/* =====================================================================
   ЗАГРУЗКА И РЕНДЕР ШАБЛОНОВ
   ===================================================================== */

/**
 * Загружает /api/templates и строит все динамические секции.
 * При ошибке показывает сообщение и блокирует покупку.
 */
async function loadTemplates() {
  try {
    const resp = await fetch(API.templates);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const tpl = await resp.json();

    state.maxLines = tpl.max_lines ?? 6;

    renderSizes(tpl.sizes);
    renderColors(tpl.colors);
    renderFonts(tpl.fonts);

    // Привязываем события после рендера
    bindSizes();
    bindSwatches(el.bgSwatches,  "bgColor",   "textColor");
    bindSwatches(el.txtSwatches, "textColor", "bgColor");
    bindFonts();

  } catch (e) {
    el.buyBtn.disabled = true;
    el.previewMeta.textContent = "Не удалось загрузить параметры шаблонов";
    console.error("loadTemplates:", e);
  }
}

/** Рендерит кнопки размеров. Первый размер становится активным. */
function renderSizes(sizes) {
  el.sizeGrid.innerHTML = "";
  sizes.forEach((s, i) => {
    const btn = document.createElement("button");
    btn.className = "size-btn" + (i === 0 ? " active" : "");
    btn.dataset.size = s.key;
    btn.innerHTML = `
      <span class="size-key">${escapeHtml(s.label)}</span>
      <span class="size-desc">${escapeHtml(formatDimensions(s.width_mm, s.height_mm))}</span>
    `;
    el.sizeGrid.appendChild(btn);
  });
  // Устанавливаем начальное значение
  if (sizes.length > 0) state.sizeKey = sizes[0].key;
}

/** Рендерит свотчи цветов в оба контейнера. */
function renderColors(colors) {
  // Сохраняем список имён для защиты от совпадения
  state.colorNames = colors.map((c) => c.name);

  [el.bgSwatches, el.txtSwatches].forEach((container, ci) => {
    container.innerHTML = "";
    colors.forEach((c, i) => {
      // Фон: первый цвет активен; Текст: второй цвет активен (или первый если один)
      const defaultIdx = ci === 0 ? 0 : Math.min(1, colors.length - 1);
      const btn = document.createElement("button");
      btn.className = "swatch" + (i === defaultIdx ? " active" : "");
      btn.dataset.color = c.name;
      btn.title = c.name;
      const rgb = `rgb(${c.rgb[0]},${c.rgb[1]},${c.rgb[2]})`;
      btn.style.background = rgb;
      // Белый свотч: видимая рамка
      if (c.name === "Белый") btn.style.borderColor = "var(--border)";
      // Цвет чекмарка: тёмный на светлых, белый на тёмных
      const bright = (c.rgb[0] * 299 + c.rgb[1] * 587 + c.rgb[2] * 114) / 1000;
      const checkColor = bright > 128 ? "#1a1a1a" : "#fff";
      btn.innerHTML = `<span class="swatch-check" style="color:${checkColor}">✓</span>`;
      container.appendChild(btn);
    });
    // Устанавливаем начальное значение в state
    const defaultIdx = ci === 0 ? 0 : Math.min(1, colors.length - 1);
    const field = ci === 0 ? "bgColor" : "textColor";
    if (colors.length > 0) state[field] = colors[defaultIdx].name;
  });
}

/** Рендерит кнопки шрифтов. Первый шрифт становится активным. */
function renderFonts(fonts) {
  el.fontList.innerHTML = "";
  fonts.forEach((name, i) => {
    const btn = document.createElement("button");
    btn.className = "font-btn" + (i === 0 ? " active" : "");
    btn.dataset.font = name;
    btn.innerHTML = `
      <span class="font-name">${escapeHtml(name)}</span>
      <span class="font-sample">Продажа 123-45-67</span>
      <span class="font-check">✓</span>
    `;
    el.fontList.appendChild(btn);
  });
  if (fonts.length > 0) state.font = fonts[0];
}

/** Форматирует размеры мм → "Стандарт" / "3×2 м" как подпись */
function formatDimensions(w, h) {
  return `${(w / 1000).toFixed(w % 1000 === 0 ? 0 : 1)}×${(h / 1000).toFixed(h % 1000 === 0 ? 0 : 1)} м`;
}

/* =====================================================================
   ЗАЩИТА ОТ СОВПАДЕНИЯ ЦВЕТОВ
   ===================================================================== */

/**
 * Возвращает контейнер свотчей по имени поля состояния.
 */
function swatchContainerFor(field) {
  return field === "bgColor" ? el.bgSwatches : el.txtSwatches;
}

/**
 * Принудительно активирует свотч с указанным именем цвета в контейнере.
 * Возвращает true если свотч найден и переключён.
 */
function activateSwatch(container, colorName) {
  const swatches = container.querySelectorAll(".swatch");
  for (const sw of swatches) {
    if (sw.dataset.color === colorName) {
      swatches.forEach((s) => s.classList.remove("active"));
      sw.classList.add("active");
      return true;
    }
  }
  return false;
}

/**
 * Проверяет совпадение bgColor и textColor.
 * Если совпадают — переключает oppositeField на первый доступный
 * отличный от выбранного цвет и обновляет UI.
 *
 * @param {string} chosenField   — поле, которое только что изменили ("bgColor" | "textColor")
 * @param {string} oppositeField — противоположное поле
 */
function resolveColorConflict(chosenField, oppositeField) {
  if (state.bgColor !== state.textColor) return; // конфликта нет

  // Ищем первый цвет, отличный от только что выбранного
  const chosen = state[chosenField];
  const fallback = state.colorNames.find((name) => name !== chosen);

  if (!fallback) return; // только один цвет в системе — защита невозможна

  state[oppositeField] = fallback;
  activateSwatch(swatchContainerFor(oppositeField), fallback);
}

/* =====================================================================
   ПРИВЯЗКА СОБЫТИЙ — РАЗМЕР
   ===================================================================== */
function bindSizes() {
  el.sizeGrid.addEventListener("click", (e) => {
    const btn = e.target.closest(".size-btn");
    if (!btn) return;
    el.sizeGrid.querySelectorAll(".size-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.sizeKey = btn.dataset.size;
    schedulePreview();
  });
}

/* =====================================================================
   ПРИВЯЗКА СОБЫТИЙ — ЦВЕТА
   ===================================================================== */

/**
 * @param {HTMLElement} container    — контейнер свотчей
 * @param {string}      field        — поле state ("bgColor" | "textColor")
 * @param {string}      oppositeField — противоположное поле для проверки конфликта
 */
function bindSwatches(container, field, oppositeField) {
  container.addEventListener("click", (e) => {
    const sw = e.target.closest(".swatch");
    if (!sw) return;
    container.querySelectorAll(".swatch").forEach((s) => s.classList.remove("active"));
    sw.classList.add("active");
    state[field] = sw.dataset.color;

    // Защита: если выбранный цвет совпал с противоположным — переключаем противоположный
    resolveColorConflict(field, oppositeField);

    schedulePreview();
  });
}

/* =====================================================================
   ПРИВЯЗКА СОБЫТИЙ — ШРИФТ
   ===================================================================== */
function bindFonts() {
  el.fontList.addEventListener("click", (e) => {
    const btn = e.target.closest(".font-btn");
    if (!btn) return;
    el.fontList.querySelectorAll(".font-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.font = btn.dataset.font;
    schedulePreview();
  });
}

/* =====================================================================
   ПРЕВЬЮ
   ===================================================================== */
let _previewTimer = null;

function schedulePreview() {
  clearTimeout(_previewTimer);
  _previewTimer = setTimeout(fetchPreview, PREVIEW_DEBOUNCE_MS);
}

async function fetchPreview() {
  const lines = getTextLines();
  if (lines.length === 0) {
    showPlaceholder();
    return;
  }

  showLoader();

  try {
    const resp = await fetch(API.preview, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildConfig()),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `Ошибка сервера ${resp.status}`);
    }

    const data = await resp.json();
    showPreview(data.preview_base64, data.width_mm, data.height_mm);
  } catch (e) {
    hideLoader();
    el.previewMeta.textContent = "Не удалось загрузить превью";
  }
}

function showPlaceholder() {
  el.previewImg.classList.add("hidden");
  el.previewPlaceholder.classList.remove("hidden");
  el.previewLoader.classList.add("hidden");
  el.previewMeta.textContent = "";
}

function showLoader() {
  el.previewPlaceholder.classList.add("hidden");
  el.previewLoader.classList.remove("hidden");
}

function hideLoader() {
  el.previewLoader.classList.add("hidden");
}

function showPreview(base64, widthMm, heightMm) {
  el.previewImg.src = `data:image/jpeg;base64,${base64}`;
  el.previewImg.classList.remove("hidden");
  el.previewPlaceholder.classList.add("hidden");
  el.previewLoader.classList.add("hidden");
  el.previewMeta.textContent = `${(widthMm/1000).toFixed(1)} × ${(heightMm/1000).toFixed(1)} м · CMYK для типографии`;
}

/* =====================================================================
   СБОРКА КОНФИГА
   ===================================================================== */
function getTextLines() {
  return state.lines
    .map((t) => t.trim())
    .filter((t) => t.length > 0)
    .map((t) => ({ text: t, scale: 1.0 }));
}

function buildConfig() {
  return {
    size_key:   state.sizeKey,
    bg_color:   state.bgColor,
    text_color: state.textColor,
    font:       state.font,
    text_lines: getTextLines(),
    ref_code:   state.refCode || undefined,
  };
}

/* =====================================================================
   ПРИВЯЗКА СОБЫТИЙ — ТЕКСТ
   ===================================================================== */
function renderTextLines() {
  el.textLines.innerHTML = "";

  state.lines.forEach((text, i) => {
    const row = document.createElement("div");
    row.className = "text-line-row";
    row.dataset.index = i;
    row.innerHTML = `
      <span class="line-num">${i + 1}</span>
      <input class="line-input" type="text"
             placeholder="Строка ${i + 1}..."
             maxlength="120"
             value="${escapeHtml(text)}">
      <button class="remove-line-btn" title="Удалить строку">×</button>
    `;
    const input = row.querySelector("input");
    input.addEventListener("input", () => {
      state.lines[i] = input.value;
      schedulePreview();
    });
    row.querySelector(".remove-line-btn").addEventListener("click", () => {
      if (state.lines.length <= 1) return;
      state.lines.splice(i, 1);
      renderTextLines();
      schedulePreview();
    });
    el.textLines.appendChild(row);
  });

  el.addLineBtn.disabled = state.lines.length >= state.maxLines;
}

el.addLineBtn.addEventListener("click", () => {
  if (state.lines.length >= state.maxLines) return;
  state.lines.push("");
  renderTextLines();
});

/* =====================================================================
   РЕФЕРАЛЬНЫЙ КОД — валидация на blur
   ===================================================================== */
let _refTimer = null;

el.refInput.addEventListener("input", () => {
  const val = el.refInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
  el.refInput.value = val;
  state.refCode = val;
  el.refStatus.textContent = "";
  el.refStatus.className = "ref-status";

  clearTimeout(_refTimer);
  if (val.length === 8) {
    _refTimer = setTimeout(validateRefCode, 600);
  }
});

async function validateRefCode() {
  const code = state.refCode;
  if (code.length !== 8) return;
  try {
    const resp = await fetch(API.refStats(code));
    if (resp.ok) {
      el.refStatus.textContent = "✓";
      el.refStatus.className = "ref-status ok";
    } else {
      el.refStatus.textContent = "—";
      el.refStatus.className = "ref-status err";
    }
  } catch {
    el.refStatus.textContent = "";
  }
}

/* =====================================================================
   ПОКУПКА — создание заказа
   ===================================================================== */
el.buyBtn.addEventListener("click", async () => {
  if (getTextLines().length === 0) {
    showError("Введите текст", "Добавьте хотя бы одну строку текста для баннера.");
    return;
  }

  el.buyBtn.disabled = true;
  el.buyBtn.textContent = "Создаём заказ...";

  try {
    const resp = await fetch(API.order, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildConfig()),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `Ошибка сервера ${resp.status}`);
    }

    const data = await resp.json();
    state.orderId = data.order_id;
    state.payUrl  = data.pay_url;

    showModal(el.modalPayment);
  } catch (e) {
    showError("Не удалось создать заказ", e.message);
  } finally {
    el.buyBtn.disabled = false;
    el.buyBtn.textContent = "Получить PDF";
  }
});

/* =====================================================================
   ОПЛАТА — переход и поллинг
   ===================================================================== */
el.payBtn.addEventListener("click", () => {
  hideModal(el.modalPayment);
  window.open(state.payUrl, "_blank");
  startPolling();
});

el.payCancel.addEventListener("click", () => {
  hideModal(el.modalPayment);
  state.orderId = null;
  state.payUrl  = null;
});

function startPolling() {
  showModal(el.modalWait);
  el.waitText.textContent = "Ожидаем подтверждение оплаты...";
  el.waitBar.style.width = "0%";

  let attempt = 0;

  const tick = async () => {
    attempt++;
    const progress = Math.min(95, (attempt / POLL_MAX_ATTEMPTS) * 100);
    el.waitBar.style.width = `${progress}%`;

    if (attempt > POLL_MAX_ATTEMPTS) {
      hideModal(el.modalWait);
      showError(
        "Время ожидания истекло",
        "Не получили подтверждение оплаты. Если деньги списаны — напишите нам."
      );
      return;
    }

    try {
      const resp = await fetch(API.status(state.orderId));
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const data = await resp.json();

      if (data.status === "token_issued" && data.download_token) {
        el.waitText.textContent = "Формируем PDF...";
        el.waitBar.style.width = "100%";
        setTimeout(() => downloadPdf(data.download_token), 500);
        return;
      }

      if (data.status === "expired") {
        hideModal(el.modalWait);
        showError("Заказ истёк", "Заказ устарел. Попробуйте снова.");
        return;
      }

      if (data.status === "paid") {
        el.waitText.textContent = "Оплата подтверждена, формируем файл...";
      }
    } catch {
      // Временные сетевые ошибки — продолжаем поллинг
    }

    setTimeout(tick, POLL_INTERVAL_MS);
  };

  setTimeout(tick, POLL_INTERVAL_MS);
}

/* =====================================================================
   СКАЧИВАНИЕ PDF
   ===================================================================== */
async function downloadPdf(token) {
  try {
    const resp = await fetch(API.download(token));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = `banner_${state.sizeKey}_${Date.now()}.pdf`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      URL.revokeObjectURL(url);
      a.remove();
    }, 1000);

    hideModal(el.modalWait);
    showModal(el.modalSuccess);
  } catch (e) {
    hideModal(el.modalWait);
    showError("Ошибка скачивания", `Не удалось скачать файл: ${e.message}`);
  }
}

el.successClose.addEventListener("click", () => {
  hideModal(el.modalSuccess);
  state.orderId = null;
  state.payUrl  = null;
});

/* =====================================================================
   МОДАЛКИ — хелперы
   ===================================================================== */
function showModal(overlay) {
  overlay.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function hideModal(overlay) {
  overlay.classList.add("hidden");
  document.body.style.overflow = "";
}

function showError(title, text) {
  el.errorTitle.textContent = title;
  el.errorText.textContent  = text;
  showModal(el.modalError);
}

el.errorClose.addEventListener("click", () => hideModal(el.modalError));

el.modalError.addEventListener("click", (e) => {
  if (e.target === el.modalError) hideModal(el.modalError);
});

/* =====================================================================
   УТИЛИТЫ
   ===================================================================== */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* =====================================================================
   ИНИЦИАЛИЗАЦИЯ
   ===================================================================== */
async function init() {
  renderTextLines();
  await loadTemplates();
  // Первое превью не запускаем — поля пустые
}

init();
