const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
try {
  tg?.setHeaderColor?.("#07060f");
  tg?.setBackgroundColor?.("#07060f");
} catch (_) { /* старые клиенты */ }

const state = { user: null, plans: [], collections: [], settings: null, attrs: {}, pickMode: false };
const $ = (sel) => document.querySelector(sel);

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": tg?.initData || "",
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Ошибка ${res.status}`);
  return data;
}

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), 3500);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function formatDate(ts) {
  return new Date(ts * 1000).toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

// --- статус подписки и блокировка настроек ---

function renderStatus() {
  const el = $("#status");
  const u = state.user;
  if (u.active) {
    const left = Math.max(0, Math.ceil((u.sub_until - Date.now() / 1000) / 86400));
    el.className = "card status ok";
    el.innerHTML = `<b>✅ Подписка активна</b><span class="muted">до ${formatDate(u.sub_until)} · осталось ${left} дн.</span>`;
  } else {
    el.className = "card status bad";
    el.innerHTML = `<b>❌ Нет подписки</b><span class="muted">Без подписки бот не присылает уведомления</span>`;
  }
  const locked = !u.active;
  document.querySelectorAll(".lock").forEach((x) => x.classList.toggle("hidden", !locked));
  document.querySelectorAll(".locked-content").forEach((x) => x.classList.toggle("hidden", locked));
}

document.querySelectorAll(".go-sub").forEach((b) => b.addEventListener("click", () => openTab("sub")));

// --- тарифы ---

function renderPlans() {
  const box = $("#plans");
  box.innerHTML = "";
  const daily = state.plans[0]?.price / state.plans[0]?.days;
  state.plans.forEach((p) => {
    const perDay = p.price / p.days;
    const save = daily && perDay < daily ? Math.round((1 - perDay / daily) * 100) : 0;
    const el = document.createElement("div");
    el.className = "plan" + (p.key === "quarter" ? " best" : "");
    el.innerHTML = `
      <div>
        <div class="name">${p.title}${save ? `<span class="badge">−${save}%</span>` : ""}</div>
        <div class="per">${perDay.toFixed(2)} $ в день</div>
      </div>
      <button class="btn">${p.price} $</button>`;
    el.querySelector("button").onclick = (e) => buy(p, e.currentTarget);
    box.appendChild(el);
  });
}

async function buy(plan, button) {
  button.disabled = true;
  try {
    const inv = await api("/api/invoices", { method: "POST", body: JSON.stringify({ plan: plan.key }) });
    if (tg?.openTelegramLink) tg.openTelegramLink(inv.link);
    else window.open(inv.link, "_blank");
    waitForPayment(inv.id);
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
}

function waitForPayment(id) {
  $("#pay-wait").classList.remove("hidden");
  clearInterval(waitForPayment.timer);
  const started = Date.now();
  waitForPayment.timer = setInterval(async () => {
    if (Date.now() - started > 30 * 60 * 1000) return stop();
    try {
      const res = await api(`/api/invoices/${id}`);
      if (res.status === "paid") {
        stop();
        tg?.HapticFeedback?.notificationOccurred("success");
        toast("✅ Оплата получена, подписка активна!");
        await load();
      } else if (res.status !== "active") {
        toast("Счёт истёк, создайте новый");
        stop();
      }
    } catch (_) { /* повторим */ }
  }, 4000);
  function stop() {
    clearInterval(waitForPayment.timer);
    $("#pay-wait").classList.add("hidden");
  }
}

// --- настройки уведомлений ---

function renderSettings() {
  document.querySelectorAll("[data-key]").forEach((input) => {
    const value = state.settings[input.dataset.key];
    if (input.type === "checkbox") input.checked = !!value;
    else input.value = value ?? "";
  });
}

let saveTimer;
function scheduleSave() {
  $("#save-state").textContent = "Сохранение…";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveSettings, 600);
}

async function saveSettings() {
  try {
    const res = await api("/api/settings", { method: "PUT", body: JSON.stringify(state.settings) });
    state.settings = res.settings;
    $("#save-state").textContent = "✓ Сохранено";
  } catch (e) {
    $("#save-state").textContent = "";
    toast(e.message);
  }
}

document.querySelectorAll("[data-key]").forEach((input) => {
  input.addEventListener(input.type === "checkbox" ? "change" : "input", () => {
    const key = input.dataset.key;
    if (input.type === "checkbox") state.settings[key] = input.checked;
    else state.settings[key] = input.value === "" ? null : Number(input.value);
    tg?.HapticFeedback?.selectionChanged();
    scheduleSave();
  });
});

// --- коллекции ---

function countText(n, empty) {
  return n ? `выбрано ${n}` : empty;
}

function collectionInfo(c) {
  const s = state.settings;
  const m = (s.models[c.id] || []).length;
  const p = (s.patterns[c.id] || []).length;
  const parts = [c.active ? `улучшено ${c.issued.toLocaleString("ru-RU")}` : "улучшений пока нет"];
  if (m) parts.push(`моделей: ${m}`);
  if (p) parts.push(`узоров: ${p}`);
  return parts.join(" · ");
}

function renderCollections() {
  const s = state.settings;
  const selected = new Set(s.collections);
  const all = selected.size === 0 && !state.pickMode;
  $("#all-collections").checked = all;
  $("#sel-count").textContent = all ? "уведомления по всем коллекциям"
    : selected.size ? `выбрано: ${selected.size}` : "отметьте нужные коллекции";
  $("#backdrops-info").textContent = countText(s.backdrops.length, "любые");

  const query = $("#search").value.trim().toLowerCase();
  const box = $("#collections");
  box.innerHTML = "";
  const items = state.collections.filter((c) => c.title.toLowerCase().includes(query));
  if (!items.length) {
    box.innerHTML = `<p class="muted" style="padding:12px 0">${state.collections.length ? "Ничего не найдено" : "Список коллекций появится после первого запуска мониторинга"}</p>`;
    return;
  }
  items.forEach((c) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span>${escapeHtml(c.title)}<small>${collectionInfo(c)}</small></span>
      ${all ? "" : `<input type="checkbox" class="switch" ${selected.has(c.id) ? "checked" : ""}>`}
      <span class="chev">›</span>`;
    const toggle = row.querySelector("input");
    if (toggle) {
      toggle.addEventListener("click", (e) => e.stopPropagation());
      toggle.addEventListener("change", () => setTracked(c.id, toggle.checked));
    }
    row.addEventListener("click", () => openSheet(c));
    box.appendChild(row);
  });
}

function setTracked(id, on) {
  const set = new Set(state.settings.collections);
  on ? set.add(id) : set.delete(id);
  state.settings.collections = [...set];
  scheduleSave();
  renderCollections();
}

$("#all-collections").addEventListener("change", (e) => {
  // Пустой список = все коллекции; pickMode лишь показывает переключатели
  state.pickMode = !e.target.checked;
  if (e.target.checked && state.settings.collections.length) {
    state.settings.collections = [];
    scheduleSave();
  }
  renderCollections();
});
$("#search").addEventListener("input", renderCollections);

// --- выбор моделей / узоров / фонов ---

const sheet = { collection: null, kind: "models" };

async function loadItems() {
  if (sheet.kind === "backdrops") {
    if (!state.backdrops) state.backdrops = (await api("/api/backdrops")).backdrops;
    return state.backdrops;
  }
  const id = sheet.collection.id;
  if (!state.attrs[id]) state.attrs[id] = await api(`/api/collections/${id}/attributes`);
  return state.attrs[id][sheet.kind];
}

function selectedList() {
  const s = state.settings;
  if (sheet.kind === "backdrops") return s.backdrops;
  return s[sheet.kind][sheet.collection.id] || [];
}

function setSelectedList(list) {
  const s = state.settings;
  if (sheet.kind === "backdrops") s.backdrops = list;
  else if (list.length) s[sheet.kind][sheet.collection.id] = list;
  else delete s[sheet.kind][sheet.collection.id];
  scheduleSave();
}

async function openSheet(collection, kind = "models") {
  sheet.collection = collection;
  sheet.kind = kind;
  const isBackdrops = kind === "backdrops";
  $("#sheet-title").textContent = isBackdrops ? "🎨 Фоны" : collection.title;
  $("#sheet-tabs").classList.toggle("hidden", isBackdrops);
  const tracking = !isBackdrops && state.settings.collections.length > 0;
  $("#sheet-track").classList.toggle("hidden", !tracking);
  if (tracking) $("#sheet-track-input").checked = state.settings.collections.includes(collection.id);
  $("#sheet-search").value = "";
  setSheetTab(kind);
  $("#sheet").classList.remove("hidden");
  document.body.classList.add("no-scroll");
  tg?.BackButton?.show();
  await renderChips();
}

function closeSheet() {
  $("#sheet").classList.add("hidden");
  document.body.classList.remove("no-scroll");
  tg?.BackButton?.hide();
  renderCollections();
}

function setSheetTab(kind) {
  document.querySelectorAll("#sheet-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.kind === kind));
}

async function renderChips() {
  const box = $("#chips");
  const info = $("#sheet-info");
  box.innerHTML = `<div class="loader"></div>`;
  let items;
  try {
    items = await loadItems();
  } catch (e) {
    box.innerHTML = `<p class="muted">${escapeHtml(e.message)}</p>`;
    const retry = document.createElement("button");
    retry.className = "btn ghost";
    retry.textContent = "Повторить";
    retry.onclick = renderChips;
    box.appendChild(retry);
    return;
  }
  const chosen = new Set(selectedList());
  const query = $("#sheet-search").value.trim().toLowerCase();
  const scope = sheet.kind === "backdrops" ? " (для всех коллекций)" : "";
  info.textContent = (chosen.size
    ? `Уведомления только при выбранных: ${chosen.size}`
    : "Ничего не выбрано — подходят любые") + scope;
  box.innerHTML = "";
  items.filter((a) => a.name.toLowerCase().includes(query)).forEach((a) => {
    const chip = document.createElement("button");
    chip.className = "chip" + (chosen.has(a.name) ? " on" : "");
    chip.innerHTML = `${escapeHtml(a.name)}${a.label ? `<small>${escapeHtml(a.label)}</small>` : ""}`;
    chip.onclick = () => {
      const set = new Set(selectedList());
      set.has(a.name) ? set.delete(a.name) : set.add(a.name);
      setSelectedList([...set].sort());
      tg?.HapticFeedback?.selectionChanged();
      renderChips();
    };
    box.appendChild(chip);
  });
  if (!box.children.length) box.innerHTML = `<p class="muted">Ничего не найдено</p>`;
}

document.querySelectorAll("#sheet-tabs .tab").forEach((t) => t.addEventListener("click", () => {
  sheet.kind = t.dataset.kind;
  setSheetTab(sheet.kind);
  renderChips();
}));
$("#sheet-search").addEventListener("input", renderChips);
$("#sheet-reset").addEventListener("click", () => { setSelectedList([]); renderChips(); });
$("#sheet-close").addEventListener("click", closeSheet);
$("#sheet-track-input").addEventListener("change", (e) => setTracked(sheet.collection.id, e.target.checked));
tg?.BackButton?.onClick(closeSheet);

$("#backdrops-row").addEventListener("click", () => openSheet(null, "backdrops"));

// --- вкладки ---

function openTab(name) {
  document.querySelectorAll(".tabs.main .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
}

document.querySelectorAll(".tabs.main .tab").forEach((tab) => {
  tab.addEventListener("click", () => openTab(tab.dataset.tab));
});

// --- загрузка ---

async function load() {
  const me = await api("/api/me");
  state.user = me.user;
  state.plans = me.plans;
  state.settings = me.user.settings;
  renderStatus();
  renderPlans();
  if (state.user.active) {
    state.collections = (await api("/api/collections")).collections;
    renderSettings();
    renderCollections();
  }
}

(async function init() {
  if (!tg?.initData) {
    $("#status").textContent = "Откройте мини-апп через бота Upgrade Gift в Telegram.";
    return;
  }
  try {
    await load();
  } catch (e) {
    $("#status").textContent = "Не удалось загрузить данные: " + e.message;
  }
})();
