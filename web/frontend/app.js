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
  amend:     (id) => `/api/order/${id}/amend`,
  status:    (id) => `/api/payment/status/${id}`,
  download:  (token) => `/api/download/${token}`,
  refStats:  (code) => `/api/referral/stats/${code}`,
};

const PREVIEW_DEBOUNCE_MS  = 500;
const POLL_INTERVAL_MS     = 2500;
const POLL_MAX_ATTEMPTS    = 240;   // ~600 сек (10 мин) — время ручной обработки
const CUSTOM_SIZE_MIN      = 100;   // мм
const CUSTOM_SIZE_MAX      = 3000;  // мм

// Реферальная программа: false — блок скрыт, true — показан.
// Чтобы включить: 1) поменять false → true здесь, 2) инкрементировать ?v= в index.html (строка с app.js).
const REFERRAL_ENABLED = false;

// Текст кнопки покупки — один источник правды (используется в finally)
const BUY_BTN_TEXT = "Получить PDF";

// Контакты для ручной оплаты
const CONTACT_TG = "ale007xd";

/* =====================================================================
   СОСТОЯНИЕ
   ===================================================================== */
const state = {
  sizeKey:   null,   // задаётся после загрузки шаблонов (первый размер)
  bgColor:   null,   // задаётся после загрузки шаблонов (первый цвет)
  textColor: null,   // задаётся после загрузки шаблонов (второй цвет)
  font:      null,   // задаётся после загрузки шаблонов (первый шрифт)
  lines:     [{ text: "", scale: 1.0 }, { text: "", scale: 1.0 }],   // до max_lines строк
  maxLines:  6,          // обновляется из шаблонов
  refCode:   "",
  promoCode: "",        // промокод (сбрасывается после использования)

  // Кастомный размер (мм, null = не задан)
  customW:   null,
  customH:   null,

  // Оплата
  orderId:   null,
  amendUsed: false,   // бесплатная правка уже использована в этой сессии
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
  promoInput:  $("promo-input"),
  promoStatus: $("promo-status"),
  buyBtn:      $("buy-btn"),

  // Кастомный размер
  customW:         $("custom-w"),
  customH:         $("custom-h"),
  customSizeHint:  $("custom-size-hint"),

  // Sticky-бар превью (мобиле)
  previewStickyBtn: $("preview-inline-btn"),
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
  errorRetry:   $("error-retry"),

  amendOpen:    $("amend-open"),
  openAmend:    $("open-amend"),
  modalAmend:   $("modal-amend"),
  amendFields:  $("amend-fields"),
  amendError:   $("amend-error"),
  amendSubmit:  $("amend-submit"),
  amendCancel:  $("amend-cancel"),

  modalWait:    $("modal-wait"),
  waitText:     $("wait-text"),
  waitBar:      $("wait-bar"),

  modalConfirm:     $("modal-confirm"),
  confirmLines:     $("confirm-lines"),
  confirmMeta:      $("confirm-meta"),
  confirmCheck:     $("confirm-check"),
  confirmPay:       $("confirm-pay"),
  confirmBack:      $("confirm-back"),
  confirmOpenTerms: $("confirm-open-terms"),

  modalPay:     $("modal-pay"),
  payClose:     $("pay-close"),

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
    syncBuyButtons();
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

// Маппинг имён шрифтов из шаблона → CSS font-family (Google Fonts)
const FONT_CSS_MAP = {
  "Golos Text":     "'Golos Text', sans-serif",
  "Tenor Sans":     "'Tenor Sans', serif",
  "Fira Sans Cond": "'Fira Sans Condensed', sans-serif",
  "PT Sans Narrow": "'PT Sans Narrow', sans-serif",
};

/** Рендерит кнопки шрифтов. Первый шрифт становится активным. */
function renderFonts(fonts) {
  el.fontList.innerHTML = "";
  fonts.forEach((name, i) => {
    const btn = document.createElement("button");
    btn.className = "font-btn" + (i === 0 ? " active" : "");
    btn.dataset.font = name;
    const cssFontFamily = FONT_CSS_MAP[name] || "sans-serif";
    btn.innerHTML = `
      <span class="font-name">${escapeHtml(name)}</span>
      <span class="font-sample" style="font-family:${cssFontFamily}">${escapeHtml("Продажа 123-45-67")}</span>
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
    if (typeof ym !== 'undefined') ym(108388194, 'reachGoal', 'preview_generated');
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
    .filter((l) => l.text.trim().length > 0)
    .map((l) => ({ text: l.text.trim(), scale: Math.round(l.scale * 100) }));
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
    ref_code:   state.refCode   || undefined,
    promo_code: state.promoCode || undefined,
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

  state.lines.forEach((line, i) => {
    const row = document.createElement("div");
    row.className = "text-line-row";
    row.dataset.index = i;

    const scalePct = Math.round(line.scale * 100);

    row.innerHTML = `
      <span class="line-num">${i + 1}</span>
      <div class="line-body">
        <input class="line-input" type="text"
               placeholder="${i === 0 ? 'ВАША РЕКЛАМА' : i === 1 ? 'Телефон или адрес' : 'Строка ' + (i + 1)}"
               maxlength="120"
               value="${escapeHtml(line.text)}">
        <div class="line-scale-row">
          <button class="scale-btn scale-minus" data-index="${i}" title="Уменьшить ширину строки"${scalePct <= 50 ? " disabled" : ""}>−</button>
          <span class="scale-label${scalePct < 100 ? " scaled" : ""}">${scalePct}%</span>
          <button class="scale-btn scale-plus" data-index="${i}" title="Увеличить ширину строки"${scalePct >= 100 ? " disabled" : ""}>+</button>
        </div>
      </div>
      <button class="remove-line-btn" title="Удалить строку">×</button>
    `;

    const input = row.querySelector("input");
    input.addEventListener("input", () => {
      state.lines[i].text = input.value;
      schedulePreview();
    });

    row.querySelector(".remove-line-btn").addEventListener("click", () => {
      if (state.lines.length <= 1) return;
      state.lines.splice(i, 1);
      renderTextLines();
      schedulePreview();
    });

    row.querySelector(".scale-minus").addEventListener("click", () => {
      const cur = Math.round(state.lines[i].scale * 100);
      if (cur <= 50) return;
      state.lines[i].scale = (cur - 10) / 100;
      renderTextLines();
      schedulePreview();
    });

    row.querySelector(".scale-plus").addEventListener("click", () => {
      const cur = Math.round(state.lines[i].scale * 100);
      if (cur >= 100) return;
      state.lines[i].scale = (cur + 10) / 100;
      renderTextLines();
      schedulePreview();
    });

    el.textLines.appendChild(row);
  });

  el.addLineBtn.disabled = state.lines.length >= state.maxLines;
}

el.addLineBtn.addEventListener("click", () => {
  if (state.lines.length >= state.maxLines) return;
  state.lines.push({ text: "", scale: 1.0 });
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
  syncBuyButtons();
}

/** Синхронизирует disabled кнопки покупки между main и bottom sheet. */
function syncBuyButtons() {
  el.bsBuyBtn.disabled = el.buyBtn.disabled;
}

// События sticky-bar и bottom sheet
el.previewStickyBtn.addEventListener("click", openBottomSheet);
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
   ПРОМОКОД
   ===================================================================== */
if (el.promoInput) {
  el.promoInput.addEventListener("input", () => {
    const val = el.promoInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
    el.promoInput.value = val;
    state.promoCode = val;

    if (!val) {
      if (el.promoStatus) {
        el.promoStatus.textContent = "";
        el.promoStatus.className = "promo-status";
      }
      el.buyBtn.textContent = BUY_BTN_TEXT;
      syncBuyButtons();
      return;
    }
    if (val.length < 2) return;

    if (el.promoStatus) {
      el.promoStatus.textContent = "Промокод будет применён при оформлении";
      el.promoStatus.className = "promo-status promo-status--ok";
    }
    el.buyBtn.textContent = "Получить PDF бесплатно";
    syncBuyButtons();
  });
}

/* =====================================================================
   ПОКУПКА — экран проверки макета → создание заказа
   ===================================================================== */

// Снимок конфига, показанный на экране проверки. Заказ создаётся именно по нему:
// то, что пользователь подтвердил, и то, что оплачено, не могут разойтись.
let pendingConfig = null;

el.buyBtn.addEventListener("click", () => {
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

  openConfirm();
});

/** Добавляет строку «название — значение» в блок параметров экрана проверки. */
function addConfirmMeta(label, value) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  el.confirmMeta.append(dt, dd);
}

/** Показывает итоговый текст и параметры макета; оплата доступна только после отметки. */
function openConfirm() {
  pendingConfig = buildConfig();
  loadYooKassaSdk().catch(() => {});   // если при загрузке страницы не вышло — пробуем заранее

  // Текст — только через textContent (ввод пользователя, не HTML)
  el.confirmLines.replaceChildren(
    ...pendingConfig.text_lines.map((line) => {
      const div = document.createElement("div");
      div.className = "confirm-line";
      div.textContent = line.text;
      return div;
    })
  );

  const activeSize = el.sizeGrid.querySelector(".size-btn.active");
  const sizeLabel = state.sizeKey === "custom"
    ? `${state.customW}×${state.customH} мм`
    : (activeSize
        ? activeSize.querySelector(".size-desc").textContent   // «3×2 м», считается из размеров
        : String(state.sizeKey));

  el.confirmMeta.replaceChildren();
  addConfirmMeta("Размер", sizeLabel);
  addConfirmMeta("Шрифт", state.font);
  addConfirmMeta("Фон", state.bgColor);
  addConfirmMeta("Цвет текста", state.textColor);

  el.confirmCheck.checked = false;
  el.confirmPay.disabled = true;
  showModal(el.modalConfirm);
}

el.confirmCheck.addEventListener("change", () => {
  el.confirmPay.disabled = !el.confirmCheck.checked;
});

el.confirmBack.addEventListener("click", () => hideModal(el.modalConfirm));

// Соглашение открывается поверх экрана проверки (оно позже в DOM)
el.confirmOpenTerms.addEventListener("click", () => openModal("modal-terms"));

el.confirmPay.addEventListener("click", () => {
  if (!el.confirmCheck.checked || !pendingConfig) return;
  hideModal(el.modalConfirm);
  createOrder({ ...pendingConfig, accept_terms: true });
});

async function createOrder(payload) {
  const attempt = ++payAttempt;
  el.buyBtn.disabled = true;
  el.buyBtn.textContent = "Создаём заказ...";
  syncBuyButtons();
  if (typeof ym !== 'undefined') ym(108388194, 'reachGoal', 'start_order');

  // Окно оплаты с индикатором — сразу: пока сервер создаёт платёж в ЮKassa, пользователь
  // видит, что процесс идёт (раньше был виден только текст на кнопке под затемнением).
  showPayLoading();

  try {
    const resp = await fetch(API.order, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `Ошибка сервера ${resp.status}`);
    }

    const data = await resp.json();
    state.orderId = data.order_id;

    // Бесплатный заказ (промокод 100% скидки) — пропускаем ЮКасса
    if (data.free && data.download_token) {
      hideModal(el.modalPay);
      state.promoCode = "";
      if (el.promoInput)  { el.promoInput.value  = ""; }
      if (el.promoStatus) { el.promoStatus.textContent = ""; el.promoStatus.className = "promo-status"; }
      // «Готово!» показывает downloadPdf только после успешной выдачи файла;
      // раньше окно успеха перекрывало ошибку скачивания.
      showModal(el.modalWait);
      el.waitText.textContent = "Формируем PDF...";
      el.waitBar.style.width = "100%";
      setTimeout(() => downloadPdf(data.download_token), 300);
      return;
    }

    // Окно закрыли, пока создавался заказ: заказ остаётся неоплаченным, виджет не нужен
    if (attempt !== payAttempt) return;

    // Инициализируем виджет ЮKassa с полученным confirmation_token
    await openYooKassaWidget(data.confirmation_token, data.order_id, attempt);
  } catch (e) {
    if (attempt === payAttempt) {
      hideModal(el.modalPay);
      showError("Не удалось создать заказ", e.message);
    }
  } finally {
    el.buyBtn.disabled = false;
    el.buyBtn.textContent = BUY_BTN_TEXT;
    syncBuyButtons();
  }
}

/* =====================================================================
   ОПЛАТА — виджет ЮKassa
   ===================================================================== */

const YK_SDK_URL = "https://yookassa.ru/checkout-widget/v1/checkout-widget.js";
let ykSdkPromise = null;

/** Номер попытки оплаты: закрытие окна во время создания заказа отменяет показ виджета. */
let payAttempt = 0;

/**
 * Подгружает SDK виджета ЮKassa один раз и не блокирует страницу.
 * Раньше он стоял синхронным <script> перед app.js: конструктор не оживал,
 * пока yookassa.ru не отдаст файл. После сбоя следующий вызов пробует заново.
 */
function loadYooKassaSdk() {
  if (window.YooMoneyCheckoutWidget) return Promise.resolve();
  if (!ykSdkPromise) {
    ykSdkPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = YK_SDK_URL;
      script.async = true;
      const fail = (msg) => { script.remove(); ykSdkPromise = null; reject(new Error(msg)); };
      script.onload = () => (window.YooMoneyCheckoutWidget
        ? resolve()
        : fail("SDK ЮKassa не инициализировался"));
      script.onerror = () => fail("SDK ЮKassa не загрузился");
      document.head.appendChild(script);
    });
  }
  return ykSdkPromise;
}

// Грузим SDK заранее, как только страница готова, и ещё раз при открытии экрана проверки
window.addEventListener("load", () => loadYooKassaSdk().catch(() => {}));

/** Окно «Оплата» с индикатором: показывается сразу после нажатия «Оплатить». */
function showPayLoading(text = "Готовим оплату…") {
  const loader = document.createElement("div");
  loader.className = "pay-loading";
  const spinner = document.createElement("div");
  spinner.className = "wait-spinner";
  const label = document.createElement("div");
  label.className = "modal-text";
  label.textContent = text;
  loader.append(spinner, label);

  document.getElementById("yookassa-widget-container").replaceChildren(loader);
  el.payClose.onclick = () => {
    payAttempt++;
    hideModal(el.modalPay);
  };
  showModal(el.modalPay);
}

/**
 * Открывает виджет ЮKassa (YooMoneyCheckoutWidget) с переданным токеном.
 * После успешной оплаты виджет вызывает onSuccess → запускаем поллинг.
 * После закрытия без оплаты (onClose) — ничего не делаем, пользователь
 * может нажать «Получить PDF» снова.
 * Индикатор остаётся, пока не загрузится iframe с формой оплаты.
 *
 * @param {string} confirmationToken — токен из POST /api/order
 * @param {string} orderId           — наш UUID заказа
 * @param {number} attempt           — номер попытки (см. payAttempt)
 */
async function openYooKassaWidget(confirmationToken, orderId, attempt) {
  try {
    await loadYooKassaSdk();
  } catch (e) {
    console.error(e);
    if (attempt === payAttempt) {
      hideModal(el.modalPay);
      showError(
        "Не удалось открыть форму оплаты",
        "Проверьте соединение и отключите блокировщик рекламы для этого сайта, затем попробуйте ещё раз."
      );
    }
    return;
  }
  if (attempt !== payAttempt) return;   // окно закрыли, пока грузился SDK

  const container = document.getElementById("yookassa-widget-container");
  const loader = container.querySelector(".pay-loading");
  if (loader) loader.querySelector(".modal-text").textContent = "Загружаем форму оплаты…";

  // Чистим контейнер от предыдущих iframe: форма живёт в своём блоке рядом с индикатором
  const form = document.createElement("div");
  form.id = "yookassa-widget-form";
  container.replaceChildren(...(loader ? [loader] : []), form);

  const hideLoader = () => { if (loader) loader.remove(); };
  const watcher = new MutationObserver(() => {
    const frame = form.querySelector("iframe");
    if (frame) {
      watcher.disconnect();
      frame.addEventListener("load", hideLoader, { once: true });
    }
  });
  watcher.observe(form, { childList: true, subtree: true });
  setTimeout(() => { watcher.disconnect(); hideLoader(); }, 10000);   // страховка

  const checkout = new window.YooMoneyCheckoutWidget({
    confirmation_token: confirmationToken,
    customization: {
      colors: {
        control_primary: "#0d0d0d",
      },
    },
    error_callback: (err) => {
      console.error("ЮKassa widget error:", err);
      hideModal(el.modalPay);
      showError("Ошибка виджета оплаты", "Попробуйте ещё раз или напишите нам.");
    },
  });

  checkout.on("success", () => {
    checkout.destroy();
    hideModal(el.modalPay);
    showModal(el.modalWait);
    startPolling();
  });

  checkout.on("fail", () => {
    console.warn("ЮKassa: платёж отклонён для заказа", orderId);
    checkout.destroy();
    hideModal(el.modalPay);
  });

  // Кнопка закрытия модалки
  el.payClose.onclick = () => {
    payAttempt++;
    checkout.destroy();
    hideModal(el.modalPay);
  };

  checkout.render("yookassa-widget-form");
  if (typeof ym !== 'undefined') ym(108388194, 'reachGoal', 'payment_started');
}

// Если пользователь вернулся на страницу с ?order_id= в URL (после redirect),
// автоматически открываем ожидание и запускаем поллинг
(function checkReturnFromPayment() {
  const params = new URLSearchParams(window.location.search);
  const returnOrderId = params.get("order_id");
  if (returnOrderId) {
    state.orderId = returnOrderId;
    // Убираем параметр из URL без перезагрузки
    const cleanUrl = window.location.pathname;
    window.history.replaceState({}, "", cleanUrl);
    showModal(el.modalWait);
    startPolling();
  }
})();

function startPolling(silent = false) {
  if (!silent) showModal(el.modalWait);
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
    if (!resp.ok) {
      const err = new Error(`HTTP ${resp.status}`);
      err.status = resp.status;
      throw err;
    }

    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = `banner_${state.sizeKey === "custom"
      ? `${state.customW}x${state.customH}mm`
      : state.sizeKey}_${Date.now()}.pdf`;
    document.body.appendChild(a);
    a.click();
    if (typeof ym !== 'undefined') ym(108388194, 'reachGoal', 'download_success');
    setTimeout(() => {
      URL.revokeObjectURL(url);
      a.remove();
    }, 1000);

    hideModal(el.modalWait);
    rememberPaidOrder();
    el.amendOpen.classList.toggle("hidden", !state.orderId || state.amendUsed);
    showModal(el.modalSuccess);
  } catch (e) {
    hideModal(el.modalWait);

    // 404: ссылка истекла или лимит скачиваний исчерпан — повторять бесполезно
    if (e.status === 404) {
      showError(
        "Ссылка больше не действует",
        "Время действия ссылки (15 минут) или лимит скачиваний исчерпаны. " +
        "Если заказ оплачен — напишите нам (контакты на странице «Реквизиты и оплата»), " +
        "мы повторно выдадим файл."
      );
      return;
    }

    // Сбой генерации или сети: сервер не расходует ссылку — можно повторить
    showError(
      "Не удалось скачать файл",
      `${e.message}. Оплата не потеряна: ссылка действует 15 минут, нажмите «Скачать ещё раз».`,
      () => {
        hideModal(el.modalError);
        showModal(el.modalWait);
        el.waitText.textContent = "Формируем PDF...";
        downloadPdf(token);
      }
    );
  }
}

/* =====================================================================
   БЕСПЛАТНАЯ ПРАВКА ТЕКСТА (1 раз, 24 ч после оплаты; лимиты проверяет сервер)
   ===================================================================== */
const AMEND_STORAGE_KEY = "bp_last_order";
const AMEND_WINDOW_MS   = 24 * 60 * 60 * 1000;

let amendOrderId = null;

function readPaidOrder() {
  try {
    const raw = localStorage.getItem(AMEND_STORAGE_KEY);
    if (!raw) return null;
    const rec = JSON.parse(raw);
    if (!rec.id || Date.now() - rec.ts > AMEND_WINDOW_MS) {
      localStorage.removeItem(AMEND_STORAGE_KEY);
      return null;
    }
    return rec;
  } catch (_) {
    return null;
  }
}

/** Запоминает оплаченный заказ, чтобы ссылка «Исправить текст» жила в футере 24 ч. */
function rememberPaidOrder() {
  if (!state.orderId || state.amendUsed) return;
  try {
    localStorage.setItem(AMEND_STORAGE_KEY, JSON.stringify({ id: state.orderId, ts: Date.now() }));
  } catch (_) { /* приватный режим — не критично */ }
  syncAmendLink();
}

function forgetPaidOrder() {
  try { localStorage.removeItem(AMEND_STORAGE_KEY); } catch (_) { /* noop */ }
  syncAmendLink();
}

function syncAmendLink() {
  el.openAmend.classList.toggle("hidden", !readPaidOrder());
}

function showAmendError(text) {
  el.amendError.textContent = text || "";
  el.amendError.classList.toggle("hidden", !text);
}

async function openAmend(orderId) {
  try {
    const resp = await fetch(API.amend(orderId));
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      // 404/409/410 — правка недоступна насовсем: убираем ссылку из футера
      if ([404, 409, 410].includes(resp.status)) forgetPaidOrder();
      showError("Исправить текст нельзя", body.detail || `Ошибка сервера ${resp.status}`);
      return;
    }

    amendOrderId = orderId;
    el.amendFields.replaceChildren(
      ...body.text_lines.map((text, i) => {
        const input = document.createElement("input");
        input.type = "text";
        input.maxLength = 200;
        input.value = text;                       // value, не innerHTML: ввод не интерпретируется как HTML
        input.setAttribute("aria-label", `Строка ${i + 1}`);
        return input;
      })
    );
    showAmendError("");
    el.amendSubmit.disabled = false;
    hideModal(el.modalSuccess);
    showModal(el.modalAmend);
  } catch (e) {
    showError("Не удалось открыть правку", e.message);
  }
}

el.amendOpen.addEventListener("click", () => openAmend(state.orderId));
el.openAmend.addEventListener("click", () => {
  const rec = readPaidOrder();
  if (rec) openAmend(rec.id);
});
el.amendCancel.addEventListener("click", () => hideModal(el.modalAmend));

el.amendSubmit.addEventListener("click", async () => {
  const lines = [...el.amendFields.querySelectorAll("input")].map((i) => i.value.trim());
  if (lines.some((t) => !t)) {
    showAmendError("Строка не может быть пустой.");
    return;
  }

  el.amendSubmit.disabled = true;
  showAmendError("");
  try {
    const resp = await fetch(API.amend(amendOrderId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text_lines: lines }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showAmendError(body.detail || `Ошибка сервера ${resp.status}`);
      if ([404, 409, 410].includes(resp.status)) forgetPaidOrder();
      el.amendSubmit.disabled = false;
      return;
    }

    state.amendUsed = true;
    state.orderId   = amendOrderId;
    forgetPaidOrder();
    hideModal(el.modalAmend);
    showModal(el.modalWait);
    el.waitText.textContent = "Формируем исправленный PDF...";
    el.waitBar.style.width = "100%";
    downloadPdf(body.download_token);
  } catch (e) {
    showAmendError(`Не удалось отправить: ${e.message}`);
    el.amendSubmit.disabled = false;
  }
});

syncAmendLink();

// Ссылка вида /?amend=<order_id> (например, из ответа поддержки)
(function checkAmendDeepLink() {
  const id = new URLSearchParams(window.location.search).get("amend");
  if (!id) return;
  window.history.replaceState({}, "", window.location.pathname);
  openAmend(id);
})();

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

/**
 * @param {string}   title
 * @param {string}   text
 * @param {Function} [onRetry] — если передан, показывает кнопку «Скачать ещё раз»
 */
function showError(title, text, onRetry) {
  el.errorTitle.textContent = title;
  el.errorText.textContent  = text;
  el.errorRetry.classList.toggle("hidden", !onRetry);
  el.errorRetry.onclick = onRetry || null;
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
/* =====================================================================
   ПРЕДЗАПОЛНЕНИЕ ИЗ URL (?size=…&text1=…&bg=…&color=…&font=…) — см. prefill.js
   ===================================================================== */
function applyUrlPrefill() {
  if (typeof parsePrefill !== "function" || !window.location.search) return;

  const sizeKeys = [...el.sizeGrid.querySelectorAll(".size-btn")].map((b) => b.dataset.size);
  const fonts = [...el.fontList.querySelectorAll(".font-btn")].map((b) => b.dataset.font);
  const pre = parsePrefill(window.location.search, {
    sizeKeys,
    fonts,
    colorNames: state.colorNames,
    maxLines: state.maxLines,
    min: CUSTOM_SIZE_MIN,
    max: CUSTOM_SIZE_MAX,
  });

  // Нажимаем существующие кнопки: так работают все их обработчики (в т.ч. защита цветов)
  const clickBy = (container, selector, attr, value) => {
    for (const b of container.querySelectorAll(selector)) {
      if (b.dataset[attr] === value) { b.click(); return; }
    }
  };

  let applied = false;
  if (pre.size && pre.size.key) {
    clickBy(el.sizeGrid, ".size-btn", "size", pre.size.key);
    applied = true;
  } else if (pre.size) {
    el.customW.value = String(pre.size.w);
    el.customH.value = String(pre.size.h);
    handleCustomSizeInput();
    applied = true;
  }
  if (pre.font) { clickBy(el.fontList, ".font-btn", "font", pre.font); applied = true; }
  if (pre.bg) { clickBy(el.bgSwatches, ".swatch", "color", pre.bg); applied = true; }
  if (pre.color) { clickBy(el.txtSwatches, ".swatch", "color", pre.color); applied = true; }
  if (pre.lines.length) {
    state.lines = pre.lines.map((text) => ({ text, scale: 1.0 }));
    renderTextLines();
    schedulePreview();
    applied = true;
  }
  if (applied && typeof ym === "function") ym(108388194, "reachGoal", "prefill");
}

async function init() {
  await loadTemplates();
  renderTextLines(); // после loadTemplates — maxLines уже актуален
  applyUrlPrefill();  // ссылки с SEO-страниц: размер, текст, цвета

  // Реферальный блок — управляется флагом REFERRAL_ENABLED
  const refCard = $("card-ref");
  if (refCard) refCard.style.display = REFERRAL_ENABLED ? "" : "none";

  // Первое превью не запускаем — поля пустые
}

init();
