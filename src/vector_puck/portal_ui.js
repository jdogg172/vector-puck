"use strict";
const $ = id => document.getElementById(id);
const dayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let session, state, room, serial = 0, busy = false, editing, pending, codeTimer;
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function button(text, fn) {
  const b = node("button", text);
  b.type = "button";
  b.onclick = fn;
  return b;
}
function notice(message = "") { $("notice").textContent = message; }
function closeDialogs() {
  document.querySelectorAll("dialog[open]").forEach(el => el.close());
}
function loggedOut(message) {
  serial++; room = null; session = null; state = null;
  closeDialogs(); $("pairCode").textContent = ""; $("phones").replaceChildren();
  $("alarms").replaceChildren(); $("alarmForm").reset(); $("controls").hidden = true;
  $("dashboard").hidden = $("logout").hidden = true;
  $("signin").hidden = false;
  notice(message);
}
async function api(path, data, condition = {}) {
  const headers = {...condition};
  if (data !== undefined) {
    headers["Content-Type"] = "application/json";
    if (session) headers["X-CSRF-Token"] = session.csrf;
  }
  let res, result;
  try {
    res = await fetch(path, {method:data === undefined ? "GET" : "POST", headers,
      body:data === undefined ? undefined : JSON.stringify(data), cache:"no-store",
      credentials:"same-origin", redirect:"error", signal:AbortSignal.timeout(27000)});
    result = await res.json();
  } catch {
    throw Error("Connection interrupted. Refresh the room before retrying a change.");
  }
  if (!res.ok) {
    if (res.status === 401) {
      loggedOut("Session ended. Reopen room controls or sign in again.");
      $("retry").hidden = false;
    }
    throw Error(result.error || "The request could not be completed.");
  }
  return result;
}
function lock(on) {
  busy = on;
  for (const id of ["room","refresh","add","savePrefs","saveAlarm","confirmAction","logout"])
    $(id).disabled = on;
  document.querySelectorAll(".actions button, [data-close], #family button").forEach(b => b.disabled = on);
}
async function boot() {
  $("retry").hidden = true;
  try {
    session = await api("/api/session");
    if (!session.signed_in) {
      loggedOut(location.pathname === "/signin-failed" ? "Sign-in expired or this account does not have Vector access." : "");
      return;
    }
    $("signin").hidden = true;
    $("dashboard").hidden = $("logout").hidden = false;
    $("family").hidden = !session.can_pair;
    $("room").replaceChildren(...session.rooms.map(r => new Option(r.label, r.id)));
    room = $("room").value;
    await loadRoom();
    if (session?.can_pair) await loadPhones();
  } catch (e) { notice(e.message); $("retry").hidden = false; }
}
function showState(value) {
  state = value;
  $("status").textContent = [value.online ? "Online" : "Offline",
    value.online ? (value.muted ? "Microphone muted" : "Microphone on") : "",
    value.version ? "Version " + value.version : "Version unknown"].filter(Boolean).join(" · ");
  $("status").parentElement.classList.toggle("online", value.online);
  $("controls").hidden = false;
  const list = $("alarms");
  list.replaceChildren();
  for (const a of value.alarms) {
    const li = node("li", undefined, "alarm");
    const info = node("div");
    const schedule = a.days.length ? a.days.map(d => dayNames[d]).join(", ") : a.date || "Next occurrence";
    info.append(node("div", a.at, "time"), node("strong", a.name),
      node("div", (a.status === "ringing" ? "Ringing · " : "") + schedule, "meta"));
    const actions = node("div", undefined, "actions");
    if (value.online) {
      if (a.status === "ringing")
        actions.append(button("Snooze", () => action("snooze", a)), button("Dismiss", () => action("dismiss", a)));
      actions.append(button("Edit", () => edit(a)), button("Cancel alarm", () => action("cancel", a)));
    }
    li.append(info, actions); list.append(li);
  }
  $("empty").hidden = value.alarms.length > 0;
  for (const k of ["color","brightness","spoilers"]) {
    $(k).value = value.preferences[k];
    $(k).disabled = !value.online;
  }
  $("add").hidden = $("savePrefs").hidden = !value.online;
}
async function loadRoom() {
  const turn = ++serial;
  state = null; $("controls").hidden = true; $("status").textContent = "Loading room…";
  $("status").parentElement.classList.remove("online");
  notice();
  try {
    const value = await api("/api/rooms/" + room + "/state");
    if (turn === serial) showState(value);
  } catch (e) {
    if (turn === serial) { $("status").textContent = "Room unavailable"; notice(e.message); }
  }
}
async function save(resource, data, condition, dialog, errorId) {
  if (busy || !state?.online) return;
  lock(true);
  const target = room;
  try {
    const updated = await api("/api/rooms/" + target + "/" + resource, data, condition);
    if (target !== room) return;
    if (dialog) $(dialog).close();
    showState(updated);
    notice(resource === "preferences" ? "Room preferences saved." : "Alarm schedule updated.");
  } catch (e) {
    if (errorId) $(errorId).textContent = e.message;
    notice(e.message);
  } finally { lock(false); }
}
function repeatFields() {
  const date = $("repeat").value === "date", weekly = $("repeat").value === "days";
  $("dateRow").hidden = !date; $("onDate").required = date; $("week").hidden = !weekly;
}
function edit(alarm = null) {
  editing = alarm;
  $("editorTitle").textContent = alarm ? "Edit alarm" : "Add alarm";
  $("alarmName").value = alarm?.name || ""; $("alarmName").readOnly = !!alarm;
  $("at").value = alarm?.at || ""; $("onDate").value = alarm?.date || "";
  $("repeat").value = alarm?.days.length ? "days" : alarm?.date ? "date" : "next";
  [...$("days").querySelectorAll("input")].forEach((el, i) => el.checked = !!alarm?.days.includes(i));
  repeatFields(); $("formError").textContent = ""; $("editor").showModal();
}
function action(kind, alarm) {
  pending = {kind, alarm};
  const verbs = {snooze:"Snooze", dismiss:"Dismiss", cancel:"Cancel alarm"};
  $("actionTitle").textContent = verbs[kind];
  $("actionText").textContent = kind === "cancel" ? "Remove the schedule for “" + alarm.name + "”?"
    : (kind === "dismiss" ? "End this occurrence of “" : "Snooze “") + alarm.name + "”?";
  $("snoozeRow").hidden = kind !== "snooze"; $("snoozeMinutes").value = 9;
  $("confirmAction").textContent = verbs[kind]; $("actionError").textContent = "";
  $("actionDialog").showModal();
}
function phoneList(phones) {
  $("phones").replaceChildren();
  for (const phone of phones) {
    const li = node("li", undefined, "alarm");
    const info = node("div");
    info.append(node("strong", phone.label), node("span", "Expires " +
      new Date(phone.expires * 1000).toLocaleDateString(), "meta"));
    li.append(info, button("Revoke", async () => {
      if (!confirm("Revoke Vector access for “" + phone.label + "”?")) return;
      try { phoneList((await api("/api/phones/" + phone.id + "/revoke", {})).phones); notice("Phone access revoked."); }
      catch (e) { notice(e.message); }
    }));
    $("phones").append(li);
  }
}
async function loadPhones() { phoneList((await api("/api/phones")).phones); }
document.querySelectorAll("[data-close]").forEach(b => b.onclick = () => $(b.dataset.close).close());
document.querySelectorAll("dialog").forEach(d => d.oncancel = e => { if (busy) e.preventDefault(); });
dayNames.forEach((name, i) => {
  const l = node("label"), input = node("input");
  input.type = "checkbox"; input.value = i;
  l.append(input, node("span", name)); $("days").append(l);
});
$("repeat").onchange = repeatFields;
$("room").onchange = () => { closeDialogs(); room = $("room").value; loadRoom(); };
$("refreshPhones").onclick = () => loadPhones().catch(e => notice(e.message));
$("refresh").onclick = loadRoom; $("retry").onclick = boot; $("add").onclick = () => edit();
$("alarmForm").onsubmit = e => {
  e.preventDefault();
  const days = $("repeat").value === "days" ? [...$("days").querySelectorAll(":checked")].map(x => +x.value) : [];
  if ($("repeat").value === "days" && !days.length) { $("formError").textContent = "Choose at least one weekday."; return; }
  const name = $("alarmName").value.trim().replace(/\s+/g, " ");
  if (!editing && state.alarms.some(a => a.name.toLowerCase() === name.toLowerCase())) {
    $("formError").textContent = "This alarm already exists. Open it with Edit."; return;
  }
  save("alarms", {action:"set", name, at:$("at").value, days,
    date:$("repeat").value === "date" ? $("onDate").value : null, minutes:null},
    editing ? {"If-Match":editing.revision} : {"If-None-Match":"*"}, "editor", "formError");
};
$("actionForm").onsubmit = e => {
  e.preventDefault();
  save("alarms", {action:pending.kind, name:pending.alarm.name, at:null, date:null,
    days:[], minutes:pending.kind === "snooze" ? +$("snoozeMinutes").value : null},
    {"If-Match":pending.alarm.revision}, "actionDialog", "actionError");
};
$("preferences").onsubmit = e => {
  e.preventDefault(); save("preferences", {color:$("color").value, brightness:+$("brightness").value,
    spoilers:$("spoilers").value}, {"If-Match":state.preferences_revision});
};
$("login").onclick = async () => {
  try { location.assign((await api("/auth/login", {})).location); }
  catch (e) { notice(e.message); }
};
$("redeem").onsubmit = async e => {
  e.preventDefault();
  const code = $("code").value; $("code").value = "";
  try { await api("/auth/pair", {code}); await boot(); } catch (err) { notice(err.message); }
};
$("issue").onsubmit = async e => {
  e.preventDefault();
  try {
    const result = await api("/api/pairing", {label:$("phoneLabel").value.trim()});
    $("pairCode").textContent = result.code; $("pairDialog").showModal();
    codeTimer = setTimeout(() => $("pairDialog").close(), result.expires_in * 1000);
  } catch (err) { notice(err.message); }
};
$("pairDialog").onclose = () => {
  clearTimeout(codeTimer); $("pairCode").textContent = "";
  if (session?.can_pair) loadPhones().catch(e => notice(e.message));
};
$("logout").onclick = async () => {
  try { await api("/auth/logout", {}); loggedOut("Signed out."); } catch (e) { notice(e.message); }
};
window.addEventListener("pageshow", e => { if (e.persisted) location.reload(); });
boot();
