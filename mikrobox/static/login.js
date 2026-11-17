"use strict";
/* Sign-in page. Becomes "create the first account" on a server with no accounts. */

(async () => {
  const $ = (q) => document.querySelector(q);
  try {
    const prefs = JSON.parse(localStorage.getItem("mikrobox.prefs")) || {};
    const dark = prefs.theme === "dark" || (prefs.theme !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  } catch (e) { /* default theme */ }

  let status;
  try {
    status = await (await fetch("/api/auth/status")).json();
  } catch (e) {
    status = { enabled: true, setup_needed: false, user: null };
  }
  if (!status.enabled || status.user) { location.replace("/"); return; }
  if (status.label) {
    document.title = `Sign in to Mikrobox (${status.label})`;
    const tag = document.createElement("span");
    tag.className = "install-label";
    tag.textContent = status.label;
    document.querySelector("#card .brand").append(tag);
  }

  const setup = status.setup_needed;
  if (setup) {
    $("#title").textContent = "Create your account";
    $("#lead").textContent = "This server has no accounts yet. The one you create now is used to sign in from here on. More can be added later.";
    $("#go").textContent = "Create account";
    $("#again-field").hidden = false;
    $("#again").required = true;
    $("#password").autocomplete = "new-password";
  }
  $("#card").hidden = false;
  $("#username").focus();

  $("#card").addEventListener("submit", async (e) => {
    e.preventDefault();
    const error = $("#error");
    error.textContent = "";
    if (setup && $("#password").value !== $("#again").value) {
      error.textContent = "The two passwords are not the same.";
      return;
    }
    $("#go").disabled = true;
    try {
      const res = await fetch(setup ? "/api/auth/setup" : "/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ username: $("#username").value.trim(), password: $("#password").value }),
      });
      if (res.ok) { location.replace("/"); return; }
      const data = await res.json().catch(() => null);
      error.textContent = (data && typeof data.detail === "string" && data.detail) || "That did not work. Try again.";
      // Someone else created the first account in the meantime.
      if (res.status === 409) setTimeout(() => location.reload(), 1500);
    } catch (err) {
      error.textContent = "Could not reach the server.";
    } finally {
      $("#go").disabled = false;
    }
  });
})();
