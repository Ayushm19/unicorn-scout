// Loaded in <head> so the saved theme applies before first paint (no flash). Light by default.
(function () {
  var t = "light";
  try { t = localStorage.getItem("theme") || "light"; } catch (e) {}
  document.documentElement.dataset.theme = t;
  window.toggleTheme = function () {
    var next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) {}
  };
})();
