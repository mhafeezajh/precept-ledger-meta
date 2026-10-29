// Runs in <head> before the page paints, so a chosen theme never flashes the other one.
// "system" follows the operating system; the choice is a per-browser convenience only.
(function () {
  var choice = "system";
  try { choice = localStorage.getItem("pl-theme") || "system"; } catch (e) { /* private mode */ }
  if (choice === "light" || choice === "dark") {
    document.documentElement.setAttribute("data-theme", choice);
  }
})();
