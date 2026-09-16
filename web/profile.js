/*
 * Sidebar profile card.
 * Injected into the ".rail-foot" WORKSPACE block on every page: avatar initials, name, email,
 * role and workspace. Click opens /profile. The dashboard has no sign-in yet, so this is the
 * workspace admin the server runs as.
 */
(function () {
  var foot = document.querySelector(".rail-foot");
  if (!foot || document.getElementById("railProfile")) return;

  var css = document.createElement("style");
  css.textContent =
    "#railProfile{margin:0 0 12px;padding:10px 10px 10px;border-radius:10px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);cursor:pointer;transition:background .15s}" +
    "#railProfile:hover{background:rgba(255,255,255,.09)}" +
    "#railProfile .rp-row{display:flex;align-items:flex-start;gap:10px}" +
    "#railProfile .rp-avatar{width:36px;height:36px;border-radius:50%;flex:0 0 36px;display:flex;align-items:center;justify-content:center;font:700 13px/1 var(--f-mono,monospace);color:#fff;letter-spacing:.5px;background:linear-gradient(135deg,#C2560F,#E08A3C)}" +
    "#railProfile .rp-name{color:var(--rail-on,#fff);font-weight:600;font-size:13px;line-height:1.3;font-family:inherit;overflow-wrap:anywhere}" +
    "#railProfile .rp-sub{color:var(--rail-ink,#aaa);font-size:10.5px;line-height:1.5;overflow-wrap:anywhere}" +
    "#railProfile .rp-role{display:inline-block;margin-top:4px;padding:1px 7px;border-radius:9px;font-size:9.5px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;background:rgba(194,86,15,.25);color:#F3B27A}" +
    "#railProfile .rp-open{font-size:10px;color:var(--rail-ink,#aaa);margin-top:6px}";
  document.head.appendChild(css);

  var card = document.createElement("div");
  card.id = "railProfile";
  card.title = "Open my profile";
  card.innerHTML =
    '<div class="rp-row">' +
      '<div class="rp-avatar" id="rpAvatar">…</div>' +
      '<div style="min-width:0;flex:1">' +
        '<div class="rp-name" id="rpName">Loading profile…</div>' +
        '<div class="rp-sub" id="rpEmail"></div>' +
        '<div class="rp-sub" id="rpWorkspace"></div>' +
        '<span class="rp-role" id="rpRole" hidden></span>' +
        '<div class="rp-open">Open profile → · <a href="#" id="rpSignOut" style="color:#F3B27A">Sign out</a></div>' +
      '</div>' +
    '</div>';
  foot.insertBefore(card, foot.firstChild);

  var $ = function (id) { return document.getElementById(id); };
  var profile = null;

  function initials(name, email) {
    var src = (name || "").trim() || (email || "").split("@")[0].replace(/[._-]+/g, " ");
    var parts = src.split(/\s+/).filter(Boolean);
    if (!parts.length) return "?";
    return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
  }

  function render() {
    if (!profile) return;
    var name = profile.name || (profile.email ? profile.email.split("@")[0] : "Workspace admin");
    $("rpAvatar").textContent = initials(profile.name, profile.email);
    $("rpName").textContent = name + (profile.title ? " · " + profile.title : "");
    $("rpEmail").textContent = profile.email || "";
    $("rpWorkspace").textContent = profile.workspace || "Workspace";
    $("rpWorkspace").title = profile.workspace_id || "";
    var role = $("rpRole");
    role.textContent = profile.role || "";
    role.hidden = !profile.role;
  }

  // Hide admin-only controls (data-admin-only) for members. The server enforces the rule too;
  // this just keeps the screens honest about what a member can do.
  window.currentRole = "admin";
  window.applyRoleVisibility = function () {
    var isAdmin = window.currentRole === "admin";
    Array.prototype.forEach.call(document.querySelectorAll("[data-admin-only]"), function (el) { el.hidden = !isAdmin; if (!isAdmin) el.style.display = "none"; else el.style.display = ""; });
  };
  function load() {
    return fetch("/api/v1/auth/profile").then(function (r) { return r.json(); }).then(function (d) {
      if (d.status === "ok") { profile = d.profile; window.currentRole = profile.role || "admin"; render(); window.applyRoleVisibility(); }
      else { $("rpName").textContent = "Profile unavailable"; }
    }).catch(function () { $("rpName").textContent = "Profile unavailable"; });
  }

  card.addEventListener("click", function (e) {
    if (e.target && e.target.id === "rpSignOut") {
      e.preventDefault(); e.stopPropagation();
      fetch("/api/v1/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .then(function () { location.href = "/login"; }).catch(function () { location.href = "/login"; });
      return;
    }
    if (location.pathname.indexOf("/profile") === -1) { location.href = "/profile"; }
  });

  load();
})();
