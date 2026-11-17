"use strict";
/* Sign-in page. Becomes "create the first account" on a server with no accounts.
   The left half is decoration, drawn by login-art.js. */

(async () => {
  const $ = (q) => document.querySelector(q);
  const SVGNS = "http://www.w3.org/2000/svg";

  function make(ns, tag, attrs, kids) {
    const el = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
    return el;
  }
  const s = (tag, attrs, ...kids) => make(SVGNS, tag, attrs, kids);
  const icon = (d) => s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d }));
  const SUN = "M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8 M12 2v2 M12 20v2 M4.9 4.9l1.4 1.4 M17.7 17.7l1.4 1.4 M2 12h2 M20 12h2 M4.9 19.1l1.4-1.4 M17.7 6.3l1.4-1.4";
  const MOON = "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z";
  const EYE = "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6";
  const EYE_OFF = "M3 3l18 18 M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.1 M6.6 6.6A17 17 0 0 0 2 12s3.5 7 10 7a9.6 9.6 0 0 0 5.4-1.6 M9.9 9.9a3 3 0 0 0 4.2 4.2";
  const CHECK = "M5 12.5l4.5 4.5L19 7.5";
  const ALERT = "M12 8v5 M12 16.5v.5 M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z";

  // ------------------------------------------------------------ theme
  // The same setting as the workspace, so switching here switches there too.
  const readPrefs = () => { try { return JSON.parse(localStorage.getItem("mikrobox.prefs")) || {}; } catch (e) { return {}; } };
  const dark = () => document.documentElement.dataset.theme === "dark";
  function applyTheme() {
    const theme = readPrefs().theme;
    const isDark = theme === "dark" || (theme !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = isDark ? "dark" : "light";
    const label = isDark ? "Switch to light theme" : "Switch to dark theme";
    $("#theme").setAttribute("aria-label", label);
    $("#theme").title = label;
    $("#theme").replaceChildren(icon(isDark ? SUN : MOON));
  }
  applyTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  $("#theme").addEventListener("click", () => {
    const prefs = readPrefs();
    prefs.theme = dark() ? "light" : "dark";
    try { localStorage.setItem("mikrobox.prefs", JSON.stringify(prefs)); } catch (e) { /* this visit only */ }
    applyTheme();
  });

  // ------------------------------------------------------------ the labs on the left
  try {
    window.MikroboxArt.start({ svg: $("#lab"), log: $("#log"), name: $("#scene-name"), blurb: $("#scene-blurb"),
      bar: $("#scene-bar"), head: $("#scene-head") });
  } catch (e) { /* decoration only: the form works without it */ }

  // ------------------------------------------------------------ server
  $("#host").textContent = location.host;
  const setServer = (up) => {
    $("#live").classList.toggle("down", !up);
    $("#server").textContent = up ? "Lab server online" : "Can't reach the server";
  };
  let status = null;
  try {
    const res = await fetch("/api/auth/status", { cache: "no-store" });
    if (res.ok) status = await res.json();
  } catch (e) { /* shown below */ }
  setServer(!!status);
  if (status && (!status.enabled || status.user)) { location.replace("/"); return; }
  status = status || { setup_needed: false };
  if (status.version) $("#version").textContent = `v${status.version}`;
  if (status.label) { $("#label").textContent = status.label; $("#label").hidden = false; }
  const minPassword = status.min_password || 8;

  // ------------------------------------------------------------ the form
  const form = $("#card"), user = $("#username"), pass = $("#password"), again = $("#again"), go = $("#go");
  const setup = !!status.setup_needed;
  const goText = (text) => { $("#go-text").textContent = text; };
  const hint = (el, text, tone) => {
    if (el.dataset.keep) {  // keeps its line while empty, so nothing below it moves
      el.hidden = false;
      el.style.visibility = text ? "" : "hidden";
      text = text || "\u00a0";
    } else {
      el.hidden = !text;
    }
    el.className = "hint" + (tone ? " " + tone : "");
    el.replaceChildren(...(tone === "good" ? [icon(CHECK)] : tone === "bad" || tone === "warn" ? [icon(ALERT)] : []), text || "");
  };

  if (setup) {
    document.title = "Create your account · Mikrobox";
    $("#title").textContent = "Create your account";
    $("#lead").textContent = "This server has no accounts yet. The one you create now is used to sign in from here on. More can be added later.";
    goText("Create account");
    $("#again-field").hidden = false;
    again.required = true;
    pass.autocomplete = "new-password";
    $("#user-hint").hidden = false;
    hint($("#match-hint"), "");
  }
  form.hidden = false;
  form.classList.add("in");
  user.focus();

  // Live checks while creating the account: long enough, and typed the same twice.
  const checkNew = () => {
    if (!setup) return;
    const n = pass.value.length;
    hint($("#length-hint"), n >= minPassword ? `${minPassword} characters or more` : `At least ${minPassword} characters`, n >= minPassword ? "good" : "");
    if (!again.value) hint($("#match-hint"), "");
    else if (again.value === pass.value) hint($("#match-hint"), "Passwords match", "good");
    else if (again.value.length >= pass.value.length) hint($("#match-hint"), "The two passwords are not the same.", "bad");
    else hint($("#match-hint"), "");
  };
  pass.addEventListener("input", checkNew);
  again.addEventListener("input", checkNew);
  checkNew();

  // Show or hide what was typed, in both password fields.
  const reveal = $("#reveal");
  const setReveal = (on) => {
    for (const el of [pass, again]) el.type = on ? "text" : "password";
    reveal.setAttribute("aria-pressed", String(on));
    reveal.setAttribute("aria-label", on ? "Hide password" : "Show password");
    reveal.title = reveal.getAttribute("aria-label");
    reveal.replaceChildren(icon(on ? EYE_OFF : EYE));
  };
  setReveal(false);
  reveal.addEventListener("click", () => { setReveal(reveal.getAttribute("aria-pressed") !== "true"); pass.focus(); });

  // Caps Lock is the usual reason a right password is refused.
  const caps = (e) => {
    if (!e.getModifierState) return;
    hint($("#caps"), e.getModifierState("CapsLock") ? "Caps Lock is on" : "", "warn");
  };
  for (const el of [pass, again]) {
    el.addEventListener("keydown", caps);
    el.addEventListener("keyup", caps);
    el.addEventListener("blur", () => hint($("#caps"), ""));
  }

  const fail = (message, field) => {
    $("#error").textContent = message;
    form.classList.remove("shake");
    void form.offsetWidth;  // restart the animation
    form.classList.add("shake");
    if (field) { field.focus(); field.select(); }
  };
  form.addEventListener("animationend", () => form.classList.remove("shake", "in"));

  let busy = false;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    $("#error").textContent = "";
    const name = user.value.trim();
    if (!name) { fail("Enter your username.", user); return; }
    if (!pass.value) { fail("Enter your password.", pass); return; }
    if (setup && pass.value.length < minPassword) { fail(`The password needs at least ${minPassword} characters.`, pass); return; }
    if (setup && pass.value !== again.value) { fail("The two passwords are not the same.", again); return; }
    busy = true;
    go.disabled = true;
    go.classList.add("busy");
    goText(setup ? "Creating account…" : "Signing in…");
    let done = false;
    try {
      const res = await fetch(setup ? "/api/auth/setup" : "/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ username: name, password: pass.value }),
      });
      setServer(true);
      if (res.ok) {
        done = true;
        go.classList.remove("busy");
        go.classList.add("done");
        goText(setup ? "Account created" : "Signed in");
        const quiet = matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (!quiet) $("#page").classList.add("leaving");
        setTimeout(() => location.replace("/"), quiet ? 0 : 260);
        return;
      }
      const data = await res.json().catch(() => null);
      fail((data && typeof data.detail === "string" && data.detail) || "That did not work. Try again.", setup ? null : pass);
      // Someone else created the first account in the meantime.
      if (res.status === 409) setTimeout(() => location.reload(), 1500);
    } catch (err) {
      setServer(false);
      fail("Could not reach the server.");
    } finally {
      if (!done) {
        busy = false;
        go.disabled = false;
        go.classList.remove("busy");
        goText(setup ? "Create account" : "Sign in");
      }
    }
  });
})();
