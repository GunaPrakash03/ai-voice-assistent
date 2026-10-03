/*
 * Sidebar profile card & Global RBAC Navigator.
 * Injected into the ".rail-foot" WORKSPACE block on every page: avatar initials, name, email,
 * role badge and workspace. Click opens /profile.
 * Dynamically enforces RBAC navigation visibility for Super Admin, Product Admin, Member Admin, and Users.
 */
(function () {
  var foot = document.querySelector(".rail-foot");
  if (!foot || document.getElementById("railProfile")) return;

  // Colours come from the page theme (web/theme.css), which also styles the Super Admin nav section.
  var css = document.createElement("style");
  css.textContent =
    "#railProfile{margin:0 0 12px;padding:10px;border-radius:12px;background:var(--panel);border:1px solid var(--line);cursor:pointer;transition:background .15s,border-color .15s}" +
    "#railProfile:hover{border-color:var(--ink-faint)}" +
    "#railProfile .rp-row{display:flex;align-items:flex-start;gap:10px}" +
    "#railProfile .rp-avatar{width:36px;height:36px;border-radius:50%;flex:0 0 36px;display:flex;align-items:center;justify-content:center;font:700 13px/1 var(--f-ui,sans-serif);color:#fff;letter-spacing:.5px;background:linear-gradient(135deg,#F97316,#EC4899)}" +
    "#railProfile .rp-avatar.sa{background:linear-gradient(135deg,#7C3AED,#C026D3)}" +
    "#railProfile .rp-name{color:var(--ink);font-weight:600;font-size:13px;line-height:1.3;overflow-wrap:anywhere}" +
    "#railProfile .rp-sub{color:var(--ink-faint);font-size:11px;line-height:1.5;overflow-wrap:anywhere}" +
    "#railProfile .rp-role{display:inline-block;margin-top:4px;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:600;letter-spacing:.02em;background:var(--accent-soft);color:var(--accent)}" +
    "#railProfile .rp-role.sa{background:var(--sa-soft);color:var(--sa)}" +
    "#railProfile .rp-role.ma{background:rgba(2,132,199,.12);color:#0284C7}" +
    "#railProfile .rp-open{font-size:11px;color:var(--ink-faint);margin-top:6px}" +
    "#railProfile .rp-open a{color:var(--accent);text-decoration:none}" +
    "#rpRoleSwitchers{margin-top:10px;padding-top:8px;border-top:1px solid var(--line)}" +
    "#rpRoleSwitchers .rp-sw-h{font-size:10px;letter-spacing:.04em;color:var(--ink-faint);margin-bottom:5px}" +
    ".rp-switch-btn{font-size:11px;padding:2px 8px;border-radius:999px;text-decoration:none;border:1px solid var(--line);color:var(--ink-soft);background:var(--panel)}" +
    ".rp-switch-btn:hover{border-color:var(--ink-faint);color:var(--ink)}";
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
        '<div class="rp-open">Open profile → · <a href="#" id="rpSignOut">Sign out</a></div>' +
      '</div>' +
    '</div>' +
    '<div id="rpRoleSwitchers">' +
      '<div class="rp-sw-h">Quick role switch</div>' +
      '<div style="display:flex;gap:4px;flex-wrap:wrap">' +
        '<a href="/switch-role?role=super_admin" class="rp-switch-btn sa">👑 Super</a>' +
        '<a href="/switch-role?role=admin" class="rp-switch-btn pa">🛡️ Prod</a>' +
        '<a href="/switch-role?role=member_admin" class="rp-switch-btn ma">👥 Member</a>' +
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
    sec.innerHTML =
      '<div class="sa-h">Overall system</div>' +
      '<a href="/organizations" class="sa-link"><span>🏢 Organizations</span><span class="ct">ALL</span></a>' +
      '<a href="/users" class="sa-link"><span>👥 Users Registry</span><span class="ct">ALL</span></a>' +
      '<a href="/api-keys" class="sa-link"><span>🔑 API Keys & Providers</span><span class="ct">SUPER</span></a>' +
      '<a href="/softphone" class="sa-link"><span>📞 Softphone Dialer</span><span class="ct">SUPER</span></a>' +
      '<a href="/profile#cardBranding" class="sa-link"><span>🎨 Dashboard Branding</span><span class="ct">SUPER</span></a>';

    nav.appendChild(sec);
  }

  window.currentRole = null;
  window.currentProfile = null;
  window.applyBranding = function (branding) {
    if (!branding || !branding.dashboard_name) return;
    var name = branding.dashboard_name;
    var tagline = branding.tagline || "AI Assistant Voice";
    Array.prototype.forEach.call(document.querySelectorAll(".brand b"), function (el) {
      el.textContent = name;
    });
    Array.prototype.forEach.call(document.querySelectorAll(".brand span"), function (el) {
      el.textContent = tagline;
    });
    if (document.title.indexOf("Call Desk") !== -1) {
      document.title = document.title.replace(/Call Desk/g, name);
    }
  };

  window.applyRoleVisibility = function () {
    if (!profile) return;
    var role = (window.currentRole || (profile && profile.role) || "viewer").toLowerCase();
    var isSuperAdmin = role === "super_admin" || (profile && Boolean(profile.is_super_admin));
    var isAdmin = isSuperAdmin || role === "admin" || (profile && Boolean(profile.is_admin));

    Array.prototype.forEach.call(document.querySelectorAll("[data-admin-only]"), function (el) {
      el.hidden = !isAdmin;
      el.style.display = isAdmin ? (el.dataset.display || "") : "none";
    });

    Array.prototype.forEach.call(document.querySelectorAll("[data-super-admin-only]"), function (el) {
      el.hidden = !isSuperAdmin;
      el.style.display = isSuperAdmin ? (el.dataset.display || "flex") : "none";
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
        window.currentProfile = profile;
        window.currentRole = profile.role || (profile.is_super_admin ? "super_admin" : (profile.is_admin ? "admin" : "member_admin"));
        render();
        window.applyRoleVisibility();
        if (profile.system_branding) {
          window.applyBranding(profile.system_branding);
        }
        window.dispatchEvent(new CustomEvent("profileLoaded", { detail: profile }));
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
