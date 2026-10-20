/* Download buttons of the installation page: the latest release of the application for the
   reader's system first, with the version and the size, from the GitHub API; without the API the
   buttons lead to the page of the latest release. */
(function () {
  function fill() {
    var buttons = document.querySelectorAll("a[data-ppg-asset]");
    if (!buttons.length) return;
    var ua = navigator.userAgent || "";
    var mine = /Windows/.test(ua) ? "windows" : /Mac/.test(ua) ? "macos" : "";
    buttons.forEach(function (a) {
      var system = a.getAttribute("data-ppg-asset").indexOf("windows") === 0 ? "windows" : "macos";
      a.classList.toggle("md-button--primary", system === mine);
      if (system === mine && a.parentNode.firstElementChild !== a) a.parentNode.insertBefore(a, a.parentNode.firstElementChild);
    });
    fetch("https://api.github.com/repos/enzococca/pyarchinit-photogrammetry/releases/latest")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (release) {
        if (!release || !release.assets) return;
        buttons.forEach(function (a) {
          var suffix = a.getAttribute("data-ppg-asset");
          var asset = release.assets.filter(function (x) { return x.name.endsWith(suffix); })[0];
          if (!asset) return;
          a.href = asset.browser_download_url;
          a.setAttribute("download", asset.name);
          var size = Math.round(asset.size / 1048576);
          a.textContent = a.textContent.replace(/\s*\(.*\)$/, "") + " (" + release.tag_name.replace(/^v/, "") + ", " + size + " MB)";
        });
      })
      .catch(function () {});
  }
  if (typeof document$ !== "undefined") document$.subscribe(fill); else document.addEventListener("DOMContentLoaded", fill);
})();
