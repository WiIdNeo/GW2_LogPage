// Generischer Filter: <select data-filter id="x"> -> value = CSS-Klasse, die ein .item haben muss.
// Zustand wird ueber ?id=wert in der URL gehalten (statisch, funktioniert auf GitHub Pages).
(function () {
  var selects = Array.prototype.slice.call(document.querySelectorAll('select[data-filter]'));
  var items = Array.prototype.slice.call(document.querySelectorAll('.item'));
  var count = document.getElementById('count');
  var params = new URLSearchParams(location.search);

  function apply() {
    var active = selects.map(function (s) { return s.value; }).filter(Boolean);
    var shown = 0;
    items.forEach(function (el) {
      var ok = active.every(function (c) { return el.classList.contains(c); });
      el.hidden = !ok;
      if (ok) shown++;
    });
    if (count) count.textContent = shown + ' / ' + items.length + ' Einträge';
    var p = new URLSearchParams();
    selects.forEach(function (s) { if (s.value) p.set(s.id, s.value); });
    var q = p.toString();
    history.replaceState(null, '', location.pathname + (q ? '?' + q : '') + location.hash);   // Sprungziel behalten
  }

  selects.forEach(function (s) {
    if (params.has(s.id)) s.value = params.get(s.id);
    s.addEventListener('change', apply);
  });
  apply();
})();

// Spielerseite: Sprunglinks oeffnen eingeklappte Abschnitte; Knoepfe "Alle auf-/zuklappen".
(function () {
  function openId(id) {
    var el = id && document.getElementById(id);
    if (el && el.tagName === 'DETAILS') el.open = true;
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href^="#"]');
    if (a) openId(decodeURIComponent(a.getAttribute('href').slice(1)));
    var b = e.target.closest && e.target.closest('button[data-details]');
    if (b) Array.prototype.forEach.call(document.querySelectorAll('details.boss-sec'), function (d) {
      d.open = b.getAttribute('data-details') === 'open';
    });
  });
  openId(decodeURIComponent(location.hash.slice(1)));
})();
