/* Предзаполнение конструктора из URL (чистая функция, без DOM — тестируется в Node).
 *
 *   ?size=2x2            — типовой размер (ключ из шаблонов)
 *   ?size=3000x1000      — свой размер в мм, в пределах [min, max]
 *   ?text1=…&text2=…     — строки текста (до maxLines, до 120 знаков каждая)
 *   ?bg=Красный&color=Белый&font=Golos%20Text — только значения из списков шаблона
 *
 * Всё, чего нет в списках или вне диапазона, молча отбрасывается. Текст дальше
 * вставляется только через value (escapeHtml в app.js), никогда через innerHTML.
 */
(function (root) {
  "use strict";

  var MAX_LINE_LEN = 120;
  var CONTROL_CHARS = /[\u0000-\u001f\u007f]/g;

  function pick(value, allowed) {
    return value && allowed.indexOf(value) !== -1 ? value : null;
  }

  function parsePrefill(search, opts) {
    var p = new URLSearchParams(search);
    var out = { size: null, lines: [], bg: null, color: null, font: null };

    var size = (p.get("size") || "").trim();
    if (size) {
      if (opts.sizeKeys.indexOf(size) !== -1) {
        out.size = { key: size };
      } else {
        var m = /^(\d{3,4})x(\d{3,4})$/.exec(size);
        if (m) {
          var w = Number(m[1]);
          var h = Number(m[2]);
          if (w >= opts.min && w <= opts.max && h >= opts.min && h <= opts.max) {
            out.size = { w: w, h: h };
          }
        }
      }
    }

    for (var i = 1; i <= opts.maxLines; i++) {
      var raw = p.get("text" + i);
      if (raw === null) continue;
      var text = raw.replace(CONTROL_CHARS, "").trim().slice(0, MAX_LINE_LEN);
      if (text) out.lines.push(text);
    }

    out.bg = pick(p.get("bg"), opts.colorNames);
    out.color = pick(p.get("color"), opts.colorNames);
    out.font = pick(p.get("font"), opts.fonts);
    return out;
  }

  root.parsePrefill = parsePrefill;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parsePrefill: parsePrefill };
  }
})(typeof window !== "undefined" ? window : this);
