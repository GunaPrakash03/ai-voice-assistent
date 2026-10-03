/*
 * Firm picker for Super Admins on the Cases and Team pages.
 * Super Admins work across firms: this adds a "Firm" select to the top bar, filled from
 * /api/v1/system/organizations (which answers only for Super Admins, so everyone else gets nothing).
 * The choice lives in the page URL as ?firm=<workspace_id> so a reload or a shared link keeps it.
 *
 *   FirmPicker.mount(containerEl, onChange)   onChange() runs after the viewer picks another firm
 *   FirmPicker.id()                           "" for the viewer's own firm, else the chosen workspace_id
 *   FirmPicker.query(prefix)                  "" or e.g. "&workspace_id=ws-123" (prefix "?" or "&")
 */
(function () {
  var current = new URLSearchParams(location.search).get("firm") || "";

  function setUrl(id) {
    var u = new URL(location.href);
    if (id) u.searchParams.set("firm", id); else u.searchParams.delete("firm");
    history.replaceState(null, "", u.pathname + u.search + u.hash);
  }

  window.FirmPicker = {
    id: function () { return current; },
    query: function (prefix) { return current ? (prefix || "&") + "workspace_id=" + encodeURIComponent(current) : ""; },
    mount: function (container, onChange) {
      if (!container) return;
      fetch("/api/v1/system/organizations").then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        var orgs = (j && j.organizations) || [];
        if (orgs.length < 2) return;
        var label = document.createElement("label");
        label.className = "firm-picker";
        label.innerHTML = '<span>Firm</span><select aria-label="Firm"></select>';
        var sel = label.querySelector("select");
        var own = document.createElement("option");
        own.value = ""; own.textContent = "My firm";
        sel.appendChild(own);
        orgs.forEach(function (o) {
          var opt = document.createElement("option");
          opt.value = o.workspace_id;
          opt.textContent = o.name + (o.active === false ? " (inactive)" : "");
          sel.appendChild(opt);
        });
        sel.value = current;
        if (sel.value !== current) { current = ""; setUrl(""); }
        sel.addEventListener("change", function () { current = sel.value; setUrl(current); onChange(); });
        container.insertBefore(label, container.firstChild);
      }).catch(function () {});
    }
  };
})();
