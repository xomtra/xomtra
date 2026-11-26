// Follow Home Assistant's light/dark mode when shown in its Ingress iframe (same origin);
// otherwise the stylesheet follows the browser (prefers-color-scheme).
(function () {
  try {
    if (window.parent === window) return;
    var ha = window.parent.document.querySelector("home-assistant");
    var dark = ha && ha.hass && ha.hass.themes && ha.hass.themes.darkMode;
    if (typeof dark === "boolean") {
      document.documentElement.dataset.theme = dark ? "dark" : "light";
    }
  } catch (e) {
    /* not framed by Home Assistant */
  }
})();
