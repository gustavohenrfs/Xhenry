const fmt = new Intl.NumberFormat("pt-BR");
const titles = { dashboard: "Dashboard", ops: "Operações", post: "Verificar Post", cloaker: "Cloaker", settings: "Configurações", archived: "Arquivadas" };
const TOKEN_KEY = "access_token";
const state = { tab: "dashboard", period: "today", folderId: null, data: null, openId: null, authMode: "login" };
let lineChart = null;
let barChart = null;
let toastTimer = 0;
let croppedFile = null;
let cropper = null;
if (window.Chart) {
  Chart.defaults.color = "#b39ab4";
  Chart.defaults.borderColor = "rgba(255,46,200,.12)";
  Chart.defaults.font.family = "Outfit, sans-serif";
  Chart.defaults.animation.duration = 700;
}
function num(value) { return value === null || value === undefined ? "—" : fmt.format(value); }
function toast(message) {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.add("hidden"), 4200);
}
function detailOf(body, fallback) {
  if (!body) return fallback;
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg || String(item)).join(" ");
  return fallback;
}
function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
function logout() {
  localStorage.removeItem(TOKEN_KEY);
  location.reload();
}
function showAuth(on) {
  document.getElementById("auth-modal").classList.toggle("off", !on);
}
function paintUser(user) {
  const name = user ? (user.nome_exibicao || user.email || "—") : "—";
  document.querySelectorAll("[data-user-name]").forEach((el) => { el.textContent = name; });
  const greet = document.getElementById("welcome-name");
  if (greet && user) {
    const handle = user.nome_exibicao || user.name || (user.email ? user.email.split("@")[0] : "Usuário");
    greet.textContent = handle;
  }
  document.querySelectorAll("[data-user-avatar]").forEach((el) => {
    el.replaceChildren();
    const url = user && user.avatar_url;
    if (url && (String(url).startsWith("https://") || String(url).startsWith("data:") || String(url).startsWith("/"))) {
      const img = document.createElement("img");
      if (String(url).startsWith("data:")) {
        img.src = url;
      } else if (String(url).startsWith("/")) {
        img.src = url + "?v=" + Date.now();
      } else {
        img.src = url;
      }
      img.alt = "";
      img.className = "h-full w-full rounded-full object-cover";
      el.append(img);
    } else {
      el.textContent = name.slice(0, 1).toUpperCase();
    }
  });
}
function setAuthMode(mode) {
  state.authMode = mode;
  const register = mode === "register";
  document.getElementById("auth-title").textContent = register ? "Criar conta" : "Entrar";
  document.getElementById("auth-name").classList.toggle("hidden", !register);
  document.getElementById("auth-name-label").classList.toggle("hidden", !register);
  document.getElementById("auth-name").required = register;
  document.querySelector("#auth-form button[type=submit]").textContent = register ? "Criar conta" : "Entrar";
  document.getElementById("auth-switch").textContent = register ? "Já tenho conta" : "Criar conta";
}
// Envia o Bearer e lança o detail do servidor em caso de erro; devolve o Response cru.
async function apiResponse(path, options) {
  const opts = Object.assign({}, options);
  const headers = new Headers(opts.headers || {});
  if (token() && !headers.has("Authorization")) headers.set("Authorization", "Bearer " + token());
  opts.headers = headers;
  const response = await fetch(path, opts);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(detailOf(body, response.statusText));
  }
  return response;
}
async function api(path, options) {
  const response = await apiResponse(path, options);
  if (response.status === 204) return null;
  return response.json().catch(() => ({}));
}
function setPeriod(period) {
  state.period = period;
  document.querySelectorAll(".period").forEach((button) => {
    const on = button.dataset.period === period;
    button.classList.toggle("bg-neon", on);
    button.classList.toggle("text-ink", on);
    button.classList.toggle("text-mute", !on);
  });
}
function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".nav-btn").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll(".pane").forEach((pane) => pane.classList.toggle("on", pane.id === "pane-" + tab));
  document.getElementById("heading").textContent = titles[tab];
  const controls = document.getElementById("dashboard-controls");
  if (controls) controls.classList.toggle("hidden", tab !== "dashboard");
  if (tab === "settings") loadSettings();
  if (tab === "archived") loadArchived();
}
function kpi(label, value, hot, trend) {
  const card = document.createElement("article");
  card.className = "glow kpi-card flex min-h-24 flex-col items-center justify-center rounded-3xl border bg-card px-4 py-4 relative text-center items-center justify-center" + (hot ? " border-neon/50 shadow-[0_0_28px_rgba(255,46,200,.12)]" : " border-neon/20");
  const k = document.createElement("p");
  k.className = "text-[11px] uppercase tracking-[0.16em] text-mute";
  k.textContent = label;
  const v = document.createElement("p");
  v.className = "mt-2 font-mono text-3xl text-white tracking-tight";
  v.textContent = value;
  if (trend) {
    const t = document.createElement("span");
    t.className = "absolute top-3 right-4 text-lg";
    t.textContent = trend === "up" ? "📈" : "📉";
    card.append(t);
  }
  card.append(k, v);
  return card;
}
function renderKpis(kpis) {
  document.getElementById("kpis").replaceChildren(
    kpi("Views", num(kpis.views), true, kpis.trend_views),
    kpi("Curtidas", num(kpis.likes), false, kpis.trend_likes),
    kpi("Retweets", num(kpis.retweets), false, kpis.trend_retweets),
    kpi("Replies", num(kpis.replies), false, kpis.trend_replies),
    kpi("Seguidores", num(kpis.followers), false, kpis.trend_followers),
    kpi("Contas", num(kpis.accounts), false)
  );
}
function chartOptions() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { labels: { boxWidth: 10 } } },
    scales: {
      x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } },
      y: { beginAtZero: true, ticks: { precision: 0 } }
    }
  };
}
function renderCharts(data) {
  if (!window.Chart) return;
  const lineData = {
    labels: data.series.map((row) => row.date.slice(5)),
    datasets: [{
      label: "Engajamento",
      data: data.series.map((row) => row.engagement),
      borderColor: "#ff2ec8",
      backgroundColor: "rgba(255,46,200,.16)",
      fill: true,
      tension: 0.35,
      pointRadius: data.series.length > 40 ? 0 : 3,
      borderWidth: 2
    }]
  };
  const names = data.by_account.map((row) => "@" + row.username);
  const hasViews = data.by_account.some((row) => row.views !== null && row.views !== undefined);
  document.getElementById("bar-caption").textContent = hasViews ? "Engajamento e views" : "Engajamento — views ainda não vieram da API";
  const barData = {
    labels: names.length ? names : ["—"],
    datasets: [
      { label: "Engajamento", data: names.length ? data.by_account.map((row) => row.engagement) : [0], backgroundColor: "#ff2ec8", borderRadius: 8, maxBarThickness: 28 },
      { label: "Views", data: names.length ? data.by_account.map((row) => row.views) : [null], backgroundColor: "rgba(255,214,242,.75)", borderRadius: 8, maxBarThickness: 28 }
    ]
  };
  // Atualiza os gráficos no lugar (sem destruir/recriar), para não piscar no auto-refresh.
  if (lineChart) { lineChart.data = lineData; lineChart.update("none"); }
  else lineChart = new Chart(document.getElementById("line-chart"), { type: "line", data: lineData, options: chartOptions() });
  if (barChart) { barChart.data = barData; barChart.update("none"); }
  else barChart = new Chart(document.getElementById("bar-chart"), { type: "bar", data: barData, options: chartOptions() });
}
function renderAccounts(accounts) {
  const host = document.getElementById("accounts");
  const visible = accounts.filter((a) => !a.is_archived);
  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "text-sm text-mute";
    empty.textContent = "Nenhuma conta ainda. Abra Operações e crie uma pasta.";
    host.replaceChildren(empty);
    return;
  }
  host.replaceChildren(...visible.map((account) => {
    const card = document.createElement("article");
    card.className = "account-card glow relative cursor-pointer rounded-2xl border border-neon/20 bg-card p-4 text-center flex flex-col items-center justify-center" + (state.openId === account.id ? " is-open" : "");
    card.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      state.openId = state.openId === account.id ? null : account.id;
      renderAccounts(accounts);
    });
    const name = document.createElement("p");
    name.className = "font-medium text-white";
    name.textContent = account.display_name || "@" + account.username;
    const handle = document.createElement("p");
    handle.className = "text-sm text-mute";
    handle.textContent = "@" + account.username + " · " + account.folder_name;
    const grid = document.createElement("dl");
    grid.className = "mt-4 grid grid-cols-2 items-center justify-center gap-x-2 gap-y-3 text-center text-sm sm:grid-cols-4";
    [["Views", num(account.views)], ["Curtidas", num(account.likes)], ["Retweets", num(account.retweets)], ["Seguidores", num(account.followers)]].forEach(([label, value]) => {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt");
      dt.className = "text-[10px] uppercase tracking-[0.12em] text-mute";
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.className = "mt-1 text-white";
      dd.textContent = value;
      wrap.append(dt, dd);
      grid.append(wrap);
    });
    const reveal = document.createElement("div");
    reveal.className = "status-reveal mt-4 flex items-center gap-2";
    const badge = document.createElement("span");
    badge.className = "rounded-full border px-2.5 py-1 text-[11px] uppercase tracking-[0.12em] st-" + (account.status || "none");
    badge.textContent = account.status_label;
    const check = document.createElement("button");
    check.type = "button";
    check.className = "rounded-full border border-neon/40 px-3 py-1 text-xs";
    check.textContent = "Verificar";
    check.addEventListener("click", () => verify(account));
    reveal.append(badge, check);
    const note = document.createElement("p");
    note.className = "status-reveal mt-2 text-[11px] text-mute";
    note.textContent = account.detail || "Passe o mouse ou clique para ver o status.";
    card.append(name, handle, grid, reveal, note);
    return card;
  }));
}
function renderFolders(folders) {
  const host = document.getElementById("folder-cards");
  if (!folders.length) {
    const empty = document.createElement("p");
    empty.className = "text-sm text-mute";
    empty.textContent = "Nenhuma pasta. Crie a primeira acima.";
    host.replaceChildren(empty);
    return;
  }
  host.replaceChildren(...folders.map((folder) => {
    const card = document.createElement("div");
    const on = state.folderId === folder.id;
    card.className = "glow flex items-start justify-between gap-3 rounded-3xl border px-4 py-5 text-left " + (on ? "border-neon bg-neon/10 shadow-[0_0_28px_rgba(255,46,200,.18)]" : "border-neon/20 bg-card");
    const info = document.createElement("button");
    info.type = "button";
    info.className = "min-w-0 flex-1 text-left";
    const name = document.createElement("p");
    name.className = "font-display text-2xl text-white";
    name.textContent = folder.name;
    const meta = document.createElement("p");
    meta.className = "mt-2 text-xs uppercase tracking-[0.14em] text-mute";
    meta.textContent = folder.accounts + " contas";
    info.append(name, meta);
    info.addEventListener("click", () => { state.folderId = folder.id; load(); });
    const actions = document.createElement("div");
    actions.className = "flex shrink-0 gap-2";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "rounded-full border border-neon/40 px-3 py-1 text-xs";
    edit.textContent = "Editar";
    edit.addEventListener("click", () => editFolder(folder));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "rounded-full border border-neon/40 px-3 py-1 text-xs text-mute";
    remove.textContent = "Excluir";
    remove.addEventListener("click", () => deleteFolder(folder));
    actions.append(edit, remove);
    card.append(info, actions);
    return card;
  }));
}
function renderOpsAccounts(accounts) {
  const host = document.getElementById("ops-accounts");
  if (!state.folderId) { host.replaceChildren(); return; }
  const rows = accounts.filter((account) => account.folder_id === state.folderId && !account.is_archived);
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "text-sm text-mute";
    empty.textContent = "Nenhuma conta ativa nesta pasta.";
    host.replaceChildren(empty);
    return;
  }
  host.replaceChildren(...rows.map((account) => {
    const card = document.createElement("article");
    card.className = "glow rounded-2xl border border-neon/20 bg-card p-4";
    const name = document.createElement("p");
    name.className = "font-medium text-white";
    name.textContent = account.display_name || "@" + account.username;
    const handle = document.createElement("p");
    handle.className = "text-sm text-mute";
    handle.textContent = "@" + account.username + " · " + account.folder_name;
    const grid = document.createElement("dl");
    grid.className = "mt-3 grid grid-cols-2 gap-x-2 gap-y-3 text-sm sm:grid-cols-4";
    [["Views", num(account.views)], ["Curtidas", num(account.likes)], ["Retweets", num(account.retweets)], ["Seguidores", num(account.followers)]].forEach(([label, value]) => {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt");
      dt.className = "text-[10px] uppercase tracking-[0.12em] text-mute";
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.className = "mt-1 text-white";
      dd.textContent = value;
      wrap.append(dt, dd);
      grid.append(wrap);
    });
    const actions = document.createElement("div");
    actions.className = "mt-4 flex flex-wrap gap-2";
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "rounded-full border border-neon/40 px-3 py-1 text-xs";
    editBtn.textContent = "Editar";
    editBtn.addEventListener("click", () => editAccount(account));
    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "rounded-full border border-neon/40 px-3 py-1 text-xs text-mute";
    deleteBtn.textContent = "Excluir";
    deleteBtn.addEventListener("click", () => removeAccount(account));
    actions.append(editBtn, deleteBtn);
    card.append(name, handle, grid, actions);
    return card;
  }));
}
function renderLastCollect(iso) {
  const el = document.getElementById("last-collect");
  if (!el) return;
  el.classList.remove("text-amber-300");
  if (!iso) { el.textContent = "Última coleta: nenhuma ainda"; return; }
  const when = new Date(iso);
  const mins = Math.max(0, Math.round((Date.now() - when.getTime()) / 60000));
  const stamp = when.toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const ago = mins < 1 ? "agora" : mins < 60 ? "há " + mins + " min" : mins < 1440 ? "há " + Math.floor(mins / 60) + " h" : "há " + Math.floor(mins / 1440) + " d";
  el.textContent = "Última coleta: " + stamp + " (" + ago + ")";
  if (mins > 90) el.classList.add("text-amber-300");
}
let loadSeq = 0;
let loadInflight = false;
let lastSig = "";
async function load({ silent = false } = {}) {
  if (silent && loadInflight) return;       // auto-refresh nunca empilha requisições
  const seq = ++loadSeq;
  loadInflight = true;
  const query = new URLSearchParams({ period: state.period });
  if (state.folderId) query.set("folder_id", String(state.folderId));
  try {
    const data = await api("/api/dashboard?" + query.toString());
    if (seq !== loadSeq) return;            // chegou uma resposta mais nova: descarta esta
    renderLastCollect(data.last_collected_at);
    const sig = JSON.stringify(data);
    if (silent && sig === lastSig) return;  // nada mudou: não mexe no DOM (zero flicker)
    lastSig = sig;
    state.data = data;
    document.getElementById("banner").classList.add("hidden");
    document.getElementById("range-label").textContent = data.start + " → " + data.end;
    renderKpis(data.kpis);
    renderCharts(data);
    renderAccounts(data.accounts);
    renderFolders(data.folders);
    renderOpsAccounts(data.accounts);
  } catch (error) {
    if (!silent) toast(error.message || "Falha ao carregar.");
  } finally {
    if (seq === loadSeq) loadInflight = false;
  }
}
async function verify(account) {
  try {
    const result = await api("/api/check/" + encodeURIComponent(account.username) + "?folder_id=" + account.folder_id);
    toast("@" + account.username + ": " + result.status_label);
    await load();
  } catch (error) { toast(error.message || "Falha na verificação."); }
}
async function editFolder(folder) {
  const name = await showPrompt("Novo nome da pasta", folder.name);
  if (!name || !name.trim() || name.trim() === folder.name) return;
  try {
    await api("/api/folders/" + folder.id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() })
    });
    await load();
  } catch (error) { toast(error.message || "Não editou a pasta."); }
}
async function deleteFolder(folder) {
  const ok = await showConfirm("Excluir pasta", "Tem certeza que deseja excluir a pasta \"" + folder.name + "\"?");
  if (!ok) return;
  try {
    await api("/api/folders/" + folder.id, { method: "DELETE" });
    if (state.folderId === folder.id) state.folderId = null;
    await load();
  } catch (error) { toast(error.message || "Não excluiu a pasta."); }
}
async function removeAccount(account) {
  const ok = await showConfirm("Remover conta", "Tem certeza que deseja remover @" + account.username + "?");
  if (!ok) return;
  try { await api("/api/accounts/" + account.id, { method: "DELETE" }); await load(); }
  catch (error) { toast(error.message || "Não removeu."); }
}

async function editAccount(account) {
  const newName = await showPrompt("Novo nome de exibição para @" + account.username, account.display_name || "");
  if (!newName || newName.trim() === "") return;
  try {
    await api("/api/accounts/" + account.id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: newName.trim() })
    });
    await load();
  } catch (error) { toast(error.message || "Não editou."); }
}


document.getElementById("nav").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (button) setTab(button.dataset.tab);
});
document.getElementById("mobile-nav").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (button) setTab(button.dataset.tab);
});
document.getElementById("period-wrap").addEventListener("click", (event) => {
  const button = event.target.closest("[data-period]");
  if (!button) return;
  setPeriod(button.dataset.period);
  load();
});
document.getElementById("folder-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const created = await api("/api/folders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: document.getElementById("folder-name").value, color: "#FF2EC8" })
    });
    event.target.reset();
    state.folderId = created.id;
    await load();
  } catch (error) { toast(error.message || "Não criou a pasta."); }
});
document.getElementById("account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.folderId) { toast("Selecione uma pasta."); return; }
  try {
    const created = await api("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder_id: state.folderId,
        username: document.getElementById("account-handle").value,
        display_name: document.getElementById("account-label").value
      })
    });
    document.getElementById("account-handle").value = "";
    document.getElementById("account-label").value = "";
    if (created.warning) toast(created.warning);
    await load();
  } catch (error) { toast(error.message || "Não adicionou a conta."); }
});
document.getElementById("post-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const verdict = document.getElementById("verdict");
  verdict.classList.remove("hidden");
  verdict.textContent = "Analisando…";
  const body = new FormData();
  body.set("caption", document.getElementById("post-caption").value);
  const file = document.getElementById("post-media").files[0];
  if (file) body.set("media", file);
  try {
    const result = await api("/api/posts/review", { method: "POST", body });
    verdict.textContent = result.verdict;
    // Auto-reset após 1 minuto
    setTimeout(() => {
      document.getElementById("post-caption").value = "";
      document.getElementById("post-media").value = "";
      verdict.classList.add("hidden");
    }, 60000);
  } catch (error) {
    verdict.textContent = error.message || "Não foi possível analisar o post.";
  }
});

// Botão Limpar manual
const clearBtn = document.createElement("button");
clearBtn.type = "button";
clearBtn.className = "rounded-2xl border border-neon/40 px-4 py-2 text-sm ml-2";
clearBtn.textContent = "Limpar";
clearBtn.onclick = () => {
  document.getElementById("post-caption").value = "";
  document.getElementById("post-media").value = "";
  document.getElementById("verdict").classList.add("hidden");
};
document.getElementById("post-form").appendChild(clearBtn);

// Nota sobre fotos
const note = document.createElement("p");
note.className = "text-[10px] text-mute mt-1";
note.textContent = "(o arquivo só aceita fotos por enquanto)";
document.getElementById("post-form").appendChild(note);

// Cloaker: gera N variantes da mídia no servidor e entrega um .zip
const cloaker = { intensity: "leve", zipUrl: "", previewUrl: "", originalRow: null, inFlight: false, phase: "", progressPct: 0 };
// Mesmos limites de app/cloaker.py; conferidos aqui para não subir o arquivo à toa.
const CLOAKER_MAX_VIDEO_BYTES = 100e6;
const CLOAKER_MAX_VIDEO_COPIES = 20;
const CLOAKER_NOTES = {
  leve: "Recorte de 1–2 px e ajuste de tom de até 2%.",
  media: "Recorte de 2–4 px e ajuste de tom de até 4%. Mais diferença, ainda invisível."
};
const cloakerForm = document.getElementById("cloaker-form");
const cloakerMedia = document.getElementById("cloaker-media");
const cloakerDrop = document.getElementById("cloaker-drop");
const cloakerCopies = document.getElementById("cloaker-copies");
const cloakerSubmit = document.getElementById("cloaker-submit");

function cloakerFile() { return cloakerMedia.files[0] || null; }
function isVideo(file) { return Boolean(file && file.type.startsWith("video/")); }
function versionsLabel(n) { return n === 1 ? "1 versão" : n + " versões"; }
function sizeLabel(bytes) {
  return bytes >= 1e6 ? (bytes / 1e6).toFixed(1).replace(".", ",") + " MB" : Math.max(1, Math.round(bytes / 1e3)) + " KB";
}

function syncCloakerControls() {
  const file = cloakerFile();
  const max = isVideo(file) ? CLOAKER_MAX_VIDEO_COPIES : 20;
  cloakerCopies.max = String(max);
  if (Number(cloakerCopies.value) > max) cloakerCopies.value = String(max);
  const copies = Number(cloakerCopies.value);
  document.getElementById("cloaker-copies-out").textContent = copies;
  cloakerSubmit.textContent = cloaker.inFlight ? "Gerando…" : "Gerar " + versionsLabel(copies);
  cloakerSubmit.disabled = !file || cloaker.inFlight;
  cloakerMedia.disabled = cloaker.inFlight;
  document.querySelectorAll("[data-intensity]").forEach((button) => {
    const on = button.dataset.intensity === cloaker.intensity;
    button.classList.toggle("bg-neon", on);
    button.classList.toggle("text-ink", on);
    button.classList.toggle("text-mute", !on);
    button.setAttribute("aria-checked", String(on));
  });
  document.getElementById("cloaker-intensity-note").textContent = CLOAKER_NOTES[cloaker.intensity];
}

// Faixa de 16 células: cada uma vem de 4 caracteres do sha256, no tom entre roxo e rosa neon.
function fingerprintStrip(sha256) {
  const strip = document.createElement("div");
  strip.className = "flex h-3 w-full gap-px overflow-hidden rounded";
  strip.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 16; i += 1) {
    const chunk = sha256.slice(i * 4, i * 4 + 4);
    const hue = 268 + (parseInt(chunk.slice(0, 2), 16) / 255) * 48;
    const light = 36 + (parseInt(chunk.slice(2), 16) / 255) * 34;
    const cell = document.createElement("span");
    cell.className = "flex-1";
    cell.style.background = "hsl(" + hue.toFixed(1) + " 88% " + light.toFixed(1) + "%)";
    strip.append(cell);
  }
  return strip;
}

function cloakerRow(name, sha256, detail, muted) {
  const row = document.createElement("li");
  row.className = "space-y-1.5";
  const head = document.createElement("div");
  head.className = "flex items-baseline justify-between gap-3 text-sm";
  const label = document.createElement("span");
  label.className = "min-w-0 truncate " + (muted ? "text-mute" : "text-white");
  label.textContent = name;
  const hash = document.createElement("span");
  hash.className = "shrink-0 font-mono text-xs text-mute";
  hash.title = sha256;
  hash.textContent = sha256.slice(0, 8) + (detail ? " · " + detail : "");
  head.append(label, hash);
  row.append(head, fingerprintStrip(sha256));
  if (muted) row.classList.add("opacity-60");
  return row;
}

function resetCloakerResults() {
  if (cloaker.zipUrl) URL.revokeObjectURL(cloaker.zipUrl);
  cloaker.zipUrl = "";
  const list = document.getElementById("cloaker-list");
  if (cloaker.originalRow) list.replaceChildren(cloaker.originalRow);
  else list.replaceChildren();
  const download = document.getElementById("cloaker-download");
  download.classList.add("hidden");
  download.classList.remove("block");
  download.removeAttribute("href");
}

async function sha256Hex(file) {
  if (!window.crypto || !crypto.subtle) return "";
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function onCloakerFile() {
  const file = cloakerFile();
  cloaker.originalRow = null;
  resetCloakerResults();
  if (cloaker.previewUrl) URL.revokeObjectURL(cloaker.previewUrl);
  cloaker.previewUrl = "";
  const preview = document.getElementById("cloaker-preview");
  preview.replaceChildren();
  preview.classList.toggle("hidden", !file);
  document.getElementById("cloaker-empty").classList.toggle("hidden", Boolean(file));
  document.getElementById("cloaker-drop-title").textContent = file ? file.name : "Solte a imagem ou o vídeo aqui";
  document.getElementById("cloaker-drop-hint").textContent = file
    ? sizeLabel(file.size) + ". Clique para trocar."
    : "ou clique para escolher. JPG, PNG ou WebP até 15 MB; MP4 ou MOV até 100 MB.";
  syncCloakerControls();
  if (!file) return;
  cloaker.previewUrl = URL.createObjectURL(file);
  const media = document.createElement(isVideo(file) ? "video" : "img");
  media.src = cloaker.previewUrl;
  media.className = "max-h-64 w-full rounded-2xl bg-ink object-contain";
  if (isVideo(file)) { media.muted = true; media.controls = true; } else { media.alt = "Prévia do original"; }
  preview.append(media);
  const original = await sha256Hex(file).catch(() => "");
  if (original && cloakerFile() === file) {
    cloaker.originalRow = cloakerRow("Original", original, sizeLabel(file.size), true);
    document.getElementById("cloaker-list").prepend(cloaker.originalRow);
  }
}

cloakerMedia.addEventListener("change", onCloakerFile);
cloakerCopies.addEventListener("input", syncCloakerControls);
document.getElementById("cloaker-intensity").addEventListener("click", (event) => {
  const button = event.target.closest("[data-intensity]");
  if (!button) return;
  cloaker.intensity = button.dataset.intensity;
  syncCloakerControls();
});
["dragenter", "dragover"].forEach((type) => cloakerDrop.addEventListener(type, (event) => {
  event.preventDefault();
  cloakerDrop.classList.add("border-neon", "bg-neon/10");
}));
["dragleave", "drop"].forEach((type) => cloakerDrop.addEventListener(type, () => {
  cloakerDrop.classList.remove("border-neon", "bg-neon/10");
}));
cloakerDrop.addEventListener("drop", (event) => {
  event.preventDefault();
  if (cloaker.inFlight || !event.dataTransfer.files.length) return;
  cloakerMedia.files = event.dataTransfer.files;
  onCloakerFile();
});

// Barra única que só avança: envio 0–20%, cópias 20–90%, zip 90–93%, download 93–100%.
const CLOAKER_PHASES = { upload: [0, 20], processing: [20, 90], zipping: [90, 93], download: [93, 100] };

function showCloakerProgress(phase, fraction, label) {
  const [from, to] = CLOAKER_PHASES[phase];
  const pct = Math.max(cloaker.progressPct || 0, from + (to - from) * Math.min(1, Math.max(0, fraction)));
  cloaker.progressPct = pct;
  document.getElementById("cloaker-progress").classList.remove("hidden");
  document.getElementById("cloaker-progress-label").textContent = label;
  document.getElementById("cloaker-progress-pct").textContent = Math.round(pct) + "%";
  document.getElementById("cloaker-progress-bar").setAttribute("aria-valuenow", String(Math.round(pct)));
  const fill = document.getElementById("cloaker-progress-fill");
  fill.style.width = pct.toFixed(1) + "%";
  // Entre uma cópia e outra o número não muda; o pulso mostra que o servidor segue trabalhando.
  fill.classList.toggle("motion-safe:animate-pulse", phase === "processing" || phase === "zipping");
}

function hideCloakerProgress() {
  cloaker.progressPct = 0;
  document.getElementById("cloaker-progress").classList.add("hidden");
  document.getElementById("cloaker-progress-fill").style.width = "0%";
}

function randomJobId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// Enquanto o POST não responde, pergunta ao servidor quantas cópias já ficaram prontas.
function pollCloakerProgress(jobId) {
  let busy = false;
  const timer = setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      const state = await api("/api/cloaker/progress/" + jobId);
      if (!cloaker.inFlight || cloaker.phase === "download") return;
      if (state.stage === "zipping") {
        showCloakerProgress("zipping", 0.5, "Montando o .zip…");
      } else if (state.total) {
        const label = state.done >= state.total
          ? "Finalizando…"
          : "Gerando cópia " + (state.done + 1) + " de " + state.total + "…";
        showCloakerProgress("processing", state.done / state.total, label);
      }
    } catch (error) {
      // 404 enquanto o servidor ainda lê o arquivo: segue tentando.
    } finally {
      busy = false;
    }
  }, 1000);
  return () => clearInterval(timer);
}

// XHR em vez de fetch: só ele informa o progresso do envio.
function sendCloaker(body, onPhase) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/cloaker");
    xhr.responseType = "blob";
    if (token()) xhr.setRequestHeader("Authorization", "Bearer " + token());
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onPhase("upload", e.loaded / e.total); };
    xhr.upload.onload = () => onPhase("processing", 0);
    xhr.onreadystatechange = () => { if (xhr.readyState === XMLHttpRequest.HEADERS_RECEIVED) onPhase("download", 0); };
    xhr.onprogress = (e) => { if (e.lengthComputable) onPhase("download", e.loaded / e.total); };
    xhr.onerror = () => reject(new Error("Falha de conexão. Tente de novo."));
    xhr.onload = async () => {
      if (xhr.status === 200) {
        resolve({
          items: JSON.parse(atob(xhr.getResponseHeader("X-Cloaker-Manifest") || "") || "[]"),
          blob: xhr.response,
          disposition: xhr.getResponseHeader("Content-Disposition") || ""
        });
        return;
      }
      const text = xhr.response ? await xhr.response.text().catch(() => "") : "";
      let body = {};
      try { body = JSON.parse(text); } catch (error) { body = {}; }
      reject(new Error(detailOf(body, xhr.statusText || "Não foi possível gerar as versões.")));
    };
    xhr.send(body);
  });
}

cloakerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = cloakerFile();
  if (!file || cloaker.inFlight) return;
  if (isVideo(file) && file.size > CLOAKER_MAX_VIDEO_BYTES) {
    toast("Vídeo acima de " + CLOAKER_MAX_VIDEO_BYTES / 1e6 + " MB.");
    return;
  }
  const copies = Number(cloakerCopies.value);
  const jobId = randomJobId();
  const body = new FormData();
  body.set("media", file);
  body.set("copies", String(copies));
  body.set("intensity", cloaker.intensity);
  body.set("fresh_meta", String(document.getElementById("cloaker-fresh").checked));
  body.set("job_id", jobId);
  cloaker.inFlight = true;
  cloaker.phase = "upload";
  syncCloakerControls();
  showCloakerProgress("upload", 0, "Enviando arquivo… 0%");
  let stopPolling = () => {};
  try {
    const result = await sendCloaker(body, (phase, fraction) => {
      if (phase === "upload") {
        showCloakerProgress("upload", fraction, "Enviando arquivo… " + Math.round(fraction * 100) + "%");
      } else if (phase === "processing" && cloaker.phase === "upload") {
        cloaker.phase = "processing";
        showCloakerProgress("processing", 0, "Preparando " + versionsLabel(copies) + "…");
        stopPolling = pollCloakerProgress(jobId);
      } else if (phase === "download") {
        cloaker.phase = "download";
        stopPolling();
        showCloakerProgress("download", fraction, "Baixando o .zip… " + Math.round(fraction * 100) + "%");
      }
    });
    const list = document.getElementById("cloaker-list");
    resetCloakerResults();
    result.items.forEach((item) => list.append(cloakerRow(item.name, item.sha256, sizeLabel(item.size), false)));
    const match = /filename="([^"]+)"/.exec(result.disposition);
    cloaker.zipUrl = URL.createObjectURL(result.blob);
    const download = document.getElementById("cloaker-download");
    download.href = cloaker.zipUrl;
    download.download = match ? match[1] : "cloaker.zip";
    download.classList.remove("hidden");
    download.classList.add("block");
    toast(result.items.length === 1 ? "1 versão gerada." : result.items.length + " versões geradas.");
  } catch (error) {
    toast(error.message || "Não foi possível gerar as versões.");
  } finally {
    stopPolling();
    cloaker.inFlight = false;
    cloaker.phase = "";
    hideCloakerProgress();
    syncCloakerControls();
  }
});
syncCloakerControls();
async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    document.getElementById("tg-chat").value = settings.telegram_chat_id || "";
    document.getElementById("goal").value = settings.impression_goal;
    document.getElementById("tg-token").placeholder = settings.telegram_token_set ? "Token já salvo" : "123456:ABC...";
    document.getElementById("xai-key").placeholder = settings.xai_key_set ? "Chave já salva" : "xai-...";
    const me = await api("/api/me");
    document.getElementById("profile-name").value = me.nome_exibicao || "";
    paintUser(me);
  } catch (error) { toast(error.message || "Não leu as configurações."); }
}

async function loadArchived() {
  const host = document.getElementById("archived-accounts");
  if (!host) return;
  try {
    const data = await api("/api/dashboard?period=total");
    const archived = (data.accounts || []).filter(a => a.is_archived);
    if (!archived.length) {
      host.innerHTML = '<p class="text-sm text-mute">Nenhuma conta arquivada.</p>';
      return;
    }
    host.innerHTML = "";
    archived.forEach(account => {
      const div = document.createElement("div");
      div.className = "flex items-center justify-between rounded-2xl border border-neon/15 bg-card px-4 py-3";
      div.innerHTML = `
        <div>
          <p class="font-medium">${account.display_name || "@" + account.username}</p>
          <p class="text-xs text-mute">@${account.username}</p>
        </div>
        <button class="rounded-full border border-neon/40 px-3 py-1 text-xs">Desarquivar</button>
      `;
      div.querySelector("button").onclick = () => toggleArchive(account);
      host.appendChild(div);
    });
  } catch (e) {
    host.innerHTML = '<p class="text-sm text-mute">Erro ao carregar.</p>';
  }
}
document.getElementById("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = new FormData();
  body.set("nome_exibicao", document.getElementById("profile-name").value);
  const originalFile = document.getElementById("profile-photo").files[0];
  const avatarFile = croppedFile || originalFile;
  if (avatarFile) body.set("avatar", avatarFile);
  try {
    paintUser(await api("/api/me", { method: "PATCH", body }));
    document.getElementById("profile-photo").value = "";
    croppedFile = null;
    toast("Perfil salvo.");
  } catch (error) { toast(error.message || "Não salvou o perfil."); }
});

// Recorte quadrado 1080x1080 da foto de perfil (Cropper.js)
const profilePhotoInput = document.getElementById("profile-photo");
const cropModal = document.getElementById("crop-modal");
const cropImage = document.getElementById("crop-image");
const cropCancel = document.getElementById("crop-cancel");
const cropConfirm = document.getElementById("crop-confirm");

if (profilePhotoInput) {
  profilePhotoInput.addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      cropImage.src = ev.target.result;
      cropModal.classList.remove("off");
      if (cropper) cropper.destroy();
      cropper = new Cropper(cropImage, {
        aspectRatio: 1,
        viewMode: 1,
        autoCropArea: 1,
        responsive: true,
        background: false,
      });
    };
    reader.readAsDataURL(file);
  });
}

if (cropCancel) {
  cropCancel.addEventListener("click", () => {
    if (cropper) {
      cropper.destroy();
      cropper = null;
    }
    cropModal.classList.add("off");
    if (profilePhotoInput) profilePhotoInput.value = "";
    croppedFile = null;
  });
}

if (cropConfirm) {
  cropConfirm.addEventListener("click", () => {
    if (!cropper) return;
    const canvas = cropper.getCroppedCanvas({ width: 1080, height: 1080 });
    canvas.toBlob((blob) => {
      if (blob) {
        croppedFile = new File([blob], "avatar.jpg", { type: "image/jpeg" });
      }
      cropper.destroy();
      cropper = null;
      cropModal.classList.add("off");
    }, "image/jpeg", 0.9);
  });
}
document.getElementById("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    telegram_chat_id: document.getElementById("tg-chat").value,
    impression_goal: Number(document.getElementById("goal").value)
  };
  const token = document.getElementById("tg-token").value.trim();
  const xai = document.getElementById("xai-key").value.trim();
  if (token) payload.telegram_token = token;
  if (xai) payload.xai_api_key = xai;
  try {
    await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    document.getElementById("tg-token").value = "";
    document.getElementById("xai-key").value = "";
    toast("Configuração salva.");
    await loadSettings();
  } catch (error) { toast(error.message || "Não salvou."); }
});
document.getElementById("test-note").addEventListener("click", async () => {
  try {
    await api("/api/telegram/test", { method: "POST" });
    toast("Notificação de teste enviada.");
  } catch (error) { toast(error.message || "O teste falhou."); }
});

// Change Password
document.getElementById("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const nova = document.getElementById("new-password").value.trim();
  if (!nova || nova.length < 8) {
    toast("Senha deve ter pelo menos 8 caracteres.");
    return;
  }
  try {
    await api("/api/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nova_senha: nova })
    });
    document.getElementById("new-password").value = "";
    toast("Senha atualizada com sucesso.");
  } catch (error) { toast(error.message || "Falha ao trocar senha."); }
});

// Delete Account with CONFIRM modal
const deleteBtn = document.getElementById("delete-account-btn");
const deleteModal = document.getElementById("delete-modal");
const deleteInput = document.getElementById("delete-confirm-input");
const deleteConfirmBtn = document.getElementById("delete-confirm");
const deleteCancelBtn = document.getElementById("delete-cancel");

deleteBtn.addEventListener("click", () => {
  deleteInput.value = "";
  deleteConfirmBtn.disabled = true;
  deleteModal.classList.remove("hidden");
  deleteInput.focus();
});

deleteCancelBtn.addEventListener("click", () => {
  deleteModal.classList.add("hidden");
});

deleteInput.addEventListener("input", () => {
  deleteConfirmBtn.disabled = deleteInput.value.trim().toUpperCase() !== "CONFIRM";
});

deleteConfirmBtn.addEventListener("click", async () => {
  if (deleteInput.value.trim().toUpperCase() !== "CONFIRM") return;
  deleteModal.classList.add("hidden");
  try {
    await api("/api/me", { method: "DELETE" });
    toast("Conta excluída. Saindo...");
    setTimeout(() => {
      localStorage.removeItem(TOKEN_KEY);
      location.reload();
    }, 1200);
  } catch (error) {
    toast(error.message || "Falha ao excluir conta.");
  }
});

document.getElementById("refresh-all").addEventListener("click", async () => {
  const btn = document.getElementById("refresh-all");
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Atualizando...";
  try {
    await load();
    toast("Dashboard relido do banco.");
  } catch (error) { toast(error.message || "Falha ao atualizar."); }
  btn.disabled = false;
  btn.textContent = orig;
});
document.getElementById("auth-switch").addEventListener("click", () => {
  setAuthMode(state.authMode === "login" ? "register" : "login");
});
document.getElementById("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("auth-error");
  error.classList.add("hidden");
  const email = document.getElementById("auth-email").value;
  const senha = document.getElementById("auth-password").value;
  try {
    if (state.authMode === "register") {
      await api("/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nome: document.getElementById("auth-name").value, email, senha })
      });
    }
    const session = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, senha })
    });
    localStorage.setItem(TOKEN_KEY, session.access_token);
    paintUser(session.user);
    showAuth(false);
    setTab("dashboard");
    await load();
  } catch (err) {
    error.textContent = err.message || "Não entrou.";
    error.classList.remove("hidden");
  }
});
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-logout]")) logout();
});

// Modal de confirmação customizado
let confirmResolve = null;
let promptResolve = null;
function showConfirm(title, message) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-message").textContent = message;
    document.getElementById("confirm-modal").classList.remove("hidden");
  });
}

function showPrompt(title, defaultValue = "") {
  return new Promise((resolve) => {
    promptResolve = resolve;
    const titleEl = document.getElementById("confirm-title");
    const messageEl = document.getElementById("confirm-message");
    titleEl.textContent = title;
    messageEl.innerHTML = `<input id="prompt-input" type="text" class="w-full rounded-2xl border border-neon/25 bg-ink px-3 py-3 text-sm outline-none focus:border-neon" value="${defaultValue.replace(/"/g, '&quot;')}">`;
    document.getElementById("confirm-modal").classList.remove("hidden");
  });
}

document.getElementById("confirm-cancel").addEventListener("click", () => {
  document.getElementById("confirm-modal").classList.add("hidden");
  if (confirmResolve) confirmResolve(false);
  if (promptResolve) promptResolve(null);
});

document.getElementById("confirm-ok").addEventListener("click", () => {
  document.getElementById("confirm-modal").classList.add("hidden");
  if (confirmResolve) confirmResolve(true);
  if (promptResolve) {
    const input = document.getElementById("prompt-input");
    promptResolve(input ? input.value : null);
  }
});

// Toggle senha (olho) para campos de token e xai-key
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-toggle]");
  if (!btn) return;
  const id = btn.dataset.toggle;
  const input = document.getElementById(id);
  if (!input) return;
  if (input.type === "password") {
    input.type = "text";
    btn.textContent = "🙈";
  } else {
    input.type = "password";
    btn.textContent = "👁️";
  }
});

async function boot() {
  if (!token()) {
    showAuth(true);
    return;
  }
  try {
    paintUser(await api("/api/me"));
    showAuth(false);
    setTab("dashboard");
    await load();
  } catch (err) {
    localStorage.removeItem(TOKEN_KEY);
    showAuth(true);
  }
}
// Date display + native picker
const dateDisplay = document.getElementById("date-display");
const dateInput = document.getElementById("specific-date");
if (dateInput && !dateInput.value) dateInput.value = new Date().toISOString().split('T')[0];

function formatBR(d) {
  return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" }).replace(/\//g, "/");
}

function updateDateDisplay() {
  if (!dateDisplay || !dateInput) return;
  const val = dateInput.value;
  if (val) {
    const [y, m, d] = val.split("-").map(Number);
    const dt = new Date(y, m - 1, d);
    dateDisplay.textContent = formatBR(dt);
  } else {
    const today = new Date();
    dateDisplay.textContent = formatBR(today);
    dateInput.value = today.toISOString().slice(0, 10);
  }
}

if (dateDisplay && dateInput) {
  dateDisplay.addEventListener("click", () => {
    if (dateInput.showPicker) dateInput.showPicker();
    else dateInput.click();
  });
  dateInput.addEventListener("change", () => {
    updateDateDisplay();
    // optional: trigger load with new date if needed
    if (state.tab === "dashboard") load();
  });
  updateDateDisplay();
}

setPeriod("today");
setAuthMode("login");
// Auto-refresh: a cada 60s, só na aba Dashboard, com a página visível e logado.
const AUTO_REFRESH_MS = 60000;
function autoRefresh() {
  if (state.tab === "dashboard" && token() && !document.hidden) load({ silent: true });
}
setInterval(autoRefresh, AUTO_REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) autoRefresh(); });

boot();
