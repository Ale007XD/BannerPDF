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

const PREVIEW_DEBOUNCE_MS  = 500;
const POLL_INTERVAL_MS     = 2500;
const POLL_MAX_ATTEMPTS    = 80;    // ~200 сек максимум
const CUSTOM_SIZE_MIN      = 100;   // мм
const CUSTOM_SIZE_MAX      = 3000;  // мм

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

  // Кастомный размер (мм, null = не задан)
  customW:   null,
  customH:   null,

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

  // Кастомный размер
  customW:         $("custom-w"),
  customH:         $("custom-h"),
  customSizeHint:  $("custom-size-hint"),

  // FAB + Bottom sheet (мобиле)
  fabPreview:   $("fab-preview"),
  bsOverlay:    $("bs-overlay"),
  bsClose:      $("bs-close"),
  bsPlaceholder:$("bs-placeholder"),
  bsPreviewImg: $("bs-preview-img"),
  bsLoader:     $("bs-loader"),
  bsMeta:       $("bs-meta"),
  bsBuyBtn:     $("bs-buy-btn"),

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
    bindCustomSize();

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

/** Форматирует размеры мм → "3×2 м" как подпись */
function formatDimensions(w, h) {
  return `${(w / 1000).toFixed(w % 1000 === 0 ? 0 : 1)}×${(h / 1000).toFixed(h % 1000 === 0 ? 0 : 1)} м`;
}

/* =====================================================================
   ЗАЩИТА ОТ СОВПАДЕНИЯ ЦВЕТОВ
   ===================================================================== */

/** Возвращает контейнер свотчей по имени поля состояния. */
function swatchContainerFor(field) {
  return field === "bgColor" ? el.bgSwatches : el.txtSwatches;
}

/**
 * Принудительно активирует свотч с указанным именем цвета в контейнере.
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
 */
function resolveColorConflict(chosenField, oppositeField) {
  if (state.bgColor !== state.textColor) return;

  const chosen = state[chosenField];
  const fallback = state.colorNames.find((name) => name !== chosen);

  if (!fallback) return;

  state[oppositeField] = fallback;
  activateSwatch(swatchContainerFor(oppositeField), fallback);
}

/* =====================================================================
   КАСТОМНЫЙ РАЗМЕР
   ===================================================================== */

/**
 * Разбирает значение инпута и возвращает целое число в диапазоне
 * [CUSTOM_SIZE_MIN, CUSTOM_SIZE_MAX] или null если невалидно.
 */
function parseCustomDim(value) {
  const n = parseInt(value, 10);
  if (isNaN(n) || n < CUSTOM_SIZE_MIN || n > CUSTOM_SIZE_MAX) return null;
  return n;
}

/**
 * Обновляет state.customW / customH из инпутов,
 * показывает/скрывает подсказку с ошибкой,
 * переключает sizeKey на "custom" если оба поля валидны
 * или возвращает к первой типовой кнопке если оба пустые.
 */
function handleCustomSizeInput() {
  const wRaw = el.customW.value.trim();
  const hRaw = el.customH.value.trim();

  // Оба пустые → выходим из режима custom, не трогаем активную кнопку
  if (wRaw === "" && hRaw === "") {
    el.customW.classList.remove("active-custom", "error");
    el.customH.classList.remove("active-custom", "error");
    setCustomHint("100–3000 мм по каждой стороне", false);

    // Если до этого был выбран custom — снимаем, возвращаем первую кнопку
    if (state.sizeKey === "custom") {
      state.sizeKey  = null;
      state.customW  = null;
      state.customH  = null;
      const firstBtn = el.sizeGrid.querySelector(".size-btn");
      if (firstBtn) {
        firstBtn.classList.add("active");
        state.sizeKey = firstBtn.dataset.size;
      }
      schedulePreview();
    }
    return;
  }

  const w = parseCustomDim(wRaw);
  const h = parseCustomDim(hRaw);

  const wErr = wRaw !== "" && w === null;
  const hErr = hRaw !== "" && h === null;

  el.customW.classList.toggle("error", wErr);
  el.customH.classList.toggle("error", hErr);

  if (wErr || hErr) {
    setCustomHint(`Введите целое число от ${CUSTOM_SIZE_MIN} до ${CUSTOM_SIZE_MAX}`, true);
    return;
  }

  // Одно из полей ещё не заполнено — ждём
  if (w === null || h === null) {
    setCustomHint("Заполните оба поля", false);
    return;
  }

  // Оба валидны — активируем режим custom
  state.customW  = w;
  state.customH  = h;
  state.sizeKey  = "custom";

  el.customW.classList.add("active-custom");
  el.customH.classList.add("active-custom");
  el.customW.classList.remove("error");
  el.customH.classList.remove("error");

  // Снимаем активность со всех типовых кнопок
  el.sizeGrid.querySelectorAll(".size-btn").forEach((b) => b.classList.remove("active"));

  setCustomHint(`${w} × ${h} мм`, false);
  schedulePreview();
}

function setCustomHint(text, isError) {
  el.customSizeHint.textContent = text;
  el.customSizeHint.classList.toggle("error", isError);
}

/** Привязывает события к инпутам кастомного размера. */
function bindCustomSize() {
  [el.customW, el.customH].forEach((input) => {
    input.addEventListener("input", handleCustomSizeInput);
    // На blur убираем .active-custom если поле пустое
    input.addEventListener("blur", () => {
      if (input.value.trim() === "") {
        input.classList.remove("active-custom", "error");
      }
    });
  });
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

    // Сброс кастомного размера при выборе типовой кнопки
    state.customW = null;
    state.customH = null;
    el.customW.value = "";
    el.customH.value = "";
    el.customW.classList.remove("active-custom", "error");
    el.customH.classList.remove("active-custom", "error");
    setCustomHint("100–3000 мм по каждой стороне", false);

    schedulePreview();
  });
}

/* =====================================================================
   ПРИВЯЗКА СОБЫТИЙ — ЦВЕТА
   ===================================================================== */

/**
 * @param {HTMLElement} container     — контейнер свотчей
 * @param {string}      field         — поле state ("bgColor" | "textColor")
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

  // Кастомный режим: оба размера должны быть валидны
  if (state.sizeKey === "custom" && (state.customW === null || state.customH === null)) {
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
  syncBottomSheetPreview();
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
  // Обновляем bottom sheet если он сейчас открыт
  syncBottomSheetPreview();
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

/**
 * Собирает конфиг для /api/preview и /api/order.
 * При кастомном размере передаёт width_mm / height_mm напрямую
 * вместо size_key.
 */
function buildConfig() {
  const base = {
    bg_color:   state.bgColor,
    text_color: state.textColor,
    font:       state.font,
    text_lines: getTextLines(),
    ref_code:   state.refCode || undefined,
  };

  if (state.sizeKey === "custom") {
    return { ...base, width_mm: state.customW, height_mm: state.customH };
  }

  return { ...base, size_key: state.sizeKey };
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
   FAB + BOTTOM SHEET — превью на мобиле
   ===================================================================== */

function openBottomSheet() {
  el.bsOverlay.classList.add("open");
  document.body.style.overflow = "hidden";
  // Синхронизируем состояние превью из основного блока
  syncBottomSheetPreview();
}

function closeBottomSheet() {
  el.bsOverlay.classList.remove("open");
  document.body.style.overflow = "";
}

/**
 * Копирует текущее состояние превью (img, placeholder, loader, meta)
 * в bottom sheet. Вызывается при открытии и после каждого fetchPreview.
 */
function syncBottomSheetPreview() {
  const mainImg = el.previewImg;
  const mainHidden = mainImg.classList.contains("hidden");

  if (!mainHidden && mainImg.src) {
    // Есть готовое превью
    el.bsPreviewImg.src = mainImg.src;
    el.bsPreviewImg.classList.remove("hidden");
    el.bsPlaceholder.classList.add("hidden");
    el.bsLoader.classList.add("hidden");
  } else if (!el.previewLoader.classList.contains("hidden")) {
    // Идёт загрузка
    el.bsPreviewImg.classList.add("hidden");
    el.bsPlaceholder.classList.add("hidden");
    el.bsLoader.classList.remove("hidden");
  } else {
    // Placeholder
    el.bsPreviewImg.classList.add("hidden");
    el.bsPlaceholder.classList.remove("hidden");
    el.bsLoader.classList.add("hidden");
  }
  el.bsMeta.textContent = el.previewMeta.textContent;
}

// События FAB и bottom sheet
el.fabPreview.addEventListener("click", openBottomSheet);
el.bsClose.addEventListener("click", closeBottomSheet);

// Тап по оверлею (мимо шита) — закрыть
el.bsOverlay.addEventListener("click", (e) => {
  if (e.target === el.bsOverlay) closeBottomSheet();
});

// Кнопка «Получить PDF» внутри bottom sheet — дублирует основную
el.bsBuyBtn.addEventListener("click", () => {
  closeBottomSheet();
  // Небольшая задержка чтобы sheet успел закрыться до модалки
  setTimeout(() => el.buyBtn.click(), 150);
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

  // Валидация кастомного размера перед заказом
  if (state.sizeKey === "custom") {
    if (state.customW === null || state.customH === null) {
      showError("Укажите размер", `Введите ширину и высоту от ${CUSTOM_SIZE_MIN} до ${CUSTOM_SIZE_MAX} мм.`);
      el.customW.focus();
      return;
    }
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
    a.download = `banner_${state.sizeKey === "custom"
      ? `${state.customW}x${state.customH}mm`
      : state.sizeKey}_${Date.now()}.pdf`;
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
