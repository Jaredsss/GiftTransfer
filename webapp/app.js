const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const state = { user: null, plans: [], collections: [], settings: null };
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
  toast.timer = setTimeout(() => el.classList.add("hidden"), 2500);
}

function formatDate(ts) {
  return new Date(ts * 1000).toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

// --- статус подписки ---

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
}

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
        state.user = res.user;
        renderStatus();
        tg?.HapticFeedback?.notificationOccurred("success");
        toast("✅ Оплата получена, подписка активна!");
        stop();
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

// --- настройки ---

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

function renderCollections() {
  const selected = new Set(state.settings.collections);
  const all = selected.size === 0 && !state.pickMode;
  $("#all-collections").checked = all;
  $("#collections-box").classList.toggle("hidden", all);
  $("#sel-count").textContent = all ? "" : selected.size ? `Выбрано: ${selected.size}` : "Выберите коллекции (пока ничего не выбрано — приходят все)";

  const query = $("#search").value.trim().toLowerCase();
  const box = $("#collections");
  box.innerHTML = "";
  const items = state.collections.filter((c) => c.title.toLowerCase().includes(query));
  if (!items.length) {
    box.innerHTML = `<p class="muted" style="padding:12px 0">${state.collections.length ? "Ничего не найдено" : "Список коллекций появится после первого запуска мониторинга"}</p>`;
    return;
  }
  items.forEach((c) => {
    const row = document.createElement("label");
    row.className = "row";
    const info = c.active ? `улучшено ${c.issued.toLocaleString("ru-RU")}` : "улучшений пока нет";
    row.innerHTML = `<span>${escapeHtml(c.title)}<small>${info}</small></span>
      <input type="checkbox" class="switch" ${selected.has(c.id) ? "checked" : ""}>`;
    row.querySelector("input").addEventListener("change", (e) => {
      const set = new Set(state.settings.collections);
      e.target.checked ? set.add(c.id) : set.delete(c.id);
      state.settings.collections = [...set];
      scheduleSave();
      renderCollections();
    });
    box.appendChild(row);
  });
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

$("#all-collections").addEventListener("change", (e) => {
  // Пустой список = все коллекции; pickMode лишь показывает выбор
  state.pickMode = !e.target.checked;
  if (e.target.checked && state.settings.collections.length) {
    state.settings.collections = [];
    scheduleSave();
  }
  renderCollections();
});
$("#search").addEventListener("input", renderCollections);
$("#clear-sel").addEventListener("click", () => {
  state.pickMode = false;
  state.settings.collections = [];
  scheduleSave();
  renderCollections();
});

// --- вкладки ---

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".panel").forEach((p) => p.classList.add("hidden"));
    $(`#tab-${tab.dataset.tab}`).classList.remove("hidden");
  });
});

// --- старт ---

(async function init() {
  if (!tg?.initData) {
    $("#status").textContent = "Откройте мини-апп через бота Upgrade Gift в Telegram.";
    return;
  }
  try {
    const [me, cols] = await Promise.all([api("/api/me"), api("/api/collections")]);
    state.user = me.user;
    state.plans = me.plans;
    state.settings = me.user.settings;
    state.collections = cols.collections;
    renderStatus();
    renderPlans();
    renderSettings();
    renderCollections();
  } catch (e) {
    $("#status").textContent = "Не удалось загрузить данные: " + e.message;
  }
})();
