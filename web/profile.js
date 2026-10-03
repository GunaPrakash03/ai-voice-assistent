/*
 * Sidebar profile card & Global RBAC Navigator.
 * Injected into the ".rail-foot" block on every page: a compact account button (avatar, name, email)
 * whose menu holds the workspace, role, My profile, the local role switcher and Sign out. Also gives
 * the sidebar links their icons and adds the collapse-to-icons toggle.
 * Dynamically enforces RBAC navigation visibility for Super Admin, Product Admin, Member Admin, and Users.
 */
(function () {
  var foot = document.querySelector(".rail-foot");
  if (!foot || document.getElementById("railProfile")) return;

  // ── Sidebar icons (Lucide shapes, MIT) ───────────────────────────────────
  var ICONS = {
    overview: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    calls: '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/>',
    detail: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
    trunks: '<rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3M12 12V8"/>',
    market: '<circle cx="8" cy="21" r="1"/><circle cx="19" cy="21" r="1"/><path d="M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12"/>',
    agents: '<path d="M12 8V4H8"/><rect x="4" y="8" width="16" height="12" rx="2"/><path d="M2 14h2M20 14h2M15 13v2M9 13v2"/>',
    webhooks: '<path d="M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 0 1 2 17c.01-.7.2-1.4.57-2"/><path d="m6 17 3.13-5.78c.53-.97.1-2.18-.5-3.1a4 4 0 1 1 6.89-4.06"/><path d="m12 6 3.13 5.73C15.66 12.7 16.9 13 18 13a4 4 0 0 1 0 8"/>',
    keys: '<path d="M2.59 17.41A2 2 0 0 0 2 18.83V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.17a2 2 0 0 0 1.42-.59l.81-.81a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/>',
    user: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    guide: '<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
    orgs: '<path d="M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z"/><path d="M6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2M10 6h4M10 10h4M10 14h4M10 18h4"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    softphone: '<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M9 7h.01M12 7h.01M15 7h.01M9 11h.01M12 11h.01M15 11h.01M9 15h.01M12 15h.01M15 15h.01M12 19h.01"/>',
    branding: '<circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.93 0 1.65-.75 1.65-1.69 0-.44-.18-.84-.44-1.13-.29-.29-.44-.65-.44-1.13a1.64 1.64 0 0 1 1.67-1.67h2c3.05 0 5.56-2.5 5.56-5.55C21.97 6.01 17.46 2 12 2z"/>',
    chart: '<path d="M3 3v18h18M18 17V9M13 17V5M8 17v-3"/>',
    cost: '<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M8 6h8M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01M16 18h.01"/>',
    dot: '<circle cx="12" cy="12" r="3"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
    chevrons: '<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
    collapse: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M16 15l-3-3 3-3"/>'
  };
  function icon(name) {
    return '<svg class="nav-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || ICONS.dot) + '</svg>';
  }
  var ROUTE_ICONS = { "": "overview", calls: "calls", "call-detail": "detail", "sip-trunks": "trunks", "phone-numbers": "market",
    agents: "agents", "agent-builder": "agents", webhooks: "webhooks", "api-keys": "keys", profile: "user", "user-guide": "guide",
    "admin-guide": "guide", organizations: "orgs", users: "users", softphone: "softphone", "competitor-analysis": "chart",
    "cost-comparison": "cost", "live-console": "calls" };
  function iconFor(a) {
    var href = a.getAttribute("href") || "";
    if (/#cardBranding/.test(href)) return "branding";
    var path = href.split(/[?#]/)[0].replace(/^\/call-desk/, "").replace(/^\/+|\/+$/g, "");
    return ROUTE_ICONS[path] || "dot";
  }
  var EMOJI = /^[\u{1F000}-\u{1FAFF}\u2600-\u27BF\uFE0F\u200D\s]+/u;

  // Gives every sidebar link an icon and a label span (the collapsed sidebar hides the label and
  // shows it as a tooltip). Safe to run again: links done before are skipped.
  function enhanceRail() {
    var rail = document.querySelector(".rail");
    if (!rail) return;
    Array.prototype.forEach.call(rail.querySelectorAll(".nav a, .nav button"), function (a) {
      if (a.dataset.enh || a.classList.contains("brand-edit-btn")) return;
      a.dataset.enh = "1";
      var label = null;
      var first = a.firstChild;
      if (first && first.nodeType === 3 && first.textContent.trim()) {
        label = document.createElement("span");
        label.textContent = first.textContent.replace(EMOJI, "").trim();
        a.replaceChild(label, first);
      } else {
        label = Array.prototype.filter.call(a.children, function (c) { return c.tagName === "SPAN" && !c.classList.contains("ct"); })[0] || null;
        if (label) label.textContent = label.textContent.replace(EMOJI, "").trim();
      }
      if (label) { label.classList.add("nav-label"); if (!a.title) a.title = label.textContent; }
      a.insertAdjacentHTML("afterbegin", icon(iconFor(a)));
    });
  }

  // ── One sidebar for every page ───────────────────────────────────────────
  // Pages used to hand-write their own lists, so items appeared and vanished between pages. The
  // list below is the only source; links a page already has (with their click handlers, counters and
  // ids) are reused, missing ones are created, extras dropped. Items are shown for the role seen on
  // the last visit straight away, then confirmed by /api/v1/auth/profile, so nothing pops in.
  // Roles follow serve.py: ADMIN_PAGES need an admin, SUPER_ADMIN_PAGES a super admin.
  var NAV = [
    { head: "Workspace" },
    { href: "/", label: "Overview", v: "overview" },
    { href: "/calls", label: "Calls", v: "calls" },
    { href: "/call-detail", label: "Call detail", v: "detail" },
    { href: "/agents", label: "Agents", v: "agents", role: "admin", also: ["/agent-builder"] },
    { href: "/sip-trunks", label: "SIP Trunks & DIDs", v: "telephony", role: "admin" },
    { href: "/phone-numbers", label: "Buy Phone Numbers", v: "market", role: "admin", tag: "DID" },
    { href: "/webhooks", label: "Webhooks & Data", role: "admin" },
    { head: "Resources", role: "admin" },
    { href: "/user-guide", label: "User guide", role: "admin" },
    { head: "Overall system", role: "super" },
    { href: "/organizations", label: "Organizations", role: "super" },
    { href: "/users", label: "Users Registry", role: "super" },
    { href: "/api-keys", label: "API Keys & Providers", role: "super" },
    { href: "/softphone", label: "Softphone Dialer", role: "super" },
    { href: "/profile#cardBranding", label: "Dashboard Branding", role: "super" }
  ];
  var ROLE_KEY = "rail_role";
  function roleAllows(role, need) {
    if (!need) return true;
    if (need === "super") return role === "super_admin";
    return role === "super_admin" || role === "admin";
  }
  function normPath(href) {
    var h = (href || "").replace(/^https?:\/\/[^/]+/, "");
    if (/#cardBranding/.test(h)) return "/profile#cardBranding";
    h = h.split(/[?#]/)[0].replace(/^\/call-desk(?=\/|$)/, "") || "/";
    return h.length > 1 ? h.replace(/\/+$/, "") : h;
  }
  function setShown(el, on) {
    el.hidden = !on;
    el.style.display = on ? (el.dataset.display || "") : "none";
  }
  function buildNav(role) {
    var nav = document.querySelector(".rail .nav");
    if (!nav || nav.dataset.built) return;
    var existing = {};
    Array.prototype.forEach.call(nav.querySelectorAll("a[href], button[data-v]"), function (a) {
      var key = a.tagName === "A" ? normPath(a.getAttribute("href")) : null;
      if (key && !existing[key]) existing[key] = a;
    });
    var here = normPath(location.pathname + (location.hash === "#cardBranding" ? "#cardBranding" : ""));
    var frag = document.createDocumentFragment(), group = frag;
    NAV.forEach(function (item) {
      if (item.head) {
        if (item.role === "super") {
          group = document.createElement("div");
          group.id = "railSuperAdminSection"; group.className = "sa-nav-section";
          group.setAttribute("data-super-admin-only", ""); group.dataset.display = "flex";
          group.innerHTML = '<div class="sa-h"></div>';
          group.firstChild.textContent = item.head;
          setShown(group, roleAllows(role, "super"));
          frag.appendChild(group);
        } else {
          var h = document.createElement("div");
          h.className = "nav-h"; h.textContent = item.head;
          if (item.role) { h.setAttribute("data-admin-only", ""); h.dataset.display = "block"; setShown(h, roleAllows(role, item.role)); }
          (group === frag ? frag : group).appendChild(h);
        }
        return;
      }
      var a = existing[item.href];
      if (a) {
        // Same wording on every page: replace the page's own label text (keeps counters and ids).
        var lbl = Array.prototype.filter.call(a.childNodes, function (n) {
          return (n.nodeType === 3 && n.textContent.trim()) || (n.nodeType === 1 && n.tagName === "SPAN" && !n.classList.contains("ct"));
        })[0];
        if (lbl) lbl.textContent = item.label;
      } else {
        a = document.createElement("a");
        a.href = item.href;
        a.textContent = item.label;
        if (item.tag) { var t = document.createElement("span"); t.className = "ct"; t.textContent = item.tag; a.appendChild(t); }
      }
      a.removeAttribute("data-admin-only"); a.removeAttribute("data-super-admin-only");
      if (item.role === "admin") a.setAttribute("data-admin-only", "");
      if (group === frag && item.role === "super") a.setAttribute("data-super-admin-only", "");
      a.dataset.display = "";
      a.classList.remove("sa-link");
      if (group === frag || item.role !== "super") setShown(a, roleAllows(role, item.role));
      else { a.hidden = false; a.style.display = ""; }
      // Real links (not the in-page tabs on Overview / Call Desk) get their selected state here.
      if (a.getAttribute("role") !== "tab") {
        var on = item.href === here || (item.also || []).indexOf(here) >= 0;
        a.classList.toggle("active", on);
        if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
        a.removeAttribute("aria-selected");
      }
      group.appendChild(a);
    });
    nav.textContent = "";
    nav.appendChild(frag);
    nav.dataset.built = "1";
  }
  var cachedRole = "";
  try { cachedRole = localStorage.getItem(ROLE_KEY) || ""; } catch (e) {}
  buildNav(cachedRole);

  // Collapse to an icon-only sidebar; remembered per browser.
  var COLLAPSE_KEY = "rail_collapsed";
  function setCollapsed(on) {
    document.documentElement.classList.toggle("rail-collapsed", on);
    try { localStorage.setItem(COLLAPSE_KEY, on ? "1" : "0"); } catch (e) {}
  }
  try { if (localStorage.getItem(COLLAPSE_KEY) === "1") document.documentElement.classList.add("rail-collapsed"); } catch (e) {}

  // Styling lives in web/theme.css (sidebar section).
  var card = document.createElement("div");
  card.id = "railProfile";
  card.innerHTML =
    '<div class="rp-menu" id="rpMenu" role="menu" hidden>' +
      '<div class="rp-menu-h"><div class="rp-sub" id="rpWorkspace"></div><span class="rp-role" id="rpRole" hidden></span></div>' +
      '<a href="/profile" class="rp-item" role="menuitem">' + icon("user") + '<span>My profile</span></a>' +
      '<div id="rpRoleSwitchers" class="rp-switch">' +
        '<div class="rp-sw-h">Switch role · this computer only</div>' +
        '<div class="rp-sw-row">' +
          '<a href="/switch-role?role=super_admin" class="rp-switch-btn sa">Super</a>' +
          '<a href="/switch-role?role=admin" class="rp-switch-btn pa">Product</a>' +
          '<a href="/switch-role?role=member_admin" class="rp-switch-btn ma">Member</a>' +
        '</div>' +
      '</div>' +
      '<a href="#" class="rp-item" id="rpSignOut" role="menuitem">' + icon("logout") + '<span>Sign out</span></a>' +
    '</div>' +
    '<button type="button" class="rp-trigger" id="rpTrigger" aria-haspopup="menu" aria-expanded="false" title="Account">' +
      '<span class="rp-avatar" id="rpAvatar">…</span>' +
      '<span class="rp-who"><span class="rp-name" id="rpName">Loading…</span><span class="rp-sub" id="rpEmail"></span></span>' +
      '<span class="rp-chev">' + icon("chevrons") + '</span>' +
    '</button>';
  foot.insertBefore(card, foot.firstChild);
  var collapseBtn = document.createElement("button");
  collapseBtn.type = "button"; collapseBtn.className = "rail-collapse"; collapseBtn.title = "Collapse sidebar";
  collapseBtn.innerHTML = icon("collapse") + '<span class="nav-label">Collapse</span>';
  collapseBtn.addEventListener("click", function () { setCollapsed(!document.documentElement.classList.contains("rail-collapsed")); });
  foot.insertBefore(collapseBtn, card);
  enhanceRail();

  function setMenu(open) {
    $("rpMenu").hidden = !open;
    $("rpTrigger").setAttribute("aria-expanded", open ? "true" : "false");
  }
  document.addEventListener("click", function (e) { if (!card.contains(e.target)) setMenu(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });

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
    enhanceRail();
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
        try { localStorage.setItem(ROLE_KEY, profile.is_super_admin ? "super_admin" : profile.is_admin ? "admin" : window.currentRole); } catch (e) {}
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
    if (e.target.closest("#rpSignOut")) {
      e.preventDefault();
      fetch("/api/v1/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .then(function () { location.href = "/login"; }).catch(function () { location.href = "/login"; });
      return;
    }
    if (e.target.closest("#rpTrigger")) setMenu($("rpMenu").hidden);
  });

  load();
})();
