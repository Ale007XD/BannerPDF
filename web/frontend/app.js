/**
 * app.js — BannerPrint конструктор
 *
 * Флоу:
 *   настройка → дебаунс превью (500 мс) → кнопка «Получить PDF» →
 *   POST /api/order → переход к оплате (Tona) → поллинг статуса →
 *   GET /api/download/{token} → скачивание файла
 */

"use strict";

/* =====================================================================
   КОНФИГУРАЦИЯ
   ===================================================================== */
const API = {
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
  sizeKey:   "3x2",
  bgColor:   "Белый",
  textColor: "Черный",
  font:      "Golos Text",
  lines:     ["", ""],   // до 6 строк
  refCode:   "",

  // Оплата
  orderId:   null,
  payUrl:    null,
};

/* =====================================================================
   DOM-ССЫЛКИ
   ===================================================================== */
const $ = (id) => document.getElementById(id);

const el = {
  previewImg:    $("preview-img"),
  previewPlaceholder: $("preview-placeholder"),
  previewLoader: $("preview-loader"),
  previewMeta:   $("preview-meta"),

  sizeGrid:   $("size-grid"),
  bgSwatches: $("bg-swatches"),
  txtSwatches:$("text-swatches"),
  textLines:  $("text-lines"),
  addLineBtn: $("add-line-btn"),
  fontList:   $("font-list"),
  refInput:   $("ref-input"),
  refStatus:  $("ref-status"),
  buyBtn:     $("buy-btn"),

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
   ПРИВЯЗКА СОБЫТИЙ — РАЗМЕР
   ===================================================================== */
el.sizeGrid.addEventListener("click", (e) => {
  const btn = e.target.closest(".size-btn");
  if (!btn) return;
  document.querySelectorAll(".size-btn").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.sizeKey = btn.dataset.size;
  schedulePreview();
});

/* =====================================================================
   ПРИВЯЗКА СОБЫТИЙ — ЦВЕТА
   ===================================================================== */
function bindSwatches(container, field) {
  container.addEventListener("click", (e) => {
    const sw = e.target.closest(".swatch");
    if (!sw) return;
    container.querySelectorAll(".swatch").forEach((s) => s.classList.remove("active"));
    sw.classList.add("active");
    state[field] = sw.dataset.color;
    schedulePreview();
  });
}

bindSwatches(el.bgSwatches,  "bgColor");
bindSwatches(el.txtSwatches, "textColor");

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

  // Отключаем кнопку добавления если строк уже 6
  el.addLineBtn.disabled = state.lines.length >= 6;
}

el.addLineBtn.addEventListener("click", () => {
  if (state.lines.length >= 6) return;
  state.lines.push("");
  renderTextLines();
});

/* =====================================================================
   ПРИВЯЗКА СОБЫТИЙ — ШРИФТ
   ===================================================================== */
el.fontList.addEventListener("click", (e) => {
  const btn = e.target.closest(".font-btn");
  if (!btn) return;
  document.querySelectorAll(".font-btn").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.font = btn.dataset.font;
  schedulePreview();
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
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* =====================================================================
   ИНИЦИАЛИЗАЦИЯ
   ===================================================================== */
function init() {
  renderTextLines();
  // Первое превью не запускаем — поля пустые
}

init();
