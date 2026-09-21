/*
 * Sidebar profile card & Global RBAC Navigator.
 * Injected into the ".rail-foot" WORKSPACE block on every page: avatar initials, name, email,
 * role badge and workspace. Click opens /profile.
 * Dynamically enforces RBAC navigation visibility for Super Admin, Product Admin, Member Admin, and Users.
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
    "#railProfile .rp-avatar.sa{background:linear-gradient(135deg,#7c3aed,#9333ea)}" +
    "#railProfile .rp-name{color:var(--rail-on,#fff);font-weight:600;font-size:13px;line-height:1.3;font-family:inherit;overflow-wrap:anywhere}" +
    "#railProfile .rp-sub{color:var(--rail-ink,#aaa);font-size:10.5px;line-height:1.5;overflow-wrap:anywhere}" +
    "#railProfile .rp-role{display:inline-block;margin-top:4px;padding:2px 8px;border-radius:9px;font-size:9.5px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;background:rgba(194,86,15,.25);color:#F3B27A}" +
    "#railProfile .rp-role.sa{background:rgba(124,58,237,.3);color:#d8b4fe;border:1px solid rgba(192,132,252,.3)}" +
    "#railProfile .rp-role.ma{background:rgba(2,132,199,.25);color:#7dd3fc;border:1px solid rgba(56,189,248,.3)}" +
    "#railProfile .rp-open{font-size:10px;color:var(--rail-ink,#aaa);margin-top:6px}" +
    ".sa-link:hover{background:rgba(124,58,237,.22) !important;color:#fff !important}";
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
    '</div>' +
    '<div id="rpRoleSwitchers" style="margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,.08)">' +
      '<div style="font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;color:rgba(255,255,255,.5);margin-bottom:5px;font-family:var(--f-mono,monospace)">Quick Role Switch:</div>' +
      '<div style="display:flex;gap:4px;flex-wrap:wrap">' +
        '<a href="/switch-role?role=super_admin" class="rp-switch-btn sa" style="font-size:10.5px;padding:2px 7px;border-radius:4px;text-decoration:none;background:rgba(124,58,237,.25);color:#d8b4fe;border:1px solid rgba(192,132,252,.3)">👑 Super</a>' +
        '<a href="/switch-role?role=admin" class="rp-switch-btn pa" style="font-size:10.5px;padding:2px 7px;border-radius:4px;text-decoration:none;background:rgba(245,158,11,.15);color:#fcd34d;border:1px solid rgba(245,158,11,.25)">🛡️ Prod</a>' +
        '<a href="/switch-role?role=member_admin" class="rp-switch-btn ma" style="font-size:10.5px;padding:2px 7px;border-radius:4px;text-decoration:none;background:rgba(2,132,199,.15);color:#7dd3fc;border:1px solid rgba(56,189,248,.25)">👥 Member</a>' +
      '</div>' +
    '</div>';
  foot.insertBefore(card, foot.firstChild);

  var $ = function (id) { return document.getElementById(id); };
  // The quick role switcher is a dev tool: /switch-role only answers from localhost, so only show it there.
  if (!/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) {
    var sw = $("rpRoleSwitchers");
    if (sw) sw.remove();
  }
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
    var av = $("rpAvatar");
    av.textContent = initials(profile.name, profile.email);
    if (profile.role === "super_admin" || profile.is_super_admin) {
      av.classList.add("sa");
    }
    $("rpName").textContent = name + (profile.title ? " · " + profile.title : "");
    $("rpEmail").textContent = profile.email || "";
    $("rpWorkspace").textContent = profile.workspace || "Workspace";
    $("rpWorkspace").title = profile.workspace_id || "";
    var role = $("rpRole");
    var ROLE_LABEL = {
      super_admin: "👑 Super Admin",
      admin: "🛡️ Product Admin",
      member_admin: "👥 Member Admin"
    };
    role.textContent = ROLE_LABEL[profile.role] || profile.role || "";
    role.className = "rp-role " + (
      (profile.role === "super_admin" || profile.is_super_admin) ? "sa" :
      profile.role === "member_admin" ? "ma" : ""
    );
    role.hidden = !profile.role;
  }

  function injectSuperAdminNav() {
    var nav = document.querySelector(".rail .nav, nav.rail .nav, .nav");
    if (!nav || document.getElementById("railSuperAdminSection")) return;

    var sec = document.createElement("div");
    sec.id = "railSuperAdminSection";
    sec.className = "sa-nav-section";
    sec.setAttribute("data-super-admin-only", "");
    sec.style.cssText = "margin-top:14px;padding-top:10px;border-top:1px solid rgba(255,255,255,.1);display:flex;flex-direction:column;gap:1px";

    sec.innerHTML =
      '<div style="font-family:var(--f-mono,monospace);font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#c084fc;padding:4px 12px 6px;font-weight:700">👑 Overall System</div>' +
      '<a href="/organizations" class="sa-link" style="all:unset;cursor:pointer;padding:9px 12px;border-radius:5px;color:#e9d5ff;font-size:13.5px;font-weight:500;display:flex;justify-content:space-between;align-items:center;transition:background .15s">' +
        '<span>🏢 Organizations</span><span class="ct" style="font-family:var(--f-mono,monospace);background:rgba(124,58,237,.3);color:#d8b4fe;padding:2px 6px;border-radius:4px;font-weight:700;font-size:10px">ALL</span>' +
      '</a>' +
      '<a href="/users" class="sa-link" style="all:unset;cursor:pointer;padding:9px 12px;border-radius:5px;color:#e9d5ff;font-size:13.5px;font-weight:500;display:flex;justify-content:space-between;align-items:center;transition:background .15s">' +
        '<span>👥 Users Registry</span><span class="ct" style="font-family:var(--f-mono,monospace);background:rgba(124,58,237,.3);color:#d8b4fe;padding:2px 6px;border-radius:4px;font-weight:700;font-size:10px">ALL</span>' +
      '</a>';

    nav.appendChild(sec);
  }

  window.currentRole = "super_admin";
  window.applyRoleVisibility = function () {
    var role = (window.currentRole || (profile && profile.role) || "super_admin").toLowerCase();
    var isSuperAdmin = role === "super_admin" || (profile && Boolean(profile.is_super_admin));
    var isAdmin = isSuperAdmin || role === "admin" || (profile && Boolean(profile.is_admin));

    Array.prototype.forEach.call(document.querySelectorAll("[data-admin-only]"), function (el) {
      el.hidden = !isAdmin;
      el.style.display = isAdmin ? "" : "none";
    });

    Array.prototype.forEach.call(document.querySelectorAll("[data-super-admin-only]"), function (el) {
      el.hidden = !isSuperAdmin;
      el.style.display = isSuperAdmin ? "flex" : "none";
    });

    var saSec = document.getElementById("railSuperAdminSection");
    if (saSec) {
      saSec.hidden = !isSuperAdmin;
      saSec.style.display = isSuperAdmin ? "flex" : "none";
    } else if (isSuperAdmin) {
      injectSuperAdminNav();
    }
  };

  function load() {
    return fetch("/api/v1/auth/profile").then(function (r) { return r.json(); }).then(function (d) {
      if (d.status === "ok") {
        profile = d.profile;
        window.currentRole = profile.role || (profile.is_super_admin ? "super_admin" : (profile.is_admin ? "admin" : "member_admin"));
        render();
        window.applyRoleVisibility();
      } else {
        $("rpName").textContent = "Profile unavailable";
      }
    }).catch(function () {
      $("rpName").textContent = "Profile unavailable";
    });
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
