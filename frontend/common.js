// Shared by every dashboard page; loaded in <head> so role-based hiding applies before the first paint.
//  - any fetch that gets 401 sends the browser to /login?next=<this page>
//  - GET /api/me: <html data-role="admin|member"> and data-auth="on" when login is on
//  - .admin-only is hidden unless the user is an admin; .auth-only unless login is on
//  - the top bar gets the username and a 登出 button
(() => {
  const toLogin = () => {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
  };

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const res = await realFetch(input, init);
    const url = typeof input === "string" ? input : input.url;
    if (res.status === 401 && !/\/api\/log(in|out)\b/.test(url)) {
      toLogin();
      return new Promise(() => {});  // the page is going away: don't let it show an error
    }
    return res;
  };

  const style = document.createElement("style");
  style.textContent = `
    html:not([data-role=admin]) .admin-only, html:not([data-auth=on]) .auth-only { display: none !important; }
    .user-box { margin-left: auto; display: flex; align-items: center; gap: 10px; flex: none; }
    .user-box .user-name { color: var(--topbar-text); font-size: 14px; font-weight: 600; white-space: nowrap;
      max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
    .user-box button { font: inherit; font-size: 13px; padding: 4px 12px; border-radius: 8px; cursor: pointer;
      border: 1px solid var(--border); background: var(--card); color: var(--text); }
    .user-box button:hover { border-color: var(--accent); }
    @media (max-width: 560px) { .user-box .user-name { display: none; } }
  `;
  document.head.appendChild(style);

  const ready = fn => document.readyState === "loading"
    ? document.addEventListener("DOMContentLoaded", fn) : fn();

  function drawUserBox(me) {
    const bar = document.querySelector(".topbar-inner");
    if (!bar || !me.auth) return;
    const box = document.createElement("div");
    box.className = "user-box";
    const name = document.createElement("span");
    name.className = "user-name";
    name.textContent = me.username;
    name.title = `${me.username} · ${me.role === "admin" ? "管理员" : "成员"}`;
    const out = document.createElement("button");
    out.type = "button";
    out.textContent = "登出";
    out.onclick = async () => {
      out.disabled = true;
      try { await fetch("/api/logout", { method: "POST" }); } catch {}
      location.href = "/login";
    };
    box.append(name, out);
    bar.append(box);
  }

  const me = fetch("/api/me").then(r => r.json()).then(m => {
    document.documentElement.dataset.role = m.role;
    if (m.auth) document.documentElement.dataset.auth = "on";
    ready(() => drawUserBox(m));
    return m;
  });
  me.catch(() => {});

  window.kiri = {
    me,  // Promise of {username, role, auth}
    isAdmin: () => document.documentElement.dataset.role === "admin",
  };
})();
