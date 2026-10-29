// Small enhancements only: every page works without this file (forms post normally).
// No inline handlers anywhere, so the Content Security Policy can forbid inline script.
(function () {
  "use strict";

  var THEMES = ["system", "light", "dark"];
  var LABELS = { system: "Theme: follow system", light: "Theme: light", dark: "Theme: dark" };

  function currentTheme() {
    try { return localStorage.getItem("pl-theme") || "system"; } catch (e) { return "system"; }
  }

  function applyTheme(choice) {
    if (choice === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", choice);
    try { localStorage.setItem("pl-theme", choice); } catch (e) { /* not saved; still applied */ }
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.textContent = LABELS[choice];
      button.setAttribute("aria-label", LABELS[choice] + ". Activate to change.");
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-theme-toggle]");
    if (!button) return;
    var next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    applyTheme(next);
  });

  // The persona menu switches as soon as a person is chosen; the button stays for no-JS use.
  document.addEventListener("change", function (event) {
    var select = event.target.closest("[data-autosubmit]");
    if (select && select.form) select.form.requestSubmit();
  });

  // "Use this example" buttons fill the nearest text field.
  document.addEventListener("click", function (event) {
    var example = event.target.closest("[data-fill]");
    if (!example) return;
    var field = document.getElementById(example.getAttribute("data-fill"));
    if (field) { field.value = example.getAttribute("data-value"); field.focus(); }
  });

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(currentTheme());
    document.querySelectorAll("[data-js-only]").forEach(function (el) { el.hidden = false; });
    document.querySelectorAll("[data-no-js]").forEach(function (el) { el.hidden = true; });
  });
})();
