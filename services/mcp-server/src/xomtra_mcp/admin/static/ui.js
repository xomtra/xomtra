// xomtra admin GUI: follows Home Assistant when shown in its Ingress iframe (same
// origin): light/dark mode, language, number, date and time format, time zone.
// Outside Home Assistant it stays dark and uses the browser's language.
(function () {
  "use strict";
  var LANGS = ["en", "cs", "de", "pl", "nl"];
  var hass = null;
  try {
    if (window.parent !== window) {
      var ha = window.parent.document.querySelector("home-assistant");
      hass = (ha && ha.hass) || null;
    }
  } catch (e) {
    hass = null; // not framed by Home Assistant
  }
  // The server translates the pages (i18n.py); tell it Home Assistant's language once.
  var want = String((hass && ((hass.locale && hass.locale.language) || hass.language)) ||
    navigator.language || "en").toLowerCase().split("-")[0];
  if (LANGS.indexOf(want) < 0) want = "en";
  if (want !== document.documentElement.lang) {
    var me = document.currentScript;
    var path = me ? new URL(me.src, location.href).pathname.replace(/\/ui\.js$/, "") : "";
    try {
      document.cookie = "xomtra_lang=" + want + "; path=" + (path || "/") +
        "; max-age=31536000; SameSite=Strict";
      if (sessionStorage.getItem("xomtra_lang") !== want) {
        sessionStorage.setItem("xomtra_lang", want);
        location.reload();
        return;
      }
    } catch (e) {
      /* no cookies or storage: stay in the server's language */
    }
  }
  var dark = hass && hass.themes && hass.themes.darkMode;
  if (dark === false) document.documentElement.dataset.theme = "light";

  var loc = (hass && hass.locale) || {};
  var lang = loc.language || (hass && hass.language) || navigator.language || "en";
  var numLocale = {
    comma_decimal: "en-US",
    decimal_comma: "de-DE",
    space_comma: "fr-FR",
    system: undefined,
  }[loc.number_format];
  if (!(loc.number_format in { comma_decimal: 1, decimal_comma: 1, space_comma: 1, system: 1 })) {
    numLocale = lang;
  }
  var grouping = loc.number_format !== "none";
  var timeZone;
  if (loc.time_zone === "server" && hass && hass.config && hass.config.time_zone) {
    timeZone = hass.config.time_zone;
  }
  var hour12;
  if (loc.time_format === "12") hour12 = true;
  if (loc.time_format === "24") hour12 = false;

  function number(el) {
    var v = Number(el.getAttribute("value"));
    if (!isFinite(v)) return;
    var pct = el.classList.contains("p");
    var opts = { useGrouping: grouping };
    if (pct) {
      opts.style = "percent";
      opts.maximumFractionDigits = 1;
    } else if (Number.isInteger(v) || Math.abs(v) >= 1000) {
      opts.maximumFractionDigits = 0;
    } else {
      opts.maximumSignificantDigits = 4;
    }
    try {
      el.textContent = new Intl.NumberFormat(numLocale, opts).format(v);
    } catch (e) {
      /* keep the server's text */
    }
  }

  function dateParts(d, withTime) {
    var opts = { year: "numeric", month: "2-digit", day: "2-digit", timeZone: timeZone };
    if (withTime) {
      opts.hour = "2-digit";
      opts.minute = "2-digit";
      if (hour12 !== undefined) opts.hour12 = hour12;
    }
    var f = new Intl.DateTimeFormat(lang, opts);
    var order = { DMY: ["day", "month", "year"], MDY: ["month", "day", "year"], YMD: ["year", "month", "day"] }[
      loc.date_format
    ];
    if (!order) return f.format(d);
    var p = {};
    f.formatToParts(d).forEach(function (x) {
      p[x.type] = x.value;
    });
    var sep = loc.date_format === "YMD" ? "-" : loc.date_format === "MDY" ? "/" : ".";
    var out = order
      .map(function (k) {
        return p[k];
      })
      .join(sep);
    if (withTime) out += " " + p.hour + ":" + p.minute + (p.dayPeriod ? " " + p.dayPeriod : "");
    return out;
  }

  function when(el) {
    var iso = el.getAttribute("datetime");
    var withTime = iso.length > 10;
    var d = new Date(withTime ? iso : iso + "T00:00:00Z");
    if (isNaN(d)) return;
    try {
      el.textContent = withTime ? dateParts(d, true) : dateParts(d, false);
      el.title = iso;
    } catch (e) {
      /* keep the server's text */
    }
  }

  var tip = null;
  function showTip(el, text) {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tip";
      tip.setAttribute("role", "tooltip");
      document.body.appendChild(tip);
    }
    tip.textContent = text;
    tip.hidden = false;
    var r = el.getBoundingClientRect();
    var w = tip.offsetWidth;
    var h = tip.offsetHeight;
    var x = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
    var y = r.bottom + 8;
    if (y + h > window.innerHeight - 8) y = Math.max(8, r.top - h - 8);
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function hideTip() {
    if (tip) tip.hidden = true;
  }

  function tips(texts) {
    document.querySelectorAll("[data-tip]").forEach(function (el) {
      var key = el.getAttribute("data-tip");
      var text = (texts && texts[key]) || el.getAttribute("title") || "";
      el.removeAttribute("title");
      el.setAttribute("aria-label", el.textContent + ": " + text);
      el.addEventListener("mouseenter", function () {
        showTip(el, text);
      });
      el.addEventListener("focus", function () {
        showTip(el, text);
      });
      el.addEventListener("mouseleave", hideTip);
      el.addEventListener("blur", hideTip);
    });
    window.addEventListener("scroll", hideTip, true);
  }

  function ready() {
    document.querySelectorAll("data.n, data.p").forEach(number);
    document.querySelectorAll("time[datetime]").forEach(when);
    var code = String(lang).toLowerCase().split("-")[0];
    if (LANGS.indexOf(code) < 0) code = "en";
    // tips.json sits next to ui.js and carries its version (?v=), so an upgrade does
    // not keep the cached tips of the previous one.
    var src = document.querySelector("script[src*='/ui.js']");
    var url = src ? src.src.replace(/ui\.js(\?|$)/, "tips.json$1") : "tips.json";
    fetch(url, { credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : {};
      })
      .then(function (all) {
        tips(all[code] || all.en);
      })
      .catch(function () {
        tips(null);
      });
  }
  var script = document.currentScript;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      ready(script);
    });
  } else {
    ready(script);
  }
})();
