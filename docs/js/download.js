/* Download control of the installation page: one button that downloads the build for the
   reader's system (macOS: the dmg; Windows: the ARM64 zip when the browser says the machine is
   ARM, the x64 zip otherwise) and a menu with every build. The links are the fixed ones of the
   latest release (releases/latest/download/<file without version>): they download without any
   script; the GitHub API, when it answers, only adds the version and the size. A download followed from
   here is counted as an event of GoatCounter (download-macos, download-windows-x64, download-windows-arm64)
   when the counter is on the page (extra.goatcounter in mkdocs.yml): country and source of those who
   download from the site, for the page "Downloads and visits". */
(function () {
  var API = "https://api.github.com/repos/enzococca/pyarchinit-photogrammetry/releases/latest";

  function count(system) {
    var gc = window.goatcounter;
    if (!system || !gc || typeof gc.count !== "function") return;
    gc.count({path: "download-" + system, title: "Download " + system, event: true});
  }

  function counted(link, system) {  // left and middle click (a new tab downloads as well)
    link.addEventListener("click", function () { count(system()); });
    link.addEventListener("auxclick", function (e) { if (e.button === 1) count(system()); });
  }

  function detect() {
    var ua = navigator.userAgent || "";
    if (/Mac/.test(ua) && !/iPhone|iPad/.test(ua)) return Promise.resolve("macos");
    if (!/Windows/.test(ua)) return Promise.resolve("");
    var uad = navigator.userAgentData;  // Windows on ARM declares itself x64 in the user agent
    if (uad && uad.getHighEntropyValues) {
      return uad.getHighEntropyValues(["architecture"]).then(function (v) {
        return v.architecture === "arm" ? "windows-arm64" : "windows-x64";
      }).catch(function () { return "windows-x64"; });
    }
    return Promise.resolve(/ARM64|aarch64/i.test(ua) ? "windows-arm64" : "windows-x64");
  }

  function setup(box) {
    if (box.getAttribute("data-ppg-ready")) return;
    box.setAttribute("data-ppg-ready", "1");
    var main = box.querySelector("[data-ppg-main]");
    var toggle = box.querySelector("[data-ppg-toggle]");
    var menu = box.querySelector("[data-ppg-menu]");
    var entries = Array.prototype.slice.call(menu.querySelectorAll("a[data-ppg-system]"));
    var prefix = main.getAttribute("data-ppg-prefix") || "";

    function choose(system) {
      var entry = entries.filter(function (a) { return a.getAttribute("data-ppg-system") === system; })[0];
      if (!entry) return;
      main.href = entry.href;
      main.setAttribute("download", "");
      main.setAttribute("data-ppg-current", system);
      main.textContent = prefix + entry.getAttribute("data-ppg-label");
      entries.forEach(function (a) { a.classList.toggle("ppg-download__current", a === entry); });
    }
    detect().then(choose);
    counted(main, function () { return main.getAttribute("data-ppg-current"); });  // none: the releases page
    entries.forEach(function (a) { counted(a, function () { return a.getAttribute("data-ppg-system"); }); });

    function close() { menu.hidden = true; toggle.setAttribute("aria-expanded", "false"); }
    toggle.addEventListener("click", function (e) {
      e.preventDefault();
      menu.hidden = !menu.hidden;
      toggle.setAttribute("aria-expanded", menu.hidden ? "false" : "true");
    });
    document.addEventListener("click", function (e) { if (!box.contains(e.target)) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });

    fetch(API)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (release) {
        if (!release || !release.assets) return;
        var version = String(release.tag_name || "").replace(/^v/, "");
        entries.forEach(function (a) {
          var suffix = a.getAttribute("data-ppg-asset");
          var asset = release.assets.filter(function (x) { return x.name.endsWith(suffix); })[0];
          if (!asset) {  // a build the latest release does not have (Windows on ARM before 1.1.0)
            a.parentNode.hidden = true;
            if (a.classList.contains("ppg-download__current")) choose(a.getAttribute("data-ppg-fallback") || "");
            return;
          }
          var note = version + ", " + Math.round(asset.size / 1048576) + " MB";
          var text = a.textContent.replace(/,\s*\d[\d.]*,\s*\d+ MB\)$/, ")");  // without a note added before
          a.textContent = /\)$/.test(text) ? text.slice(0, -1) + ", " + note + ")" : text + " (" + note + ")";
          if (a.classList.contains("ppg-download__current")) main.textContent = prefix + a.getAttribute("data-ppg-label") + " \u2013 " + note;
        });
      })
      .catch(function () {});
  }

  function fill() { document.querySelectorAll("[data-ppg-download]").forEach(setup); }
  if (typeof document$ !== "undefined") document$.subscribe(fill); else document.addEventListener("DOMContentLoaded", fill);
})();
