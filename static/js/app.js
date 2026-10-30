const fmt = new Intl.NumberFormat("pt-BR");
const titles = { dashboard: "Dashboard", radar: "Radar de trend", ops: "Operações", post: "Verificar post", cloaker: "Metadados", settings: "Configurações", archived: "Arquivadas", users: "Usuários" };
const TOKEN_KEY = "access_token";
const state = {
  tab: "dashboard", period: "today", folderId: null, opsFolderId: null, data: null, openId: null, authMode: "login",
  config: { allow_registration: false, password_reset: false }, resetToken: "", invite: "", user: null, apify: null
};
const SESSION_EXPIRED = "Sua sessão expirou. Entre novamente.";
const PENDING = "Seu acesso está aguardando aprovação do administrador.";
// Multiusuário: o admin vê robô e Usuários; os outros leem as contas pelo Apify. O CSS
// esconde [data-admin-only] / [data-user-only] pelo <body data-role>; o papel da última visita já
// pinta certo antes do /api/me responder.
const ROLE_KEY = "user_role";
const USER_KEY = "user_id";
const ADMIN_TABS = ["users"];
try { const role = localStorage.getItem(ROLE_KEY); if (role) document.body.dataset.role = role; } catch { /* sem storage */ }
// O navegador só oferece login salvo na tela de entrada: o resto dos campos não preenche sozinho.
document.querySelectorAll("input:not([autocomplete]), textarea:not([autocomplete])").forEach((field) => {
  if (!field.closest("#auth-form")) field.setAttribute("autocomplete", "off");
});
let lineChart = null;
let barChart = null;
let mixChart = null;
let drawerChart = null;
let toastTimer = 0;
let croppedFile = null;
let cropper = null;
// Tema claro/escuro: <html data-theme>, aplicado antes da pintura por um script no <head>. A escolha
// fica em localStorage.theme; sem escolha, segue o sistema. As cores moram nas variáveis do styles.css.
const THEME_KEY = "theme";
const systemLight = matchMedia("(prefers-color-scheme: light)");
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function currentTheme() { return document.documentElement.dataset.theme === "light" ? "light" : "dark"; }
function applyChartTheme() {
  if (!window.Chart) return;
  Chart.defaults.color = cssVar("--muted");
  Chart.defaults.borderColor = cssVar("--line");
  if (mixChart) mixChart.data.datasets[0].borderColor = cssVar("--slice-gap");
  [lineChart, barChart, mixChart, drawerChart, ...archivedCharts].forEach((chart) => chart && chart.update("none"));
}
function setTheme(theme, remember) {
  document.documentElement.dataset.theme = theme;
  if (remember) { try { localStorage.setItem(THEME_KEY, theme); } catch { /* sem armazenamento: vale só nesta aba */ } }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = theme === "light" ? "#eef0f8" : "#07050c";
  applyChartTheme();
}
function savedTheme() { try { return localStorage.getItem(THEME_KEY); } catch { return null; } }
systemLight.addEventListener("change", (event) => { if (!savedTheme()) setTheme(event.matches ? "light" : "dark", false); });
if (window.Chart) {
  Chart.defaults.color = cssVar("--muted");
  Chart.defaults.borderColor = cssVar("--line");
  Chart.defaults.font.family = "Outfit, sans-serif";
  Chart.defaults.animation.duration = 450;
  Chart.defaults.animation.easing = "easeOutCubic";
  // Legendas com bolinhas na cor sólida da série (legendColor no dataset; senão a cor da linha/barra).
  const legendLabels = Chart.defaults.plugins.legend.labels;
  const baseLabels = legendLabels.generateLabels;
  Object.assign(legendLabels, { usePointStyle: true, pointStyle: "circle", boxWidth: 8, boxHeight: 8, padding: 14 });
  // Tooltip (a caixinha ao passar o mouse) também com bolinha, na mesma cor sólida da legenda.
  Object.assign(Chart.defaults.plugins.tooltip, { usePointStyle: true, boxWidth: 8, boxHeight: 8, boxPadding: 4 });
  Chart.defaults.plugins.tooltip.callbacks.labelPointStyle = () => ({ pointStyle: "circle", rotation: 0 });
  Chart.defaults.plugins.tooltip.callbacks.labelColor = (ctx) => {
    const dataset = ctx.dataset || {};
    // Gráfico com uma cor por item (o anel de Interações): a cor da fatia, não a borda (que é a fresta).
    const perItem = Array.isArray(dataset.backgroundColor) ? dataset.backgroundColor[ctx.dataIndex] : null;
    const color = perItem || dataset.legendColor
      || (typeof dataset.borderColor === "string" ? dataset.borderColor : typeof dataset.backgroundColor === "string" ? dataset.backgroundColor : "#ff2ec8");
    return { borderColor: color, backgroundColor: color, borderWidth: 0 };
  };
  legendLabels.generateLabels = (chart) => baseLabels(chart).map((item) => {
    const dataset = chart.data.datasets[item.datasetIndex] || {};
    const color = dataset.legendColor || (typeof dataset.borderColor === "string" ? dataset.borderColor : item.fillStyle);
    return Object.assign(item, { fillStyle: color, strokeStyle: color, lineWidth: 0, lineDash: [], pointStyle: "circle" });
  });
  // As fontes chegam depois do primeiro desenho: mede os rótulos de novo (senão eles ficam inclinados).
  if (document.fonts) document.fonts.ready.then(() => [lineChart, barChart, mixChart].forEach((chart) => chart && chart.update("none")));
}
function num(value) { return value === null || value === undefined ? "—" : fmt.format(value); }
// Avisos com tipo: ícone e cor de sucesso, erro, escudo (🛡️) ou informação, e uma barrinha que mostra
// quanto falta para sumir. O tipo vem do próprio texto quando quem chama não diz.
const TOAST_ICONS = { ok: "✓", error: "!", shield: "🛡️", info: "i" };
const TOAST_MS = 4200;
function toastKind(message) {
  if (/^🛡/.test(message)) return "shield";
  if (/^(✅|Salv|Pronto|Teste (enviado|mostrado|pedido)|Criad|Removid|Excluíd|Atualizad|Arquivad|Desarquivad|Senha (alterada|atualizada)|XHenry instalado|Notificações ligadas)/i.test(message)) return "ok";
  if (/^(❌|⛔|Falha|Erro|Não |Sem |Sua sessão|Notificações bloqueadas)|não (foi|deu|carregou)|inválid|recus/i.test(message)) return "error";
  return "info";
}
function toast(message, kind) {
  const box = document.getElementById("toast");
  const text = String(message || "");
  const type = kind || toastKind(text);
  box.dataset.kind = type;
  box.replaceChildren(
    el("span", "toast-icon", TOAST_ICONS[type]),
    el("span", "toast-text", text.replace(/^(✅|❌|⛔|🛡️?)\s*/u, "")),
    el("span", "toast-bar")
  );
  box.style.setProperty("--toast-ms", TOAST_MS + "ms");
  box.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.add("hidden"), TOAST_MS);
}
function detailOf(body, fallback) {
  if (!body) return fallback;
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg || String(item)).join(" ");
  return fallback;
}
function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
// Lê o exp do JWT só para avisar antes; quem valida de verdade é o servidor.
function tokenExpired() {
  const t = token();
  if (!t) return false;
  try {
    const payload = JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.exp * 1000 <= Date.now();
  } catch (error) {
    return true;
  }
}
function sessionExpired() {
  localStorage.removeItem(TOKEN_KEY);
  paintUser(null);
  setAuthMode("login");
  authMessage("auth-info", SESSION_EXPIRED);
  showAuth(true);
}
function logout() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem("dash_snapshot");
  localStorage.removeItem(ROLE_KEY);
  location.reload();
}
function isAdmin() {
  return state.user ? Boolean(state.user.is_admin) : document.body.dataset.role === "admin";
}
// Outra pessoa entrou neste navegador: nada do anterior (dashboard salvo, alertas vistos) passa adiante.
function forgetUserData() {
  ["dash_snapshot", "viral_seen_id", "viral_notified_id"].forEach((key) => localStorage.removeItem(key));
  dashCache.clear();
}
function applyRole(user) {
  state.user = user;
  const role = user.is_admin ? "admin" : "user";
  document.body.dataset.role = role;
  try {
    const last = localStorage.getItem(USER_KEY);
    if (last && last !== String(user.id)) forgetUserData();
    localStorage.setItem(USER_KEY, String(user.id));
    localStorage.setItem(ROLE_KEY, role);
  } catch { /* sem storage */ }
  if (!user.is_admin && ADMIN_TABS.includes(state.tab)) setTab("dashboard");
}
// Tela de espera: cadastrado pelo convite, ainda sem a aprovação do admin. Confere sozinha a cada 30 s.
let pendingTimer = 0;
function showPending(user) {
  const email = document.getElementById("pending-email");
  if (user) email.textContent = user.email || "";
  else api("/api/me").then((me) => { email.textContent = me.email || ""; }).catch(() => {});
  showAuth(false);
  document.getElementById("pending-screen").classList.remove("off");
  clearInterval(pendingTimer);
  // A cada 3 s: o admin aprova e o painel abre sozinho em seguida, sem clique.
  pendingTimer = setInterval(() => checkApproval(false), 3000);
}
async function checkApproval(manual) {
  try {
    const me = await api("/api/me");
    if (me.is_approved) { clearInterval(pendingTimer); location.reload(); }
    else if (manual) toast("Ainda aguardando a aprovação.", "info");
  } catch (error) {
    if (manual && error.message !== SESSION_EXPIRED) toast(error.message || "Não conferiu agora.");
  }
}
document.getElementById("pending-check").addEventListener("click", () => checkApproval(true));
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
      // A foto vem de /api/avatar/{id}?v=… (o v muda quando a foto muda): o cache do navegador vale.
      img.src = url;
      img.decoding = "async";
      img.alt = "";
      img.className = "h-full w-full rounded-full object-cover";
      el.append(img);
    } else {
      el.textContent = name.slice(0, 1).toUpperCase();
    }
  });
}
const AUTH_MODES = {
  login: { title: "Entrar", submit: "Entrar" },
  register: { title: "Criar conta", submit: "Criar conta" },
  forgot: { title: "Recuperar senha", submit: "Enviar link pelo Telegram" },
  reset: { title: "Nova senha", submit: "Salvar nova senha" }
};
// Mostra um aviso no modal de login (auth-info ou auth-error); texto vazio esconde.
function authMessage(id, text) {
  const el = document.getElementById(id);
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}
function setAuthMode(mode) {
  const canRegister = state.config.allow_registration || Boolean(state.invite);
  if (mode === "register" && !canRegister) mode = "login";
  state.authMode = mode;
  const register = mode === "register";
  const reset = mode === "reset";
  const field = (id, on) => {
    document.getElementById(id + "-label").classList.toggle("hidden", !on);
    const input = document.getElementById(id);
    input.classList.toggle("hidden", !on);
    input.required = on;
  };
  field("auth-name", register);
  field("auth-email", !reset);
  field("auth-password", mode !== "forgot");
  field("auth-password2", reset);
  document.getElementById("auth-password-label").textContent = reset ? "Nova senha" : "Senha";
  document.getElementById("auth-password").autocomplete = register || reset ? "new-password" : "current-password";
  document.getElementById("auth-title").textContent = AUTH_MODES[mode].title;
  document.querySelector("#auth-form button[type=submit]").textContent = AUTH_MODES[mode].submit;
  document.getElementById("forgot-link").classList.toggle("hidden", mode !== "login" || !state.config.password_reset);
  const sw = document.getElementById("auth-switch");
  sw.textContent = mode === "login" ? "Criar conta" : "Voltar para o login";
  sw.classList.toggle("hidden", mode === "login" && !canRegister);
  authMessage("auth-info", "");
  authMessage("auth-error", "");
}
async function loadConfig(invite) {
  try {
    const query = invite ? "?invite=" + encodeURIComponent(invite) : "";
    state.config = Object.assign(state.config, await api("/api/config" + query));
  } catch (error) {
    // Sem config: fica o padrão fechado (sem cadastro, sem recuperação).
  }
}
// Botão que disparou uma escrita (salvar, excluir, aprovar…): gira até o servidor responder e não aceita
// clique duplo. Vale para o último botão clicado ou enviado nos últimos 3 s; confirmações devolvem o botão
// que abriu o modal (confirmOrigin).
let pressed = null;
let pressedAt = 0;
let confirmOrigin = null;
let changed = null;
let changedAt = 0;
function markPressed(button) { pressed = button; pressedAt = Date.now(); }
document.addEventListener("change", (event) => {
  const field = event.target;
  if (field.matches && field.matches("input[type=checkbox], input[type=time], select")) { changed = field; changedAt = Date.now(); }
}, true);
// Campo que salvou ao mudar: travado (e o rótulo dele pulsando) até a resposta.
function busyField() {
  const field = changed;
  if (!field || Date.now() - changedAt > 1500 || !field.isConnected || field.disabled) return null;
  changed = null;
  // O anel vai no cartãozinho do interruptor (label) ou no próprio campo (lista, horário).
  const shown = field.closest("label.rounded-2xl, .notify-pill") || field;
  field.disabled = true;
  shown.classList.add("is-saving");
  return () => { field.disabled = false; shown.classList.remove("is-saving"); };
}
document.addEventListener("click", (event) => { const b = event.target.closest && event.target.closest("button"); if (b) markPressed(b); }, true);
document.addEventListener("submit", (event) => {
  const b = event.submitter || event.target.querySelector("button[type=submit], button:not([type])");
  if (b) markPressed(b);
}, true);
function busyButton(method) {
  if (!method || method.toUpperCase() === "GET") return null;
  const b = pressed;
  if (!b || Date.now() - pressedAt > 3000 || !b.isConnected || b.id === "refresh-all" || b.classList.contains("is-busy")) return busyField();
  pressed = null;
  const wasDisabled = b.disabled;
  b.classList.add("is-busy");
  b.setAttribute("aria-busy", "true");
  b.disabled = true;
  return () => { b.classList.remove("is-busy"); b.removeAttribute("aria-busy"); b.disabled = wasDisabled; };
}
// Envia o Bearer e lança o detail do servidor em caso de erro; devolve o Response cru.
async function apiResponse(path, options) {
  const release = busyButton(options && options.method);
  try { return await sendApi(path, options); } finally { if (release) release(); }
}
async function sendApi(path, options) {
  const opts = Object.assign({}, options);
  const headers = new Headers(opts.headers || {});
  if (token() && !headers.has("Authorization")) headers.set("Authorization", "Bearer " + token());
  opts.headers = headers;
  const response = await fetch(path, opts);
  // 401 com token enviado = sessão vencida ou revogada: volta para o login.
  if (response.status === 401 && headers.has("Authorization")) {
    sessionExpired();
    throw new Error(SESSION_EXPIRED);
  }
  // 403 "pending": logado, mas o admin ainda não aprovou (ou revogou).
  if (response.status === 403 && response.headers.get("X-Access") === "pending") {
    showPending(state.user);
    throw new Error(PENDING);
  }
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
  if (ADMIN_TABS.includes(tab) && !isAdmin()) tab = "dashboard";
  state.tab = tab;
  document.querySelectorAll(".nav-btn, .tab-btn[data-tab], .more-item").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  const moreBtn = document.getElementById("more-btn");
  if (moreBtn) {
    moreBtn.classList.toggle("active", MORE_TABS.includes(tab));
    setMoreOpen(false);
  }
  document.querySelectorAll(".pane").forEach((pane) => pane.classList.toggle("on", pane.id === "pane-" + tab));
  document.getElementById("heading").textContent = titles[tab];
  const controls = document.getElementById("dashboard-controls");
  if (controls) controls.classList.toggle("hidden", tab !== "dashboard");
  if (tab === "dashboard" && dashboardDirty) { dashboardDirty = false; load({ silent: true }); }
  if (tab === "ops") closeOpsFolder();
  if (tab === "settings") loadSettings();
  if (tab === "archived") loadArchived();
  if (tab === "radar") loadRadar();
  if (tab === "users") loadUsers();
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function pctText(value, digits) {
  return value.toLocaleString("pt-BR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
// Variação contra o período anterior, do mesmo tamanho. points=true compara taxas em pontos percentuais.
function delta(current, previous, points) {
  if (current === null || current === undefined || previous === null || previous === undefined) return null;
  if (points) {
    const diff = current - previous;
    const dir = Math.abs(diff) < 0.05 ? "flat" : diff > 0 ? "up" : "down";
    return { dir, text: (diff > 0 ? "+" : "") + pctText(diff, 1) + " p.p." };
  }
  if (previous === 0) return current > 0 ? { dir: "up", text: "novo" } : null;
  const pct = (current - previous) / previous * 100;
  const dir = Math.abs(pct) < 0.5 ? "flat" : pct > 0 ? "up" : "down";
  return { dir, text: (pct > 0 ? "+" : "") + pctText(pct, Math.abs(pct) < 10 ? 1 : 0) + "%" };
}
function deltaChip(current, previous, points) {
  const d = delta(current, previous, points);
  if (!d) return el("span", "delta-none");
  const chip = el("span", "delta-chip delta-" + d.dir, (d.dir === "up" ? "▲ " : d.dir === "down" ? "▼ " : "") + d.text);
  const label = (state.data && state.data.compare_label) || "";
  chip.title = label + ": " + (points ? pctText(previous, 2) + "%" : num(previous));
  chip.setAttribute("aria-label", d.text + " " + label);
  return chip;
}
function prevOf(key) {
  const prev = state.data && state.data.previous;
  return prev ? prev[key] : null;
}
function signed(value) {
  if (value === null || value === undefined) return "—";
  return (value > 0 ? "+" : value < 0 ? "−" : "") + fmt.format(Math.abs(value));
}
// Views por dia em SVG, no rodapé do cartão principal.
function sparkline(series) {
  const ns = "http://www.w3.org/2000/svg";
  const values = series.map((row) => row.views || 0);
  const max = Math.max(...values, 1);
  const step = 100 / (values.length - 1);
  const line = values.map((v, i) => (i ? "L" : "M") + (i * step).toFixed(2) + " " + (30 - v / max * 26).toFixed(2)).join(" ");
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 100 32");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("spark");
  const defs = document.createElementNS(ns, "defs");
  const grad = document.createElementNS(ns, "linearGradient");
  grad.id = "spark-fill";
  grad.setAttribute("x1", "0"); grad.setAttribute("x2", "0"); grad.setAttribute("y1", "0"); grad.setAttribute("y2", "1");
  [["0", "rgba(255,99,195,.38)"], ["1", "rgba(103,220,255,0)"]].forEach(([offset, color]) => {
    const stop = document.createElementNS(ns, "stop");
    stop.setAttribute("offset", offset);
    stop.setAttribute("stop-color", color);
    grad.append(stop);
  });
  defs.append(grad);
  const area = document.createElementNS(ns, "path");
  area.setAttribute("d", line + " L100 32 L0 32 Z");
  area.setAttribute("fill", "url(#spark-fill)");
  const stroke = document.createElementNS(ns, "path");
  stroke.setAttribute("d", line);
  stroke.setAttribute("class", "spark-line");
  svg.append(defs, area, stroke);
  return svg;
}
function miniLine(values) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 100 24");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("mini-line");
  const max = Math.max(...values, 1);
  const step = 100 / Math.max(1, values.length - 1);
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", values.map((v, i) => (i ? "L" : "M") + (i * step).toFixed(2) + " " + (22 - v / max * 20).toFixed(2)).join(" "));
  svg.append(path);
  return svg;
}
// Números que contam: ao trocar de período, o valor desliza do que estava na tela até o novo (só
// depois do primeiro desenho; com "reduzir movimento", troca direto).
const shownNumbers = new Map();
function countTo(node, key, value, format) {
  const to = typeof value === "number" && Number.isFinite(value) ? value : null;
  const from = shownNumbers.has(key) ? shownNumbers.get(key) : null;
  shownNumbers.set(key, to);
  node.textContent = format(value);
  if (from !== null && to !== null && from !== to) node.dataset.changed = "1";
  if (from === null || to === null || from === to || matchMedia("(prefers-reduced-motion: reduce)").matches) return node;
  const start = performance.now();
  const step = (now) => {
    if (!node.isConnected) return;
    const t = Math.min(1, (now - start) / 650);
    const eased = 1 - Math.pow(1 - t, 3);
    node.textContent = format(Math.round(from + (to - from) * eased));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return node;
}
function heroCard(kpis, series) {
  const card = el("article", "kpi-card kpi-left kpi-hero col-span-2 rounded-3xl border bg-card p-5");
  card.dataset.key = "views";
  const top = el("div", "flex items-center justify-between gap-3");
  top.append(el("p", "text-sm text-mute", "Views"), deltaChip(kpis.views, prevOf("views")));
  const label = state.data.compare_label;
  card.append(
    top,
    countTo(el("p", "mt-1 font-mono text-5xl tracking-tight text-white"), "views", kpis.views, num),
    el("p", "mt-1 text-xs text-mute", label ? label + ": " + num(prevOf("views")) + " views" : "Desde o primeiro post lido")
  );
  const goal = state.data.goal;
  if (goal && goal.target) {
    const share = Math.min(1, goal.today / goal.target);
    const row = el("div", "goal-row mt-3");
    const track = el("div", "goal-track");
    const fill = el("div", "goal-fill" + (share >= 1 ? " goal-done" : ""));
    fill.style.width = (share * 100).toFixed(1) + "%";
    track.append(fill);
    row.append(el("span", "text-[11px] text-mute", "Meta do dia"), track, el("span", "font-mono text-[11px] text-white", num(goal.today) + " / " + num(goal.target)));
    card.append(row);
  }
  if (series.length > 1) {
    const days = series.length;
    card.append(sparkline(series), el("p", "spark-caption text-[10px] text-mute", days <= 14 ? "últimos " + days + " dias" : "views por dia"));
    sparkHover(card, series);
  }
  return card;
}
function perPostCard(kpis) {
  const card = el("article", "kpi-card kpi-left col-span-2 rounded-3xl border bg-card p-5");
  card.dataset.key = "views_per_post";
  const top = el("div", "flex items-center justify-between gap-3");
  top.append(el("p", "text-sm text-mute", "Views por post"), deltaChip(kpis.views_per_post, prevOf("views_per_post")));
  const posts = kpis.posts || 0;
  card.append(
    top,
    countTo(el("p", "mt-1 font-mono text-4xl tracking-tight text-white"), "views_per_post", kpis.views_per_post, num),
    el("p", "mt-1 text-xs text-mute", "Média de " + posts + (posts === 1 ? " post" : " posts"))
  );
  if (state.data.previous) {
    const prev = prevOf("views_per_post");
    const max = Math.max(kpis.views_per_post || 0, prev || 0, 1);
    const bars = el("div", "compare-bars mt-4");
    [["Agora", kpis.views_per_post, "now"], ["Antes", prev, "before"]].forEach(([name, val, kind]) => {
      const track = el("div", "compare-track");
      const fill = el("div", "compare-fill compare-" + kind);
      fill.style.width = ((val || 0) / max * 100).toFixed(1) + "%";
      track.append(fill);
      const row = el("div", "compare-row");
      row.append(el("span", "text-[11px] text-mute", name), track, el("span", "font-mono text-xs text-white", num(val)));
      bars.append(row);
    });
    card.append(bars);
  }
  return card;
}
function followersCard(kpis) {
  const card = el("article", "kpi-card kpi-left col-span-2 rounded-3xl border bg-card p-5");
  card.dataset.key = "followers_gained";
  const gained = kpis.followers_gained;
  const tone = gained > 0 ? "gain-up" : gained < 0 ? "gain-down" : "text-white";
  card.append(
    el("p", "text-sm text-mute", "Seguidores no período"),
    countTo(el("p", "mt-1 font-mono text-4xl tracking-tight " + tone), "followers_gained", gained, signed),
    el("p", "mt-1 text-xs text-mute", "Total agora: " + num(kpis.followers))
  );
  return card;
}
function tile(label, value, current, previous, hint) {
  const card = el("article", "kpi-card flex min-h-24 flex-col items-center justify-center rounded-3xl border bg-card px-4 py-4 text-center");
  card.dataset.key = "tile:" + label;
  const big = el("p", "mt-1 font-mono text-2xl tracking-tight text-white", value);
  if (typeof current === "number") countTo(big, "tile:" + label, current, num);
  card.append(el("p", "text-xs text-mute", label), big);
  card.append(hint ? el("p", "mt-1 text-[11px] text-mute", hint) : deltaChip(current, previous));
  return card;
}
function renderKpis(kpis) {
  document.getElementById("kpis").replaceChildren(
    heroCard(kpis, state.data.spark || []),
    perPostCard(kpis),
    followersCard(kpis),
    tile("Curtidas", num(kpis.likes), kpis.likes, prevOf("likes")),
    tile("Retweets", num(kpis.retweets), kpis.retweets, prevOf("retweets")),
    tile("Replies", num(kpis.replies), kpis.replies, prevOf("replies")),
    tile("Citações", num(kpis.quotes), kpis.quotes, prevOf("quotes")),
    tile("Salvamentos", num(kpis.bookmarks), kpis.bookmarks, prevOf("bookmarks"), kpis.bookmarks === null ? "Ainda sem leitura" : ""),
    tile("Contas", num(kpis.accounts), null, null, kpis.healthy + (kpis.healthy === 1 ? " saudável" : " saudáveis"))
  );
  renderMix(kpis);
}
const MIX_PARTS = [
  ["likes", "Curtidas", "#ff63c3"],
  ["retweets", "Retweets", "#67dcff"],
  ["replies", "Replies", "#9b8cff"],
  ["quotes", "Citações", "#ffc46b"],
  ["bookmarks", "Salvamentos", "#7cf0b5"]
];
// Rosca das interações: a fatia de cada tipo e, no meio, a taxa de interação (interações ÷ views).
function renderMix(kpis) {
  const parts = MIX_PARTS.map(([key, label, color]) => ({ label, color, value: kpis[key] || 0 })).filter((part) => part.value > 0);
  const total = parts.reduce((sum, part) => sum + part.value, 0);
  const rate = kpis.interaction_rate;
  document.getElementById("mix-rate").textContent = rate === null || rate === undefined ? "—" : pctText(rate, 2) + "%";
  const chip = deltaChip(rate, prevOf("interaction_rate"), true);
  chip.id = "mix-rate-delta";
  document.getElementById("mix-rate-delta").replaceWith(chip);
  document.getElementById("mix-note").textContent = total
    ? fmt.format(total) + " interações em " + fmt.format(kpis.views || 0) + " views"
    : "Sem interações nos posts do período";
  document.getElementById("mix-legend").replaceChildren(...parts.map((part) => {
    const li = el("li", "mix-item");
    const dot = el("span", "mix-dot");
    dot.style.background = part.color;
    li.append(
      dot,
      el("span", "flex-1 truncate text-sm text-white", part.label),
      el("span", "font-mono text-sm text-mute", pctText(part.value / total * 100, 0) + "%")
    );
    li.title = part.label + ": " + num(part.value);
    return li;
  }));
  if (!window.Chart) return;
  const mixData = {
    labels: parts.length ? parts.map((part) => part.label) : ["Sem dados"],
    datasets: [{
      data: parts.length ? parts.map((part) => part.value) : [1],
      backgroundColor: parts.length ? parts.map((part) => part.color) : ["rgba(162,180,255,.14)"],
      borderColor: cssVar("--slice-gap"),  // a fresta entre as fatias tem a cor do cartão
      borderWidth: 3,
      hoverOffset: 6
    }]
  };
  const options = {
    responsive: true,
    maintainAspectRatio: false,
    cutout: "72%",
    plugins: { legend: { display: false }, tooltip: { enabled: parts.length > 0 } }
  };
  if (mixChart) updateChart(mixChart, mixData, options);
  else mixChart = markChart(new Chart(document.getElementById("mix-chart"), { type: "doughnut", data: mixData, options }));
}
function renderTopPost(post) {
  const host = document.getElementById("top-post-body");
  if (!post) {
    host.replaceChildren(el("p", "text-sm text-mute", "Nenhum post lido no período. Clique em Atualizar dados para ler as contas agora."));
    return;
  }
  const initial = post.username.slice(0, 1).toUpperCase();
  const avatar = el("div", "post-avatar");
  if (post.profile_image_url) {
    const img = el("img");
    img.src = post.profile_image_url;
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = "";
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => { img.remove(); avatar.textContent = initial; });
    avatar.append(img);
  } else avatar.textContent = initial;
  const who = el("div", "min-w-0 flex-1 text-left");
  who.append(
    el("p", "truncate text-sm font-medium text-white", post.display_name || "@" + post.username),
    el("p", "truncate text-xs text-mute", "@" + post.username + ", " + stampBR(post.posted_at))
  );
  const head = el("div", "flex items-center gap-3");
  head.append(avatar, who);
  const stats = el("dl", "mt-4 grid grid-cols-4 gap-2 text-center");
  [["Views", post.views], ["Curtidas", post.likes], ["RTs", post.retweets], ["Replies", post.replies]].forEach(([label, value], index) => {
    const box = el("div", "post-stat" + (index === 0 ? " post-stat-hot" : ""));
    box.append(el("dt", "text-[10px] text-mute", label), el("dd", "mt-0.5 font-mono text-sm text-white", num(value)));
    stats.append(box);
  });
  const link = el("a", "shrink-0 rounded-full bg-neon px-4 py-1.5 text-sm font-medium text-ink hover:bg-white", "Abrir no X");
  link.href = post.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  const foot = el("div", "mt-4 flex items-center justify-between gap-3");
  foot.append(el("p", "text-left text-xs text-mute", post.share === null ? "" : pctText(post.share, 1) + "% das views do período"), link);
  host.replaceChildren(head, el("p", "post-text mt-3 text-left text-sm text-white", post.text || "Post sem texto (só mídia)."), stats, foot);
}
function renderRanking(posts) {
  const host = document.getElementById("post-ranking");
  if (!posts || !posts.length) {
    host.replaceChildren(el("li", "text-sm text-mute", "Nenhum post lido no período."));
    return;
  }
  host.replaceChildren(...posts.map((post, index) => {
    const li = el("li", "rank-row");
    const link = el("a", "rank-text truncate text-sm text-white", post.text || "Post sem texto (só mídia)");
    link.href = post.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = "Abrir no X";
    li.append(
      el("span", "rank-pos font-mono text-xs text-mute", String(index + 1)),
      el("span", "rank-who truncate text-xs text-mute", "@" + post.username),
      link,
      el("span", "rank-num font-mono text-sm text-white", num(post.views)),
      el("span", "rank-likes font-mono text-xs text-mute", num(post.likes) + " ♥")
    );
    return li;
  }));
}
const ALERT_KINDS = {
  viral: ["Viral", "#7cf0b5"], queda: ["Queda", "#ffb4c4"], ban: ["Restrição", "#ffb4c4"],
  erro: ["Erro", "#ffc46b"], silencio: ["Robô parado", "#ffc46b"],
  apify: ["Apify", "#ffc46b"], marco_post: ["Marco do post", "#7cf0b5"], marco_dia: ["Marco do dia", "#4fe3ff"],
  meta: ["Meta batida", "#ffd36b"], radar: ["Radar", "#67dcff"], acesso: ["Pedido de acesso", "#d6ccff"], resumo: ["Resumo do dia", "#ff63c3"]
};
let alertGroup = "";
const VIEW_LEVEL_TAGS = [
  [/MILHÃO/i, "👑 1M de views", "#ff63c3"],
  [/100K/i, "💯 100K de views", "#ffd36b"],
  [/BOMBANDO/i, "🔥 Post bombando", "#ff9f5a"]
];
function renderAlerts(alerts) {
  const host = document.getElementById("alert-list");
  if (!alerts.length) {
    host.replaceChildren(el("li", "text-sm text-mute", alertGroup ? "Nenhum alerta deste tipo ainda." : "Nenhum alerta ainda."));
    return;
  }
  host.replaceChildren(...alerts.map((alert) => {
    let [label, color] = ALERT_KINDS[alert.kind] || [alert.kind, "#9b8cff"];
    const lines = alert.text.split("\n").map((line) => line.trim()).filter(Boolean);
    // Alerta de views: o nível (viralizando, bombando, 100K, 1M) vem na primeira linha do robô.
    const level = alert.kind === "viral" ? VIEW_LEVEL_TAGS.find(([pattern]) => pattern.test(lines[0] || "")) : null;
    if (level) [, label, color] = level;
    const li = el("li", "alert-row");
    const dot = el("span", "alert-dot");
    dot.style.background = color;
    const body = el("div", "min-w-0 flex-1");
    const who = lines.find((line) => line.includes("Conta:"));
    body.append(el("p", "truncate text-sm text-white", label + (who ? ": " + who.replace(/^.*Conta:\s*/, "") : "")));
    const sub = alert.kind === "viral" ? lines.find((line) => line.includes("Views:")) || lines[0] : lines[0];
    if (sub) body.append(el("p", "truncate text-xs text-mute", sub));
    li.title = alert.text;
    li.append(dot, body, el("span", "shrink-0 text-[11px] text-mute", agoText(alert.sent_at)));
    return li;
  }));
}
async function loadAlerts() {
  try {
    renderAlerts(await api("/api/alerts?limit=8" + (alertGroup ? "&group=" + alertGroup : "")));
  } catch (error) {
    if (error.message === SESSION_EXPIRED) return;
    document.getElementById("alert-list").replaceChildren(el("li", "text-sm text-mute", "Não deu para ler os alertas: " + (error.message || "erro") + "."));
  }
}
document.getElementById("alert-filters").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-alert-group]");
  if (!chip) return;
  alertGroup = chip.dataset.alertGroup;
  document.querySelectorAll("#alert-filters .chip").forEach((item) => item.classList.toggle("on", item === chip));
  loadAlerts();
});
function agoText(iso) {
  if (!iso) return "nunca";
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  return mins < 1 ? "agora" : mins < 60 ? "há " + mins + " min" : mins < 1440 ? "há " + Math.floor(mins / 60) + " h" : "há " + Math.floor(mins / 1440) + " d";
}
// Anel das contas em dia sobre o total, em SVG.
function ring(done, total) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 44 44");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("gauge");
  const length = 2 * Math.PI * 19;
  const share = total ? done / total : 0;
  [["gauge-track", length], ["gauge-fill" + (share < 0.7 ? " gauge-warn" : ""), share * length]].forEach(([cls, dash]) => {
    const circle = document.createElementNS(ns, "circle");
    circle.setAttribute("cx", "22"); circle.setAttribute("cy", "22"); circle.setAttribute("r", "19");
    circle.setAttribute("class", cls);
    circle.setAttribute("stroke-dasharray", dash.toFixed(2) + " " + length.toFixed(2));
    svg.append(circle);
  });
  return svg;
}
// Selos vermelhos nos ícones das abas: Dashboard = alertas de views não vistos (como o sininho);
// Usuários = pedidos de acesso esperando. A soma vai também para o ícone do app instalado
// (barra de tarefas do Windows, tela de início do celular).
const tabBadges = { alerts: 0, users: 0 };
function paintTabBadge(name, count) {
  tabBadges[name] = count || 0;
  document.querySelectorAll('[data-badge="' + name + '"]').forEach((badge) => {
    badge.textContent = count > 9 ? "9+" : String(count || "");
    badge.classList.toggle("hidden", !count);
  });
  const total = tabBadges.alerts + tabBadges.users;
  try {
    if (total && navigator.setAppBadge) navigator.setAppBadge(total).catch(() => {});
    else if (!total && navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
  } catch { /* navegador sem selo no ícone */ }
}
function renderHealth(health) {
  const host = document.getElementById("health-body");
  const note = document.getElementById("health-note");
  const late = health.late_after_min >= 120 ? Math.round(health.late_after_min / 60) + " h" : health.late_after_min + " min";
  note.textContent = (health.apify && health.apify.active ? "Lendo pelo Apify a cada " + intervalLabel(health.apify.interval_min) + ". " : "Sem token do Apify: nada é lido. ")
    + (health.enabled
      ? "Atrasada é a conta sem leitura há mais de " + late + "."
      : "Leitura automática desligada: só lê quando você clica em Atualizar dados.");
  const gauge = el("div", "gauge-wrap");
  gauge.append(ring(health.fresh, health.accounts), el("p", "gauge-label font-mono text-lg text-white", health.fresh + "/" + health.accounts));
  const behind = health.stale.length;
  const last = health.last_run;
  const summary = el("div", "min-w-0 flex-1 text-left");
  summary.append(
    el("p", "text-sm text-white", behind ? behind + (behind === 1 ? " conta atrasada" : " contas atrasadas") : "Todas as contas em dia"),
    el("p", "mt-1 text-xs text-mute", last ? "Última leitura " + agoText(last.finished_at) + ", às " + stampBR(last.finished_at) + (last.status === "failed" ? " (falhou)" : "") : "O robô ainda não rodou")
  );
  const top = el("div", "flex items-center gap-4");
  top.append(gauge, summary);
  const parts = [top];
  if (behind) {
    const list = el("ul", "mt-4 flex flex-wrap gap-2");
    health.stale.slice(0, 6).forEach((item) => list.append(el("li", "stale-chip", "@" + item.username + " " + (item.collected_at ? agoText(item.collected_at) : "sem leitura"))));
    if (behind > 6) list.append(el("li", "stale-chip", "+" + (behind - 6)));
    parts.push(list);
  }
  host.replaceChildren(...parts);
}
async function loadHealth() {
  try {
    renderHealth(await api("/api/robo/health"));
  } catch (error) {
    if (error.message === SESSION_EXPIRED) return;
    document.getElementById("health-note").textContent = "Não deu para ler a saúde da leitura: " + (error.message || "erro") + ".";
    document.getElementById("health-body").replaceChildren();  // tira o esqueleto de carregamento
  }
}
// Saúde da leitura e pedidos de acesso são do admin; quem lê pelo Apify vê o estado da leitura dele.
function roleSideLoads() {
  if (isAdmin()) { loadHealth(); loadUsersBadge(); } else loadApify();
}
function apifyIssue(apify) {
  if (!apify.token_set) return ["Token do Apify em falta. Cole o seu em Configurações para o robô ler as suas contas.", false];
  if (apify.status === "erro") return ["A última leitura pelo Apify falhou: " + (apify.detail || "erro") + "", true];
  return null;
}
function apifyStatusText(apify) {
  if (!apify.token_set) return "Sem token: o robô não lê as suas contas.";
  if (apify.requested_at) return "Leitura pedida às " + stampBR(apify.requested_at) + ", aguardando o robô.";
  if (!apify.status) return "Token salvo. A primeira leitura sai em até 1 minuto.";
  const when = apify.checked_at ? stampBR(apify.checked_at) : "—";
  if (apify.status === "erro") return "Última tentativa (" + when + ") falhou: " + (apify.detail || "erro");
  return "Última leitura: " + when + (apify.detail ? " · " + apify.detail : "");
}
function paintApify(apify) {
  state.apify = apify;
  const issue = apifyIssue(apify);
  const banner = document.getElementById("apify-banner");
  banner.classList.toggle("hidden", !issue);
  if (issue) {
    document.getElementById("apify-banner-text").textContent = issue[0];
    banner.classList.toggle("is-bad", issue[1]);
  }
  const status = document.getElementById("apify-status");
  status.textContent = apifyStatusText(apify);
  status.classList.toggle("text-amber-300", Boolean(issue) && !(isAdmin() && !apify.token_set));
}
// ---------- Configurações → Notificações ----------
function stepLabel(value, unit) {
  return value ? "A cada " + num(value) + " " + unit : "Desligado";
}
function deviceName(agent) {
  const ua = String(agent || "");
  const browser = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Firefox\//.test(ua) ? "Firefox"
    : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Navegador";
  const system = /iPhone|iPad/.test(ua) ? "iPhone" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows"
    : /Mac OS/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "";
  return browser + (system ? " no " + system : "");
}
function paintNotifyStatus() {
  const status = document.getElementById("notify-status");
  const enable = document.getElementById("notify-enable");
  if (!notifySupported()) {
    status.textContent = isIos() && !isStandalone()
      ? "No iPhone, instale o app (Compartilhar → Adicionar à Tela de Início) para receber notificações."
      : "Este navegador não recebe notificações. Use Chrome, Edge ou o app instalado.";
    enable.classList.add("hidden");
    return;
  }
  const permission = Notification.permission;
  enable.classList.toggle("hidden", permission === "granted" && Boolean(pushEndpoint()));
  status.textContent = permission === "denied"
    ? "Notificações bloqueadas neste navegador. Libere nas permissões do site e volte aqui."
    : permission === "granted" && pushEndpoint() ? "Este aparelho recebe as notificações."
    : "Este aparelho ainda não recebe notificações.";
}
function notifySelect(id, steps, value, unit) {
  const select = document.getElementById(id);
  select.replaceChildren(...steps.map((step) => {
    const option = document.createElement("option");
    option.value = String(step);
    option.textContent = stepLabel(step, unit);
    return option;
  }));
  select.value = String(value);
}
function paintNotify(data) {
  const prefs = data.prefs;
  // Pílulas compactas: nome curto na tela, o nome inteiro no title (e no leitor de tela, pelo aria-label).
  document.getElementById("notify-types").replaceChildren(...data.types.map((type) => {
    const on = prefs.kinds[type.key] !== false;
    const pill = el("label", "notify-pill" + (on ? " on" : ""));
    pill.title = type.label;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = on;
    input.setAttribute("aria-label", type.label);
    input.addEventListener("change", () => {
      pill.classList.toggle("on", input.checked);
      saveNotify({ kinds: { [type.key]: input.checked } });
    });
    pill.append(input, el("b", "", type.emoji), el("span", "", type.short || type.label));
    return pill;
  }));
  notifySelect("notify-post-step", data.post_steps, prefs.post_step, "views");
  notifySelect("notify-day-step", data.day_steps, prefs.day_step, "views no dia");
  document.getElementById("notify-goal").textContent = "A meta do dia (" + num(data.goal) + " views) avisa uma vez quando o total passa dela; mude a meta em Configurações.";
  document.getElementById("quiet-on").checked = Boolean(prefs.quiet.on);
  document.getElementById("quiet-from").value = prefs.quiet.from;
  document.getElementById("quiet-to").value = prefs.quiet.to;
  ["quiet-from", "quiet-to"].forEach((id) => { document.getElementById(id).disabled = !prefs.quiet.on; });
  // Resumo no título de cada bloco: no celular eles ficam fechados.
  const step = (value) => (value ? num(value) : "desligado");
  document.getElementById("notify-steps-sum").textContent = "Post: " + step(prefs.post_step) + " · Dia: " + step(prefs.day_step);
  document.getElementById("notify-quiet-sum").textContent = prefs.quiet.on ? prefs.quiet.from + "–" + prefs.quiet.to : "Desligado";
  paintNotifyStatus();
}
async function loadNotify() {
  try {
    paintNotify(await api("/api/notifications"));
    loadDevices();
  } catch (error) { if (error.message !== SESSION_EXPIRED) toast(error.message || "Não leu as notificações."); }
}
async function saveNotify(body) {
  try {
    paintNotify(await api("/api/notifications", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
    toast("Notificações salvas.");
  } catch (error) {
    toast(error.message || "Não salvou.");
    loadNotify();
  }
}
async function loadDevices() {
  const host = document.getElementById("notify-devices");
  try {
    const devices = await api("/api/push/devices");
    const mine = pushEndpoint();
    host.replaceChildren(...(devices.length ? devices.map((device) => {
      const row = el("li", "device-row");
      const text = el("div", "min-w-0 flex-1");
      const here = mine && mine.endsWith(device.tail);
      text.append(el("p", "truncate text-sm text-white", deviceName(device.user_agent) + (here ? " · este aparelho" : "")),
        el("p", "text-xs text-mute", device.created_at ? "Desde " + stampBR(device.created_at) : ""));
      const remove = cardButton("Remover", async () => {
        if (!(await showConfirm("Remover este aparelho?", "Ele para de receber as notificações até você ativar de novo nele."))) return;
        try {
          await api("/api/push/devices/" + device.id, { method: "DELETE" });
          if (here) setPushEndpoint("");
          toast("Aparelho removido.");
          loadDevices();
          paintNotifyStatus();
        } catch (error) { toast(error.message || "Não removeu."); }
      });
      row.append(text, remove);
      return row;
    }) : [el("li", "text-sm text-mute", "Nenhum aparelho recebe notificações ainda. Clique em Ativar neste aparelho.")]));
    document.getElementById("notify-devices-sum").textContent = devices.length ? devices.length + (devices.length === 1 ? " aparelho" : " aparelhos") : "Nenhum";
  } catch (error) { host.replaceChildren(); }
}
// Celular: os blocos de Notificações começam fechados (mostram só o resumo); no computador ficam abertos.
const narrowSettings = matchMedia("(max-width: 767px)");
function fitNotifyBlocks() {
  document.querySelectorAll(".notify-block").forEach((block) => { block.open = !narrowSettings.matches; });
}
fitNotifyBlocks();
narrowSettings.addEventListener("change", fitNotifyBlocks);
document.getElementById("notify-post-step").addEventListener("change", (event) => saveNotify({ post_step: Number(event.target.value) }));
document.getElementById("notify-day-step").addEventListener("change", (event) => saveNotify({ day_step: Number(event.target.value) }));
document.getElementById("quiet-on").addEventListener("change", (event) => saveNotify({ quiet: { on: event.target.checked } }));
["quiet-from", "quiet-to"].forEach((id) => document.getElementById(id).addEventListener("change", (event) => {
  if (event.target.value) saveNotify({ quiet: { [id === "quiet-from" ? "from" : "to"]: event.target.value } });
}));
document.getElementById("notify-enable").addEventListener("click", async () => {
  if (notifySupported() && Notification.permission === "granted") await enablePush();  // já liberado: só inscreve
  else await askNotifications();
  paintNotifyStatus();
  loadDevices();
});
document.getElementById("notify-test").addEventListener("click", () => testNotification());

function paintReading(apify) {
  paintApify(apify);
  document.getElementById("apify-auto").checked = Boolean(apify.auto);
  fillIntervals(document.getElementById("apify-every"), apify.intervals, apify.interval_min, !apify.auto);
  if (apify.pause) paintPause("reading", apify.pause);
  const next = apify.auto ? "Leitura automática a cada " + intervalLabel(apify.interval_min) + "." + pauseText(apify.pause || {}) : "Leitura automática desligada: só pelo botão Atualizar dados.";
  document.getElementById("reading-status").textContent = next + " " + apifyStatusText(apify);
}
async function saveReading(body) {
  try {
    const apify = await api("/api/apify", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    paintReading(apify);
    if (body.pause) toast(apify.pause.on ? "Pausa salva: de " + apify.pause.from + " às " + apify.pause.to + "." : "Pausa da madrugada desligada.");
    else toast(body.auto === false ? "Leitura automática desligada." : body.auto ? "Leitura automática ligada." : "Intervalo salvo: a cada " + intervalLabel(apify.interval_min) + ".");
  } catch (error) { toast(error.message || "Não salvou."); }
}
document.getElementById("apify-auto").addEventListener("change", (event) => saveReading({ auto: event.target.checked }));
document.getElementById("apify-every").addEventListener("change", (event) => saveReading({ interval_min: Number(event.target.value) }));
watchPause("reading", saveReading);
async function loadApify() {
  try { paintApify(await api("/api/apify")); } catch { /* o aviso só aparece quando dá para ler */ }
}
document.getElementById("apify-banner-go").addEventListener("click", () => {
  setTab("settings");
  setTimeout(() => document.getElementById("apify-token").focus(), 50);
});

// ---------- Usuários (só o admin): pedidos, acesso e convites ----------
function userAvatar(user) {
  const box = el("span", "user-avatar");
  const url = String(user.avatar_url || "");
  if (url.startsWith("data:") || url.startsWith("https://") || url.startsWith("/api/avatar/")) {
    const img = document.createElement("img");
    img.src = url;
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = "";
    box.append(img);
  } else box.textContent = (user.nome_exibicao || user.email || "?").slice(0, 1).toUpperCase();
  return box;
}
function userCard(user, actions) {
  const card = el("article", "glow rounded-2xl border border-neon/20 bg-card p-4");
  const head = el("div", "user-head");
  const who = el("div", "min-w-0");
  who.append(el("p", "truncate font-medium text-white", user.nome_exibicao || user.email), el("p", "truncate text-xs text-mute", user.email));
  head.append(userAvatar(user), who);
  card.append(head);
  if (user.is_admin) card.append(infoLine("Papel", "Administrador (você)", false));
  if (user.is_approved) {
    card.append(infoLine("Acesso desde", user.approved_at ? stampBR(user.approved_at) : "—", false));
    card.append(infoLine("Contas", String(user.accounts), false));
  } else card.append(infoLine("Pediu em", user.created_at ? stampBR(user.created_at) : "—", false));
  if (actions.length) {
    const row = el("div", "mt-4 flex flex-wrap gap-2");
    row.append(...actions);
    card.append(row);
  }
  return card;
}
const INVITE_STATES = { aberto: ["Aberto", "st-healthy"], usado: ["Usado", "st-none"], vencido: ["Vencido", "st-search_ban"] };
function copyLink(link) {
  if (!navigator.clipboard) { toast("Copie o link à mão.", "info"); return; }
  navigator.clipboard.writeText(link).then(() => toast("Link copiado. Mande para a pessoa.")).catch(() => toast("Copie o link à mão.", "info"));
}
function inviteRow(invite) {
  const row = el("div", "invite-row rounded-2xl border border-neon/20 bg-ink");
  const head = el("div", "flex items-start justify-between gap-3");
  const [label, style] = INVITE_STATES[invite.status] || INVITE_STATES.vencido;
  head.append(el("p", "min-w-0 truncate text-sm text-white", invite.note || "Convite #" + invite.id),
    el("span", "shrink-0 rounded-full border px-2.5 py-1 text-xs font-medium " + style, label));
  row.append(head);
  const when = invite.status === "usado"
    ? "Usado por " + (invite.used_by || "conta apagada") + (invite.used_at ? " em " + stampBR(invite.used_at) : "")
    : (invite.status === "aberto" ? "Vence " : "Venceu ") + stampBR(invite.expires_at);
  row.append(el("p", "mt-1 text-xs text-mute", when));
  if (invite.link) {
    const link = el("button", "invite-link", invite.link);
    link.type = "button";
    link.title = "Copiar link";
    link.addEventListener("click", () => copyLink(invite.link));
    const actions = el("div", "mt-2 flex flex-wrap gap-2");
    actions.append(cardButton("Copiar link", () => copyLink(invite.link)), cardButton("Cancelar", () => cancelInvite(invite)));
    row.append(link, actions);
  }
  return row;
}
async function userAction(path, method, done) {
  try {
    await api(path, { method });
    toast(done);
    await loadUsers();
  } catch (error) { toast(error.message || "Não deu certo."); }
}
async function loadUsers() {
  try {
    const [users, invites] = await Promise.all([api("/api/admin/users"), api("/api/admin/invites")]);
    paintTabBadge("users", users.pending.length);
    const name = (user) => user.nome_exibicao || user.email;
    document.getElementById("users-pending").replaceChildren(...(users.pending.length
      ? users.pending.map((user) => userCard(user, [
        cardButton("Aprovar", () => userAction("/api/admin/users/" + user.id + "/approve", "POST", "Acesso liberado para " + name(user) + ".")),
        cardButton("Recusar", async () => {
          if (await showConfirm("Recusar o pedido?", "O cadastro de " + name(user) + " é apagado. Para entrar depois, a pessoa precisa de um convite novo.")) {
            userAction("/api/admin/users/" + user.id, "DELETE", "Pedido recusado.");
          }
        })
      ]))
      : [emptyState("users", "Nenhum pedido esperando", "Gere um convite e mande o link. Quem se cadastrar por ele aparece aqui para você aprovar.", "Gerar convite", newInvite)]));
    document.getElementById("users-approved").replaceChildren(...users.approved.map((user) => userCard(user, user.is_admin ? [] : [
      cardButton("Revogar acesso", async () => {
        if (await showConfirm("Tirar o acesso de " + name(user) + "?", "A pessoa sai na hora e volta para a tela de espera. As pastas e contas dela ficam guardadas.")) {
          userAction("/api/admin/users/" + user.id + "/revoke", "POST", "Acesso revogado.");
        }
      })
    ])));
    document.getElementById("invite-list").replaceChildren(...(invites.length
      ? invites.map(inviteRow)
      : [el("p", "text-sm text-mute", "Nenhum convite gerado ainda.")]));
  } catch (error) {
    if (error.message !== SESSION_EXPIRED) toast(error.message || "Não leu os usuários.");
  }
}
async function loadUsersBadge() {
  try { paintTabBadge("users", (await api("/api/admin/users")).pending.length); } catch { /* selo fica como estava */ }
}
async function newInvite() {
  const note = await showPrompt("Para quem é o convite? (opcional)");
  if (note === null) return;
  try {
    const invite = await api("/api/admin/invites", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note: note.trim() || null }) });
    copyLink(invite.link);
    await loadUsers();
  } catch (error) { toast(error.message || "Não gerou o convite."); }
}
async function cancelInvite(invite) {
  if (!(await showConfirm("Cancelar este convite?", "O link para de funcionar na hora."))) return;
  userAction("/api/admin/invites/" + invite.id, "DELETE", "Convite cancelado.");
}
document.getElementById("invite-new").addEventListener("click", newInvite);

// Gráfico só atualiza (e anima) quando os números ou o tema mudaram: o painel ao vivo e o auto-refresh
// não fazem as barras e linhas "pularem" à toa.
function chartSig(data) {
  return JSON.stringify([currentTheme(), data.labels, (data.datasets || []).map((set) => [set.label, set.data])]);
}
function markChart(chart) { chart.$sig = chartSig(chart.data); return chart; }
function updateChart(chart, data, options) {
  const sig = chartSig(data);
  if (chart.$sig === sig) return;
  chart.$sig = sig;
  chart.data = data;
  chart.options = options;
  chart.update();
}
function chartOptions() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } },
      y: { beginAtZero: true, ticks: { precision: 0 } }
    }
  };
}
function growthStat(label, value, extra) {
  const box = el("div", "growth-stat");
  box.append(el("p", "text-[11px] text-mute", label), el("p", "mt-0.5 font-mono text-base text-white", value));
  if (extra) box.append(extra);
  return box;
}
// Potencial de Crescimento: um dia mostra as views acumuladas hora a hora (o dia anterior tracejado);
// vários dias mostram views por dia com o período anterior por baixo. Os posts entram como barras.
function renderGrowth(data) {
  const g = data.growth;
  const hourly = g.mode === "hour";
  const label = data.compare_label || "";
  document.getElementById("growth-note").textContent = hourly
    ? "Views acumuladas ao longo do dia, pela hora do post"
    : "Views por dia dos posts lidos";
  const total = hourly ? g.current.filter((v) => v !== null).pop() || 0 : g.current.reduce((a, b) => a + b, 0);
  const stats = [growthStat("Total", num(total))];
  if (g.best) stats.push(growthStat(hourly ? "Melhor hora" : "Melhor dia", (hourly ? g.best.label : g.best.label.slice(8, 10) + "/" + g.best.label.slice(5, 7)) + ", " + num(g.best.views)));
  if (g.pace) stats.push(growthStat("Até as " + g.pace.until, num(g.pace.current), deltaChip(g.pace.current, g.pace.previous)));
  if (!hourly) stats.push(growthStat("Média por dia", num(g.avg)));
  const postsTotal = g.posts.reduce((a, b) => a + b, 0);
  stats.push(growthStat("Posts", num(postsTotal)));
  document.getElementById("growth-stats").replaceChildren(...stats);
  const labels = hourly ? g.labels : g.labels.map((day) => day.slice(8, 10) + "/" + day.slice(5, 7));
  const dense = g.labels.length > 40;
  const datasets = [{
    type: "line",
    label: hourly ? "Views (acumulado)" : "Views",
    legendColor: "#ff2ec8",
    data: g.current,
    borderColor: "#ff2ec8",
    backgroundColor: (ctx) => {
      const area = ctx.chart.chartArea;
      if (!area) return "rgba(255,46,200,.16)";
      const fill = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
      fill.addColorStop(0, "rgba(255,46,200,.32)");
      fill.addColorStop(1, "rgba(103,220,255,0)");
      return fill;
    },
    fill: true,
    cubicInterpolationMode: "monotone",  // a curva acumulada nunca desce entre dois pontos
    pointRadius: dense ? 0 : 2.5,
    pointHoverRadius: 5,
    borderWidth: 2.5,
    yAxisID: "y",
    order: 1
  }];
  if (g.previous) datasets.push({
    type: "line",
    label: label ? label.replace("vs ", "") : "Anterior",
    data: g.previous,
    borderColor: "rgba(155,140,255,.75)",
    legendColor: "#9b8cff",
    borderDash: [6, 5],
    borderWidth: 1.5,
    pointRadius: 0,
    fill: false,
    cubicInterpolationMode: "monotone",
    yAxisID: "y",
    order: 2
  });
  datasets.push({
    type: "bar",
    label: "Posts",
    legendColor: "#67dcff",
    data: g.posts,
    backgroundColor: "rgba(103,220,255,.22)",
    hoverBackgroundColor: "rgba(103,220,255,.45)",
    borderRadius: 4,
    maxBarThickness: 14,
    yAxisID: "y1",
    order: 3
  });
  const options = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    scales: {
      x: { grid: { display: false }, ticks: { maxTicksLimit: hourly ? 12 : 8 } },
      y: { beginAtZero: true, ticks: { precision: 0 } },
      y1: { beginAtZero: true, position: "right", grid: { display: false }, ticks: { precision: 0 }, suggestedMax: Math.max(...g.posts, 1) * 3, display: false }
    }
  };
  // Atualiza no lugar e anima do valor antigo para o novo (sem recriar o gráfico).
  if (lineChart) updateChart(lineChart, { labels, datasets }, options);
  else lineChart = markChart(new Chart(document.getElementById("line-chart"), { type: "line", data: { labels, datasets }, options }));
}
function renderCharts(data) {
  if (!window.Chart) return;
  document.querySelectorAll(".chart-skel").forEach((node) => node.classList.remove("chart-skel"));
  renderGrowth(data);
  // Melhor conta: barras deitadas, da maior para a menor. O @ fica legível com muitas contas e no
  // celular (em pé, os nomes ficavam inclinados e espremidos); a altura cresce com o nº de contas.
  const rows = [...data.by_account].sort((a, b) => (b.views || 0) - (a.views || 0));
  const canvas = document.getElementById("bar-chart");
  canvas.parentElement.style.height = Math.max(256, rows.length * 34 + 48) + "px";
  const barData = {
    labels: rows.length ? rows.map((row) => "@" + row.username) : ["—"],
    datasets: [{
      label: "Views",
      legendColor: "#ff2ec8",
      data: rows.length ? rows.map((row) => row.views) : [0],
      backgroundColor: (ctx) => {
        const area = ctx.chart.chartArea;
        if (!area) return "#ff2ec8";
        const fill = ctx.chart.ctx.createLinearGradient(area.left, 0, area.right, 0);
        fill.addColorStop(0, "#8b6cff");
        fill.addColorStop(1, "#ff2ec8");
        return fill;
      },
      borderRadius: 8,
      borderSkipped: false,
      maxBarThickness: 22
    }]
  };
  const barOptions = {
    indexAxis: "y",
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { beginAtZero: true, ticks: { precision: 0, maxTicksLimit: 6 } },
      y: { grid: { display: false }, ticks: { autoSkip: false } }
    }
  };
  // Atualiza os gráficos no lugar (sem destruir/recriar), para não piscar no auto-refresh.
  if (barChart) updateChart(barChart, barData, barOptions);
  else barChart = markChart(new Chart(canvas, { type: "bar", data: barData, options: barOptions }));
}
// Top 3 horários do build_suggestions (vem junto no /api/dashboard, com o mesmo período e pasta).
const WEEK_SHORT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"];
// Grade dia da semana × hora: quanto mais forte, maior a média de views dos posts daquele horário.
function renderHeatmap(grid) {
  const host = document.getElementById("heatmap");
  const values = (grid || []).flat().filter((v) => v !== null);
  document.getElementById("heat-legend").classList.toggle("hidden", !values.length);
  if (!values.length) { host.replaceChildren(); return; }
  const max = Math.max(...values, 1);
  const cells = [el("span")];
  for (let hour = 0; hour < 24; hour++) cells.push(el("span", "heat-hour", hour % 6 === 0 ? String(hour).padStart(2, "0") + "h" : ""));
  grid.forEach((row, day) => {
    cells.push(el("span", "heat-day", WEEK_SHORT[day]));
    row.forEach((value, hour) => {
      const cell = el("span", "heat-cell");
      if (value !== null) {
        cell.style.background = "rgba(255,99,195," + (0.12 + 0.88 * value / max).toFixed(2) + ")";
        cell.title = WEEK_SHORT[day] + " " + String(hour).padStart(2, "0") + "h: " + num(value) + " views em média";
      }
      cells.push(cell);
    });
  });
  host.replaceChildren(...cells);
}
function renderSuggestions(suggestions) {
  renderHeatmap(suggestions && suggestions.heatmap);
  const host = document.getElementById("best-times");
  const items = (suggestions && suggestions.items) || [];
  document.getElementById("best-times-note").textContent = (suggestions && suggestions.note) || "";
  host.replaceChildren(...items.map((item, index) => {
    const li = document.createElement("li");
    li.className = "rounded-2xl border px-4 py-4 text-center " + (index === 0 ? "border-neon/50 bg-neon/10" : "border-neon/20 bg-ink");
    const rank = document.createElement("p");
    rank.className = "text-xs " + (index === 0 ? "text-neon" : "text-mute");
    rank.textContent = index + 1 + "º lugar";
    const hour = document.createElement("p");
    hour.className = "mt-2 font-mono text-2xl text-white tracking-tight";
    hour.textContent = item.label;
    const views = document.createElement("p");
    views.className = "mt-1 text-sm text-white";
    views.textContent = num(Math.round(item.avg_views)) + " views em média";
    const meta = document.createElement("p");
    meta.className = "mt-1 text-xs text-mute";
    meta.textContent = "melhor dia: " + item.best_weekday + " · " + item.samples + (item.samples === 1 ? " post" : " posts");
    li.append(rank, hour, views, meta);
    return li;
  }));
}
// Busca e ordem das contas do Dashboard (a ordem escolhida fica no aparelho).
const ACC_SORT_KEY = "acc_sort";
const STATUS_ORDER = { ghost_ban: 0, search_ban: 1, suspended: 2, not_found: 3, protected: 4, rate_limited: 5, api_error: 6, unknown: 7, healthy: 9 };
function plain(text) { return String(text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); }
function accountSort() { try { return localStorage.getItem(ACC_SORT_KEY) || "views"; } catch { return "views"; } }
function paintAccountSort() {
  const current = accountSort();
  document.querySelectorAll("#acc-sort [data-sort]").forEach((button) => {
    const on = button.dataset.sort === current;
    button.classList.toggle("on", on);
    button.setAttribute("aria-checked", String(on));
  });
}
function sortedAccounts(accounts) {
  const query = plain(document.getElementById("acc-search").value.trim().replace(/^@/, ""));
  const list = accounts.filter((a) => !a.is_archived && (!query || [a.display_name, a.username, a.folder_name].some((v) => plain(v).includes(query))));
  const by = accountSort();
  const name = (a) => plain(a.display_name || a.username);
  const compare = {
    views: (a, b) => (b.views || 0) - (a.views || 0),
    status: (a, b) => (STATUS_ORDER[a.status] ?? 8) - (STATUS_ORDER[b.status] ?? 8) || (b.views || 0) - (a.views || 0),
    folder: (a, b) => plain(a.folder_name).localeCompare(plain(b.folder_name)) || (b.views || 0) - (a.views || 0),
    name: (a, b) => name(a).localeCompare(name(b))
  }[by] || (() => 0);
  return list.sort(compare);
}
function folderColor(folderId) {
  const folder = ((state.data && state.data.folders) || []).find((item) => item.id === folderId);
  return folder && /^#[0-9a-f]{3,8}$/i.test(folder.color || "") ? folder.color : null;
}
function renderAccounts(accounts) {
  const host = document.getElementById("accounts");
  if (!accounts.some((a) => !a.is_archived)) {
    host.replaceChildren(emptyState("ops", "Nenhuma conta ainda", "Crie uma pasta em Operações e adicione as contas que você quer acompanhar.", "Abrir Operações", () => setTab("ops")));
    return;
  }
  const visible = sortedAccounts(accounts);
  if (!visible.length) {
    host.replaceChildren(emptyState("search", "Nenhuma conta encontrada", "Nada com “" + document.getElementById("acc-search").value.trim() + "” no nome, no @ ou na pasta.", "Limpar busca", () => {
      document.getElementById("acc-search").value = "";
      renderAccounts(accounts);
    }));
    return;
  }
  host.replaceChildren(...visible.map((account) => {
    const card = document.createElement("article");
    card.className = "account-card glow relative cursor-pointer rounded-2xl border border-neon/20 bg-card p-4 text-center flex flex-col items-center justify-center";
    card.dataset.accountId = String(account.id);
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", "Abrir detalhes de @" + account.username);
    card.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      openDrawer(account.id);
    });
    card.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && event.target === card) { event.preventDefault(); openDrawer(account.id); }
    });
    const color = folderColor(account.folder_id);
    if (color) {
      const stripe = el("span", "folder-stripe");
      stripe.style.background = color;
      card.append(stripe);
      card.style.setProperty("--folder", color);
    }
    const name = document.createElement("p");
    name.className = "font-medium text-white";
    name.textContent = account.display_name || "@" + account.username;
    const handle = document.createElement("p");
    handle.className = "text-sm text-mute";
    handle.textContent = "@" + account.username + " · " + account.folder_name;
    if (color) handle.prepend(el("span", "folder-dot"));
    const grid = document.createElement("dl");
    grid.className = "mt-4 grid grid-cols-2 items-center justify-center gap-x-2 gap-y-3 text-center text-sm sm:grid-cols-4";
    [["Views", num(account.views)], ["Curtidas", num(account.likes)], ["Retweets", num(account.retweets)], ["Seguidores", num(account.followers)]].forEach(([label, value]) => {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt");
      dt.className = "text-[11px] text-mute";
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.className = "mt-1 text-white";
      dd.textContent = value;
      wrap.append(dt, dd);
      grid.append(wrap);
    });
    const trend = account.spark && account.spark.some((v) => v > 0) ? miniLine(account.spark) : null;
    // Status sempre à vista no canto (no celular não existe "passar o mouse"); o botão Verificar e o
    // detalhe da última verificação aparecem ao passar o mouse ou tocar no cartão.
    const badge = el("span", "account-status rounded-full border px-2.5 py-0.5 text-xs font-medium st-" + (account.status || "none"), account.status_label);
    const reveal = document.createElement("div");
    reveal.className = "status-reveal mt-4 flex flex-col items-center gap-2";
    const check = document.createElement("button");
    check.type = "button";
    check.className = "rounded-full border border-neon/40 px-3 py-1 text-xs";
    check.textContent = "Verificar agora";
    check.addEventListener("click", () => verify(account));
    reveal.append(check);
    if (account.detail) reveal.append(el("p", "text-[11px] text-mute", account.detail));
    card.append(badge, name, handle);
    if (trend) card.append(trend);
    const viewsChip = deltaChip(account.views, account.views_prev);
    viewsChip.classList.add("account-delta");
    card.append(viewsChip);
    card.append(grid, reveal);
    return card;
  }));
}
function renderFolders(folders) {
  const host = document.getElementById("folder-cards");
  if (!folders.length) {
    host.replaceChildren(emptyState("ops", "Nenhuma pasta ainda", "Pastas agrupam as contas monitoradas (por nicho, cliente ou como preferir).", "Criar pasta", () => document.getElementById("folder-name").focus()));
    return;
  }
  host.replaceChildren(...folders.map((folder) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "glow flex w-full items-center justify-between gap-3 rounded-3xl border border-neon/20 bg-card px-5 py-6 text-left transition hover:border-neon hover:bg-neon/10";
    const info = document.createElement("div");
    info.className = "min-w-0 flex-1";
    const name = document.createElement("p");
    name.className = "truncate font-display text-2xl text-white";
    name.textContent = folder.name;
    const meta = document.createElement("p");
    meta.className = "mt-2 text-sm text-mute";
    meta.textContent = folder.accounts + (folder.accounts === 1 ? " conta" : " contas");
    info.append(name, meta);
    const arrow = document.createElement("span");
    arrow.className = "shrink-0 text-2xl text-neon";
    arrow.textContent = "\u203a";
    card.append(info, arrow);
    card.addEventListener("click", () => openOpsFolder(folder.id));
    return card;
  }));
}
// Operações: tela 1 lista as pastas; tela 2 (pasta aberta) mostra só as contas daquela pasta.
function opsFolder() {
  const folders = (state.data && state.data.folders) || [];
  return folders.find((folder) => folder.id === state.opsFolderId) || null;
}
function renderOpsView() {
  const folder = opsFolder();
  if (state.opsFolderId && !folder && state.data) state.opsFolderId = null;  // pasta excluída
  document.getElementById("ops-folders-view").classList.toggle("hidden", Boolean(folder));
  document.getElementById("ops-folder-view").classList.toggle("hidden", !folder);
  if (!folder) return;
  document.getElementById("ops-folder-name").textContent = folder.name;
  renderOpsAccounts((state.data && state.data.accounts) || []);
}
function openOpsFolder(folderId) {
  state.opsFolderId = folderId;
  renderOpsView();
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function closeOpsFolder() {
  state.opsFolderId = null;
  renderOpsView();
}
function renderOpsAccounts(accounts) {
  const host = document.getElementById("ops-accounts");
  if (!state.opsFolderId) { host.replaceChildren(); return; }
  const rows = accounts.filter((account) => account.folder_id === state.opsFolderId && !account.is_archived);
  if (!rows.length) {
    host.replaceChildren(emptyState("account", "Nenhuma conta nesta pasta", "Adicione o @ de uma conta acima e o robô começa a ler na próxima rodada.", "Adicionar conta", () => document.getElementById("account-handle").focus()));
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
    handle.textContent = "@" + account.username;
    const grid = document.createElement("dl");
    grid.className = "mt-3 grid grid-cols-2 gap-x-2 gap-y-3 text-sm sm:grid-cols-4";
    [["Views", num(account.views)], ["Curtidas", num(account.likes)], ["Retweets", num(account.retweets)], ["Seguidores", num(account.followers)]].forEach(([label, value]) => {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt");
      dt.className = "text-[11px] text-mute";
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
    const archiveBtn = document.createElement("button");
    archiveBtn.type = "button";
    archiveBtn.className = "rounded-full border border-neon/40 px-3 py-1 text-xs";
    archiveBtn.textContent = "Arquivar";
    archiveBtn.addEventListener("click", () => archiveAccount(account));
    actions.append(editBtn, archiveBtn, deleteBtn);
    card.append(name, handle, grid, actions);
    return card;
  }));
}
function renderLastCollect(iso) {
  const el = document.getElementById("last-collect");
  if (!el) return;
  el.classList.remove("text-amber-300");
  if (!iso) { el.textContent = "Última leitura: nenhuma ainda"; return; }
  const when = new Date(iso);
  const mins = Math.max(0, Math.round((Date.now() - when.getTime()) / 60000));
  const stamp = when.toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const ago = mins < 1 ? "agora" : mins < 60 ? "há " + mins + " min" : mins < 1440 ? "há " + Math.floor(mins / 60) + " h" : "há " + Math.floor(mins / 1440) + " d";
  el.textContent = "Última leitura: " + stamp + " (" + ago + ")";
  if (mins > 90) el.classList.add("text-amber-300");
}
// Dashboard na hora: cada período já visto fica em memória (dashCache) e aparece assim que é
// escolhido, enquanto a versão nova chega por trás; o de "Hoje" também fica salvo no aparelho
// (DASH_SNAPSHOT_KEY) para o painel abrir já preenchido. Depois da primeira carga os outros
// períodos são buscados com o navegador ocioso.
const DASH_SNAPSHOT_KEY = "dash_snapshot";
const PREFETCH_PERIODS = ["yesterday", "7d", "30d", "3m", "1y", "total"];
const dashCache = new Map();
let loadSeq = 0;
let loadInflight = false;
let lastSig = "";
let prefetchedFor;                          // pasta já pré-carregada (null = todas; undefined = nenhuma ainda)
let sideLoadedAt = 0;
function dashKey(period, folderId) { return period + "|" + (folderId || ""); }
function dashQuery(period, folderId) {
  const query = new URLSearchParams({ period });
  if (folderId) query.set("folder_id", String(folderId));
  return "/api/dashboard?" + query.toString();
}
function renderDashboard(data, sig, swap, live) {
  if (sig === lastSig) return;              // nada mudou: não mexe no DOM (zero flicker)
  lastSig = sig;
  const before = state.data;
  state.data = data;
  if (swap) {                               // troca de período/pasta: os números entram com um fade curto
    const pane = document.getElementById("pane-dashboard");
    pane.classList.remove("dash-swap");
    void pane.offsetWidth;
    pane.classList.add("dash-swap");
  }
  renderLastCollect(data.last_collected_at);
  document.getElementById("banner").classList.add("hidden");
  document.getElementById("range-label").textContent = data.start + " → " + data.end;
  document.getElementById("kpis").removeAttribute("aria-busy");
  renderKpis(data.kpis);
  renderTopPost(data.top_post);
  renderRanking(data.top_posts);
  renderCharts(data);
  renderSuggestions(data.suggestions);
  renderAccounts(data.accounts);
  renderFolders(data.folders);
  renderOpsView();
  // Atualização automática com números novos: os cartões que mudaram ganham uma passada de laser.
  if (live && before && before.start === data.start && before.end === data.end) flashChanges(before, data);
}
function rememberDash(period, folderId, data) {
  const sig = JSON.stringify(data);
  dashCache.set(dashKey(period, folderId), { data, sig });
  if (period === "today" && !folderId) {
    try { localStorage.setItem(DASH_SNAPSHOT_KEY, sig); } catch { /* cheio ou bloqueado: só não guarda */ }
  }
  return sig;
}
// Abre o painel com o último "Hoje" salvo, antes de qualquer resposta do servidor.
function paintSnapshot() {
  let raw = null;
  try { raw = localStorage.getItem(DASH_SNAPSHOT_KEY); } catch { return; }
  if (!raw || state.period !== "today" || state.folderId) return;
  try {
    const data = JSON.parse(raw);
    dashCache.set(dashKey("today", null), { data, sig: raw });
    renderDashboard(data, raw);
  } catch { /* snapshot velho ou corrompido: espera o servidor */ }
}
function prefetchPeriods() {
  const folder = state.folderId || null;
  if (prefetchedFor === folder) return;
  prefetchedFor = folder;
  const idle = window.requestIdleCallback ? (fn) => requestIdleCallback(fn, { timeout: 2000 }) : (fn) => setTimeout(fn, 400);
  idle(async () => {
    for (const period of PREFETCH_PERIODS) {
      if (document.hidden || (state.folderId || null) !== folder) return;
      if (dashCache.has(dashKey(period, folder))) continue;
      try { rememberDash(period, folder, await api(dashQuery(period, folder))); } catch { return; }
    }
  });
}
async function load({ silent = false } = {}) {
  if (silent && loadInflight) return;       // auto-refresh nunca empilha requisições
  const seq = ++loadSeq;
  loadInflight = true;
  const period = state.period;
  const folderId = state.folderId || null;
  const cached = dashCache.get(dashKey(period, folderId));
  if (cached && !silent) renderDashboard(cached.data, cached.sig, true);  // na hora; a versão nova vem já já
  try {
    const data = await api(dashQuery(period, folderId));
    const sig = rememberDash(period, folderId, data);
    if (seq !== loadSeq) return;            // chegou uma resposta mais nova: descarta esta
    // Rotas com login que não dependem do período: no máximo uma vez a cada 30 s, não a cada clique.
    if (Date.now() - sideLoadedAt > 30000) {
      sideLoadedAt = Date.now();
      roleSideLoads();
      loadAlerts();
      checkViral();
    }
    renderLastCollect(data.last_collected_at);
    renderDashboard(data, sig, !silent && !cached && lastSig !== "", silent);
    markLive(true);
    startLive();
    if (!silent) prefetchPeriods();
  } catch (error) {
    if (error.message === PENDING || error.message === SESSION_EXPIRED) return;
    if (!silent) toast(error.message || "Falha ao carregar.");
    else markLive(false);
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
    if (state.opsFolderId === folder.id) state.opsFolderId = null;
    await load();
  } catch (error) { toast(error.message || "Não excluiu a pasta."); }
}
async function removeAccount(account) {
  const ok = await showConfirm("Remover conta", "Tem certeza que deseja remover @" + account.username + "?");
  if (!ok) return;
  try { await api("/api/accounts/" + account.id, { method: "DELETE" }); await load(); }
  catch (error) { toast(error.message || "Não removeu."); }
}

async function setArchived(account, archived) {
  await api("/api/accounts/" + account.id, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ is_archived: archived })
  });
}
async function archiveAccount(account) {
  const ok = await showConfirm(
    "Arquivar conta",
    "@" + account.username + " sai do dashboard e o robô para de ler. O histórico fica guardado na aba Arquivadas."
  );
  if (!ok) return;
  try {
    await setArchived(account, true);
    toast("@" + account.username + " arquivada.");
    await load();
  } catch (error) { toast(error.message || "Não arquivou."); }
}
async function unarchiveAccount(account) {
  try {
    await setArchived(account, false);
    toast("@" + account.username + " voltou para o dashboard.");
    await Promise.all([loadArchived(), load({ silent: true })]);
  } catch (error) { toast(error.message || "Não desarquivou."); }
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
// Celular: abas embaixo; "Mais" abre as que não cabem (MORE_TABS).
const MORE_TABS = ["archived", "post", "cloaker", "settings"];
function setMoreOpen(open) {
  document.getElementById("more-sheet").classList.toggle("open", open);
  document.getElementById("more-btn").setAttribute("aria-expanded", String(open));
}
document.getElementById("bottom-nav").addEventListener("click", (event) => {
  if (event.target.closest("#more-btn")) {
    setMoreOpen(!document.getElementById("more-sheet").classList.contains("open"));
    return;
  }
  const button = event.target.closest("[data-tab]");
  if (button) setTab(button.dataset.tab);
});
document.getElementById("more-sheet").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (button) setTab(button.dataset.tab);
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#more-sheet, #more-btn")) setMoreOpen(false);
});
document.getElementById("period-wrap").addEventListener("click", (event) => {
  const button = event.target.closest("[data-period]");
  if (!button) return;
  setPeriod(button.dataset.period);
  load();
});
document.getElementById("ops-back").addEventListener("click", closeOpsFolder);
document.getElementById("ops-folder-edit").addEventListener("click", () => { const folder = opsFolder(); if (folder) editFolder(folder); });
document.getElementById("ops-folder-delete").addEventListener("click", () => { const folder = opsFolder(); if (folder) deleteFolder(folder); });
document.getElementById("folder-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const created = await api("/api/folders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: document.getElementById("folder-name").value, color: "#FF2EC8" })
    });
    event.target.reset();
    state.opsFolderId = created.id;
    await load();
  } catch (error) { toast(error.message || "Não criou a pasta."); }
});
document.getElementById("account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.opsFolderId) { toast("Abra uma pasta."); return; }
  try {
    const created = await api("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder_id: state.opsFolderId,
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
      if (xhr.status === 401 && token()) {
        sessionExpired();
        reject(new Error(SESSION_EXPIRED));
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
const SAVED_KEY = "Já salva (vazio mantém)";
function paintKeyField(id, key, saved, empty) {
  document.getElementById(id).placeholder = saved ? SAVED_KEY : empty;
  document.querySelector('[data-clear-key="' + key + '"]').classList.toggle("hidden", !saved);
}
async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    document.getElementById("goal").value = settings.impression_goal;
    if (isAdmin()) {
      const tg = document.getElementById("tg-status");
      tg.textContent = settings.telegram_ready
        ? "Telegram: usado só no \"Esqueceu a senha?\" e no aviso de falha do backup. Os alertas vêm pelas Notificações."
        : "Telegram: falta TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env do VPS (o \"Esqueceu a senha?\" e o aviso do backup dependem dele).";
      tg.classList.toggle("text-amber-300", !settings.telegram_ready);
      loadRobo();
    }
    paintApify(Object.assign({ token_set: settings.apify_token_set }, settings.apify));
    if (!isAdmin()) api("/api/apify").then(paintReading).catch(() => {});
    loadNotify();
    paintKeyField("xai-key", "xai_api_key", settings.xai_key_set, "xai-...");
    paintKeyField("x-token", "x_bearer_token", settings.x_token_set, "AAAA...");
    paintKeyField("apify-token", "apify_token", settings.apify_token_set, "apify_api_...");
    const me = await api("/api/me");
    document.getElementById("profile-name").value = me.nome_exibicao || "";
    paintUser(me);
  } catch (error) { toast(error.message || "Não leu as configurações."); }
}

let archivedCharts = [];
function dateBR(iso) {
  return iso ? new Date(iso).toLocaleDateString("pt-BR", { timeZone: "America/Sao_Paulo" }) : "—";
}
function archivedCard(account) {
  const card = document.createElement("article");
  card.className = "glow rounded-2xl border border-neon/20 bg-card p-4";
  const head = document.createElement("div");
  head.className = "flex items-start justify-between gap-3";
  const who = document.createElement("div");
  who.className = "min-w-0";
  const name = document.createElement("p");
  name.className = "font-medium text-white";
  name.textContent = account.display_name || "@" + account.username;
  const handle = document.createElement("p");
  handle.className = "text-sm text-mute";
  handle.textContent = "@" + account.username + " · " + account.folder_name;
  who.append(name, handle);
  const badge = document.createElement("span");
  badge.className = "shrink-0 rounded-full border px-2.5 py-1 text-xs font-medium st-" + (account.status || "none");
  badge.textContent = account.status_label;
  head.append(who, badge);

  const dates = document.createElement("p");
  dates.className = "mt-2 text-xs text-mute";
  const span = account.first_post_at ? "posts de " + dateBR(account.first_post_at) + " a " + dateBR(account.last_post_at) : "nenhum post lido";
  dates.textContent = "Arquivada em " + dateBR(account.archived_at) + " · " + span;

  const grid = document.createElement("dl");
  grid.className = "mt-4 grid grid-cols-3 gap-x-2 gap-y-3 text-center text-sm sm:grid-cols-5";
  [["Views", account.views], ["Curtidas", account.likes], ["Retweets", account.retweets], ["Replies", account.replies], ["Seguidores", account.followers]].forEach(([label, value]) => {
    const wrap = document.createElement("div");
    const dt = document.createElement("dt");
    dt.className = "text-[11px] text-mute";
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.className = "mt-1 text-white";
    dd.textContent = num(value);
    wrap.append(dt, dd);
    grid.append(wrap);
  });

  const chartBox = document.createElement("div");
  chartBox.className = "mt-4 h-32";
  const history = account.history || [];
  if (window.Chart && history.length) {
    const canvas = document.createElement("canvas");
    chartBox.append(canvas);
    const options = chartOptions();
    options.plugins.legend = { display: false };
    archivedCharts.push(new Chart(canvas, {
      type: "line",
      data: {
        labels: history.map((day) => day.date.slice(5)),
        datasets: [{
          label: "Views", data: history.map((day) => day.views), borderColor: "#ff2ec8",
          backgroundColor: "rgba(255,46,200,.16)", fill: true, tension: 0.35, pointRadius: history.length > 40 ? 0 : 2, borderWidth: 2
        }]
      },
      options
    }));
  } else {
    chartBox.className = "mt-4 flex h-16 items-center justify-center rounded-2xl border border-neon/10 text-xs text-mute";
    chartBox.textContent = "Sem histórico diário de views.";
  }

  const unarchive = document.createElement("button");
  unarchive.type = "button";
  unarchive.className = "mt-4 rounded-full border border-neon/40 px-3 py-1 text-xs";
  unarchive.textContent = "Desarquivar";
  unarchive.addEventListener("click", () => unarchiveAccount(account));
  card.append(head, dates, grid, chartBox, unarchive);
  return card;
}
async function loadArchived() {
  const host = document.getElementById("archived-accounts");
  if (!host) return;
  const message = (text) => {
    const p = document.createElement("p");
    p.className = "text-sm text-mute";
    p.textContent = text;
    return p;
  };
  try {
    const accounts = await api("/api/accounts/archived");
    archivedCharts.forEach((chart) => chart.destroy());
    archivedCharts = [];
    host.replaceChildren(...(accounts.length ? accounts.map(archivedCard) : [emptyState("archived", "Nenhuma conta arquivada", "Arquivar tira a conta do Dashboard e da leitura, mas o histórico fica guardado aqui.")]));
  } catch (error) {
    host.replaceChildren(message(error.message || "Erro ao carregar."));
  }
}
function infoLine(label, value, warn) {
  const p = document.createElement("p");
  p.className = "mt-1 break-all text-xs " + (warn ? "text-amber-200" : "text-mute");
  const b = document.createElement("span");
  b.className = "text-white/70";
  b.textContent = label + ": ";
  p.append(b, document.createTextNode(value));
  return p;
}
function cardButton(text, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "rounded-full border border-neon/40 px-3 py-1 text-xs";
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
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
// O Cropper só baixa quando alguém escolhe uma foto (fora da abertura do painel).
const CROPPER_BASE = "https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.1/";
let cropperReady = null;
function loadCropper() {
  if (window.Cropper) return Promise.resolve();
  if (!cropperReady) {
    cropperReady = new Promise((resolve, reject) => {
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = CROPPER_BASE + "cropper.min.css";
      const js = document.createElement("script");
      js.src = CROPPER_BASE + "cropper.min.js";
      js.onload = resolve;
      js.onerror = () => { cropperReady = null; reject(new Error("Não carregou o recorte da foto. Tente de novo.")); };
      document.head.append(css, js);
    });
  }
  return cropperReady;
}

if (profilePhotoInput) {
  profilePhotoInput.addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (ev) => {
      try { await loadCropper(); } catch (error) { toast(error.message); return; }
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
// Chave em branco = mantém a salva; "Apagar" manda "" e o servidor apaga.
const KEY_FIELDS = [["xai-key", "xai_api_key"], ["x-token", "x_bearer_token"], ["apify-token", "apify_token"]];
async function putSettings(payload) {
  return api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
}
document.getElementById("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = { impression_goal: Number(document.getElementById("goal").value) };
  KEY_FIELDS.forEach(([id, key]) => {
    const value = document.getElementById(id).value.trim();
    if (value) payload[key] = value;
  });
  try {
    await putSettings(payload);
    KEY_FIELDS.forEach(([id]) => { document.getElementById(id).value = ""; });
    toast(payload.apify_token ? "Token do Apify salvo: a primeira leitura sai em até 1 minuto." : "Configuração salva.");
    await loadSettings();
    if (payload.apify_token) loadApify();
  } catch (error) { toast(error.message || "Não salvou."); }
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-clear-key]");
  if (!button) return;
  const key = button.dataset.clearKey;
  const label = { xai_api_key: "a chave da xAI", x_bearer_token: "o token da API do X", apify_token: "o token do Apify" }[key];
  if (!(await showConfirm("Apagar " + label + "?", "Fica sem chave salva até você colar outra."))) return;
  try {
    await putSettings({ [key]: "" });
    toast("Apagado.");
    await loadSettings();
    if (key === "apify_token") loadApify();
  } catch (error) { toast(error.message || "Não apagou."); }
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
    const result = await api("/api/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nova_senha: nova })
    });
    // O token antigo deixou de valer; guarda o novo para continuar logado aqui.
    if (result && result.access_token) localStorage.setItem(TOKEN_KEY, result.access_token);
    document.getElementById("new-password").value = "";
    toast("Senha atualizada. Os outros aparelhos foram desconectados.");
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

// Robô: a aba Configurações liga/desliga a leitura automática e escolhe o intervalo; o robo.py lê isso
// em app_settings a cada minuto (cron). "Atualizar dados" grava um pedido e espera a rodada terminar.
function intervalLabel(minutes) {
  return minutes < 60 ? minutes + " min" : minutes / 60 + (minutes === 60 ? " hora" : " horas");
}
function stampBR(iso) {
  return new Date(iso).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function fillIntervals(select, intervals, value, disabled) {
  select.replaceChildren(...intervals.map((minutes) => {
    const option = document.createElement("option");
    option.value = String(minutes);
    option.textContent = intervalLabel(minutes);
    return option;
  }));
  select.value = String(value);
  select.disabled = disabled;
}
function paintPause(prefix, pause) {
  document.getElementById(prefix + "-pause-on").checked = Boolean(pause.on);
  ["from", "to"].forEach((end) => {
    const field = document.getElementById(prefix + "-pause-" + end);
    field.value = pause[end];
    field.disabled = !pause.on;
  });
}
function pauseText(pause) {
  return pause.on ? " Pausa de " + pause.from + " às " + pause.to + "." : "";
}
function watchPause(prefix, save) {
  document.getElementById(prefix + "-pause-on").addEventListener("change", (event) => save({ pause: { on: event.target.checked } }));
  ["from", "to"].forEach((end) => document.getElementById(prefix + "-pause-" + end).addEventListener("change", (event) => {
    if (event.target.value) save({ pause: { [end]: event.target.value } });
  }));
}
function paintRobo(robo) {
  paintPause("robo", robo.pause);
  const toggle = document.getElementById("robo-enabled");
  toggle.checked = robo.enabled;
  fillIntervals(document.getElementById("robo-apify-interval"), robo.apify.intervals, robo.apify.interval_min, !robo.enabled);
  const parts = [robo.apify.active ? "Lendo pelo Apify." : "Sem token do Apify: cole o seu em Configurações para o robô ler as suas contas."];
  if (robo.last_run) {
    const run = robo.last_run;
    let text = "Última leitura: " + stampBR(run.finished_at);
    if (run.contas !== null && run.contas !== undefined) text += " · " + run.contas + " contas, " + (run.tweets || 0) + " tweets";
    if (run.status === "failed") text += " · falhou";
    else if (run.status === "partial") text += " · com falhas";
    parts.push(text);
  } else parts.push("O robô ainda não rodou.");
  if (robo.requested_at) parts.push("Leitura pedida às " + stampBR(robo.requested_at) + ", aguardando o robô.");
  else if (!robo.enabled) parts.push("Leitura automática desligada.");
  else if (robo.next_run_at) parts.push("Próxima: por volta de " + stampBR(robo.next_run_at) + ".");
  document.getElementById("robo-status").textContent = parts.join(" ");
}
async function loadRobo() {
  try { paintRobo(await api("/api/robo")); }
  catch (error) { document.getElementById("robo-status").textContent = error.message || "Não leu o robô."; }
}
async function saveRobo(body) {
  try {
    const robo = await api("/api/robo", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    paintRobo(robo);
    if (body.pause) toast(robo.pause.on ? "Pausa salva: de " + robo.pause.from + " às " + robo.pause.to + "." : "Pausa da madrugada desligada.");
    else toast(body.enabled === false ? "Leitura automática desligada." : body.enabled ? "Leitura automática ligada: a primeira rodada sai em até 1 minuto." : "Intervalo salvo: a cada " + intervalLabel(robo.apify.interval_min) + ".");
  } catch (error) {
    toast(error.message || "Não salvou o robô.");
    loadRobo();
  }
}
document.getElementById("robo-enabled").addEventListener("change", (event) => saveRobo({ enabled: event.target.checked }));
watchPause("robo", saveRobo);
document.getElementById("robo-apify-interval").addEventListener("change", (event) => saveRobo({ apify_interval_min: Number(event.target.value) }));

const ROBO_PICKUP_MS = 3 * 60000;   // cron a cada minuto: passou disso, o robô não está pegando os pedidos
const ROBO_RUN_MS = 12 * 60000;     // uma leitura com muitas contas pode levar alguns minutos no Apify
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const READ_POLL_MS = 2000;
async function refreshByApify(btn) {
  btn.textContent = "Lendo pelo Apify...";
  let apify = await api("/api/apify/run", { method: "POST" });
  paintApify(apify);
  const started = Date.now();
  while (apify.running || apify.requested_at) {
    // O painel lê na hora (sem Postgres, o robô pega o pedido em até 1 minuto); o Apify leva segundos.
    if (Date.now() - started > ROBO_RUN_MS) throw new Error("A leitura está demorando. Os dados aparecem sozinhos quando ela terminar.");
    await wait(READ_POLL_MS);
    apify = await api("/api/apify");
    paintApify(apify);
  }
  await load();
  if (apify.status === "ok") toast("Leitura feita: " + (apify.detail || "dados atualizados") + ".");
  else toast("A leitura falhou: " + (apify.detail || "erro no Apify") + ".", "error");
}
document.getElementById("refresh-all").addEventListener("click", async () => {
  const btn = document.getElementById("refresh-all");
  const orig = btn.textContent;
  btn.disabled = true;
  if (!isAdmin()) {
    try { await refreshByApify(btn); } catch (error) { if (error.message !== PENDING) toast(error.message || "Falha ao atualizar."); }
    btn.disabled = false;
    btn.textContent = orig;
    return;
  }
  btn.textContent = "Atualizando...";
  try {
    const asked = await api("/api/robo/run", { method: "POST" });
    const askedAt = Date.parse(asked.asked_at || asked.requested_at);
    const started = Date.now();
    let robo = asked;
    // O painel lê na hora pelo Apify; sem Postgres o robô pega o pedido em até 1 minuto.
    const say = (r) => { btn.textContent = r.running ? "Lendo pelo Apify..." : r.requested_at ? "Esperando o robô..." : "Lendo..."; };
    say(robo);
    let pickedFrom = Date.now();
    for (;;) {
      await wait(READ_POLL_MS);
      robo = await api("/api/robo");
      const finished = robo.last_run ? Date.parse(robo.last_run.finished_at) : 0;
      if (!robo.running && !robo.requested_at && finished >= askedAt) break;
      say(robo);
      if (robo.running) pickedFrom = Date.now();  // o prazo do cron só conta depois da leitura na hora
      else if (robo.requested_at && Date.now() - pickedFrom > ROBO_PICKUP_MS) {
        throw new Error("O robô não pegou o pedido. Confira no VPS se o cron roda o robo.py a cada minuto.");
      }
      if (Date.now() - started > ROBO_RUN_MS) throw new Error("A leitura está demorando. Os dados aparecem sozinhos quando ela terminar.");
    }
    await load();
    const run = robo.last_run;
    if (run.status === "failed") toast("A leitura falhou: " + (run.error || "erro no Apify") + ".", "error");
    else toast("Leitura feita pelo Apify: " + (run.contas || 0) + " contas, " + (run.tweets || 0) + " tweets.");
    if (state.tab === "settings") paintRobo(robo);
  } catch (error) { toast(error.message || "Falha ao atualizar."); }
  btn.disabled = false;
  btn.textContent = orig;
});
function clearInviteLink() {
  state.invite = "";
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
}
function clearResetLink() {
  state.resetToken = "";
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
}
document.getElementById("auth-switch").addEventListener("click", () => {
  if (state.authMode === "reset") clearResetLink();
  setAuthMode(state.authMode === "login" ? "register" : "login");
});
document.getElementById("forgot-link").addEventListener("click", () => setAuthMode("forgot"));
document.getElementById("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  authMessage("auth-error", "");
  const email = document.getElementById("auth-email").value;
  const senha = document.getElementById("auth-password").value;
  const json = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  try {
    if (state.authMode === "forgot") {
      const answer = await api("/api/password-reset/request", json({ email }));
      authMessage("auth-info", answer.detail || "Link enviado ao Telegram.");
      return;
    }
    if (state.authMode === "reset") {
      if (senha !== document.getElementById("auth-password2").value) throw new Error("As senhas não conferem.");
      await api("/api/password-reset/confirm", json({ token: state.resetToken, nova_senha: senha }));
      clearResetLink();
      setAuthMode("login");
      document.getElementById("auth-password").value = "";
      document.getElementById("auth-password2").value = "";
      authMessage("auth-info", "Senha alterada. Entre com a nova senha.");
      return;
    }
    // Token velho aqui faria o 401 de senha errada parecer sessão expirada.
    localStorage.removeItem(TOKEN_KEY);
    if (state.authMode === "register") {
      await api("/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nome: document.getElementById("auth-name").value, email, senha, invite: state.invite || null })
      });
      clearInviteLink();
    }
    const session = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, senha })
    });
    localStorage.setItem(TOKEN_KEY, session.access_token);
    paintUser(session.user);
    applyRole(session.user);
    if (!session.user.is_approved) { showPending(session.user); return; }
    showAuth(false);
    setTab("dashboard");
    await load();
  } catch (err) {
    authMessage("auth-info", "");
    authMessage("auth-error", err.message || "Não entrou.");
  }
});
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-logout]")) logout();
});

// Modal de confirmação customizado
let confirmResolve = null;
let promptResolve = null;
function showConfirm(title, message) {
  confirmOrigin = pressed;
  return new Promise((resolve) => {
    confirmResolve = resolve;
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-message").textContent = message;
    document.getElementById("confirm-modal").classList.remove("hidden");
  });
}

function showPrompt(title, defaultValue = "") {
  confirmOrigin = pressed;
  return new Promise((resolve) => {
    promptResolve = resolve;
    const titleEl = document.getElementById("confirm-title");
    const messageEl = document.getElementById("confirm-message");
    titleEl.textContent = title;
    messageEl.innerHTML = `<input id="prompt-input" type="text" autocomplete="off" data-lpignore="true" data-1p-ignore class="w-full rounded-2xl border border-neon/25 bg-ink px-3 py-3 text-sm outline-none focus:border-neon" value="${defaultValue.replace(/"/g, '&quot;')}">`;
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
  if (confirmOrigin) markPressed(confirmOrigin);  // o spinner vai no botão que abriu a confirmação
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
  // Link de convite do admin: /#invite=<token>. Abre o cadastro direto (sem login aberto neste navegador).
  const invite = !token() && location.hash.match(/^#invite=([\w\-]+)$/);
  if (invite) {
    await loadConfig(invite[1]);
    if (state.config.invite_valid) {
      state.invite = invite[1];
      setAuthMode("register");
      authMessage("auth-info", "Convite aceito. Crie sua conta; o acesso abre quando o administrador aprovar.");
    } else {
      clearInviteLink();
      setAuthMode("login");
      authMessage("auth-error", "Este convite não vale mais. Peça um novo ao administrador.");
    }
    showAuth(true);
    return;
  }
  const configReady = loadConfig();
  // Link do Telegram: /#reset=<token>. No hash para o token não ir ao servidor nem aos logs.
  const reset = location.hash.match(/^#reset=([\w\-.]+)$/);
  if (reset || !token()) {
    await configReady;
    setAuthMode("login");
    if (reset) {
      state.resetToken = reset[1];
      setAuthMode("reset");
    }
    showAuth(true);
    return;
  }
  if (tokenExpired()) {
    await configReady;
    sessionExpired();
    return;
  }
  // Logado: o painel aparece já com o último Dashboard salvo, e usuário, config e dados chegam em
  // paralelo (antes eram três esperas em fila).
  showAuth(false);
  setTab("dashboard");
  paintSnapshot();
  configReady.then(() => { if (!state.resetToken) setAuthMode("login"); });
  try {
    const loading = load();  // não rejeita: os erros ficam dentro de load()
    const user = await api("/api/me");
    paintUser(user);
    applyRole(user);
    if (!user.is_approved) { showPending(user); return; }
    roleSideLoads();
    await loading;
  } catch (err) {
    if (err.message === SESSION_EXPIRED || err.message === PENDING) return;
    localStorage.removeItem(TOKEN_KEY);
    showAuth(true);
  }
}
// Sininho: os alertas de views do robô em todos os níveis (viralizando, bombando, 100K, 1M). O id
// do alerta mais novo já visto fica no navegador; os mais novos contam no selo. Com a permissão, o
// navegador se inscreve no Web Push (/sw.js): o servidor avisa mesmo com o painel fechado, no
// celular e no Windows. Sem push (HTTP, navegador antigo), a própria aba avisa enquanto estiver aberta.
const VIRAL_SEEN_KEY = "viral_seen_id";
const VIRAL_NOTIFIED_KEY = "viral_notified_id";
const PUSH_ENDPOINT_KEY = "push_endpoint";
const VIRAL_POLL_MS = 60000;
const NOTIFY_ICON = "/static/img/icon-192.png";
const NOTIFY_BADGE = "/static/img/badge-96.png";
let viralItems = [];
function storedId(key) {
  try { return Number(localStorage.getItem(key)) || 0; } catch { return 0; }
}
function storeId(key, value) {
  try { localStorage.setItem(key, String(value)); } catch { /* aba anônima: segue sem lembrar */ }
}
function pushEndpoint() {
  try { return localStorage.getItem(PUSH_ENDPOINT_KEY) || ""; } catch { return ""; }
}
function setPushEndpoint(value) {
  try {
    if (value) localStorage.setItem(PUSH_ENDPOINT_KEY, value);
    else localStorage.removeItem(PUSH_ENDPOINT_KEY);
  } catch { /* aba anônima */ }
}
function notifySupported() {
  return "Notification" in window;
}
function isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}
function isIos() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}
let workerReady = null;
function registerWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return Promise.resolve(null);
  if (!workerReady) {
    workerReady = navigator.serviceWorker.register("/sw.js")
      .then(() => navigator.serviceWorker.ready)
      .catch(() => null);
  }
  return workerReady;
}
// Mostra uma notificação pelo service worker (o Android só aceita assim) ou, sem ele, direto pela aba.
async function showNotice(title, body, url, tag) {
  const options = { body, icon: NOTIFY_ICON, badge: NOTIFY_BADGE, tag, requireInteraction: true, data: { url: url || "/" } };
  const reg = await registerWorker();
  if (reg) return reg.showNotification(title, options);
  const note = new Notification(title, options);
  note.onclick = () => {
    window.focus();
    if (url) window.open(url, "_blank", "noopener");
    note.close();
  };
}
function keyBytes(base64url) {
  const text = atob(base64url.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - base64url.length % 4) % 4));
  return Uint8Array.from(text, (ch) => ch.charCodeAt(0));
}
function sameKey(buffer, bytes) {
  if (!buffer) return false;
  const have = new Uint8Array(buffer);
  return have.length === bytes.length && have.every((value, index) => value === bytes[index]);
}
// Inscreve este navegador no push do servidor. Devolve true quando ficou inscrito.
async function enablePush() {
  if (!notifySupported() || Notification.permission !== "granted" || !("PushManager" in window)) return false;
  const reg = await registerWorker();
  if (!reg) return false;
  try {
    const { public_key: serverKey } = await api("/api/push/key");
    const bytes = keyBytes(serverKey);
    let sub = await reg.pushManager.getSubscription();
    if (sub && !sameKey(sub.options.applicationServerKey, bytes)) {
      await sub.unsubscribe();  // chave do servidor trocou: a inscrição antiga não recebe mais nada
      sub = null;
    }
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytes });
    const json = sub.toJSON();
    await api("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys, user_agent: navigator.userAgent.slice(0, 300) })
    });
    setPushEndpoint(json.endpoint);
    return true;
  } catch {
    setPushEndpoint("");
    return false;
  }
}
function paintBellPermission() {
  const button = document.getElementById("bell-permission");
  const note = document.getElementById("bell-note");
  if (!notifySupported()) {
    button.classList.add("hidden");
    note.textContent = isIos() && !isStandalone()
      ? "No iPhone, instale o app (botão Instalar) para receber notificações."
      : "Este navegador não mostra notificações.";
    return;
  }
  const status = Notification.permission;
  button.classList.toggle("hidden", status !== "default");
  note.textContent = status === "granted"
    ? (pushEndpoint()
      ? "Notificações ligadas neste aparelho, mesmo com o painel fechado."
      : "Notificações ligadas enquanto o painel estiver aberto.")
    : status === "denied"
      ? "Notificações bloqueadas. Libere nas permissões do site (cadeado da barra de endereço)."
      : "Toque em Ativar notificações para receber o aviso na hora.";
}
async function askNotifications() {
  if (!notifySupported() || Notification.permission !== "default") return;
  try { await Notification.requestPermission(); } catch { /* navegador antigo: ignora */ }
  if (Notification.permission === "granted") {
    await enablePush();
    showNotice("Notificações ligadas", "Views, marcos, quedas e falhas chegam aqui. Escolha o que avisa em Configurações → Notificações.", "/", "xhenry-hello").catch(() => {});
  }
  paintBellPermission();
}
async function testNotification() {
  const button = document.getElementById("bell-test");
  if (!notifySupported()) {
    toast(isIos() && !isStandalone() ? "No iPhone, instale o app primeiro (botão Instalar)." : "Este navegador não mostra notificações.");
    return;
  }
  if (Notification.permission === "default") await askNotifications();
  if (Notification.permission !== "granted") {
    paintBellPermission();
    toast("Notificações bloqueadas. Libere nas permissões do site.");
    return;
  }
  button.disabled = true;
  try {
    await enablePush();  // garante que o servidor tem a inscrição atual (o navegador pode ter trocado)
    if (pushEndpoint()) {
      try {
        await api("/api/push/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: pushEndpoint() })
        });
        toast("Teste enviado. A notificação chega em alguns segundos.");
      } catch (error) {
        if (/inscri|expirou/i.test(error.message)) setPushEndpoint("");
        throw error;
      }
    } else {
      await showNotice("🔥 Teste do XHenry", "Notificações ligadas enquanto o painel estiver aberto.", "/", "xhenry-teste");
      toast("Teste mostrado. Sem push neste navegador: o aviso só chega com o painel aberto.");
    }
  } catch (error) {
    toast(error.message || "Não foi possível testar.");
  } finally {
    button.disabled = false;
    paintBellPermission();
  }
}
function viralWho(item) {
  if (!item.username) return item.display_name || (VIEW_LEVELS.includes(item.level) ? "Conta removida" : "XHenry");
  return (item.display_name ? item.display_name + " " : "") + "@" + item.username;
}
const VIEW_LEVELS = ["viral", "bombando", "100k", "1m"];
function viralTitle(item) {
  return (item.emoji || "🚀") + " " + (item.title || "Post viralizando");
}
function renderBell() {
  const unread = viralItems.filter((item) => item.id > storedId(VIRAL_SEEN_KEY)).length;
  const badge = document.getElementById("bell-badge");
  badge.textContent = unread > 9 ? "9+" : String(unread);
  badge.classList.toggle("hidden", !unread);
  document.getElementById("bell").classList.toggle("has-new", unread > 0);
  paintTabBadge("alerts", unread);
  const host = document.getElementById("bell-list");
  if (!viralItems.length) {
    host.replaceChildren(el("li", "bell-empty text-sm text-mute", "Nenhuma notificação ainda. Views, quedas, marcos e falhas aparecem aqui (ajuste em Configurações → Notificações)."));
    return;
  }
  const seen = storedId(VIRAL_SEEN_KEY);
  host.replaceChildren(...viralItems.map((item) => {
    const li = el("li", "bell-item lvl-" + (item.level || "viral") + (item.id > seen ? " is-new" : ""));
    const avatar = el("div", "post-avatar bell-avatar");
    const initial = item.username ? item.username.slice(0, 1).toUpperCase() : (item.emoji || "🔔");
    if (item.profile_image_url) {
      const img = el("img");
      img.src = item.profile_image_url;
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.addEventListener("error", () => { img.remove(); avatar.textContent = initial; });
      avatar.append(img);
    } else avatar.textContent = initial;
    const body = el("div", "min-w-0 flex-1");
    const top = el("div", "flex items-center justify-between gap-2");
    top.append(el("span", "bell-level", viralTitle(item)), el("span", "shrink-0 text-[11px] text-mute", agoText(item.sent_at)));
    body.append(top, el("p", "mt-1 truncate text-sm font-medium text-white", viralWho(item)));
    if (item.text) body.append(el("p", "bell-text mt-0.5 text-xs text-mute", item.text));
    const foot = el("div", "mt-2 flex items-center justify-between gap-2");
    foot.append(el("span", "bell-views font-mono text-xs", item.views === null ? "" : num(item.views) + " views"));
    if (item.url) {
      const link = el("a", "bell-link", "Abrir no X");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      foot.append(link);
    }
    body.append(foot);
    li.append(avatar, body);
    return li;
  }));
}
function notifyViral(items) {
  // Com push ligado quem avisa é o servidor; aqui só sem push, para não chegar em dobro.
  if (!notifySupported() || Notification.permission !== "granted" || pushEndpoint()) return;
  items.slice(0, 3).reverse().forEach((item) => {
    const lines = [viralWho(item) + (item.views === null ? "" : ", " + num(item.views) + " views")];
    if (item.text) lines.push(item.text);
    showNotice(viralTitle(item), lines.join("\n"), item.url, "viral-" + item.id).catch(() => {});
  });
}
function ringBell() {
  const bell = document.getElementById("bell");
  bell.classList.remove("ring-now");
  void bell.offsetWidth;  // reinicia a animação
  bell.classList.add("ring-now");
}
let viralBusy = false;
let pushSynced = false;
async function checkViral() {
  if (viralBusy || !token() || tokenExpired()) return;
  viralBusy = true;
  try {
    // Uma vez por abertura: renova a inscrição no servidor (ela pode ter sido apagada ou trocada).
    if (!pushSynced && notifySupported() && Notification.permission === "granted") {
      pushSynced = true;
      enablePush();
    }
    viralItems = await api("/api/viral?limit=20");
    const newest = viralItems.length ? viralItems[0].id : 0;
    const notified = storedId(VIRAL_NOTIFIED_KEY);
    if (!notified) storeId(VIRAL_NOTIFIED_KEY, newest || 1);  // primeira vez: não dispara o histórico
    else if (newest > notified) {
      const fresh = viralItems.filter((item) => item.id > notified);
      notifyViral(fresh);
      ringBell();
      storeId(VIRAL_NOTIFIED_KEY, newest);
    }
    renderBell();
  } catch (error) {
    if (error.message === SESSION_EXPIRED) return;
  } finally {
    viralBusy = false;
  }
}
function setBellOpen(open) {
  const panel = document.getElementById("bell-panel");
  panel.classList.toggle("open", open);
  document.getElementById("bell").setAttribute("aria-expanded", String(open));
  if (open) {
    paintBellPermission();
    if (viralItems.length) storeId(VIRAL_SEEN_KEY, Math.max(storedId(VIRAL_SEEN_KEY), viralItems[0].id));
    // Abriu: o selo some na hora; o destaque dos novos fica até fechar.
    document.getElementById("bell-badge").classList.add("hidden");
    paintTabBadge("alerts", 0);
    document.getElementById("bell").classList.remove("has-new");
  } else renderBell();
}
document.getElementById("bell").addEventListener("click", (event) => {
  event.stopPropagation();
  const open = !document.getElementById("bell-panel").classList.contains("open");
  setBellOpen(open);
  if (open) askNotifications();
});
document.getElementById("bell-permission").addEventListener("click", (event) => {
  event.stopPropagation();
  askNotifications();
});
document.getElementById("bell-test").addEventListener("click", (event) => {
  event.stopPropagation();
  testNotification();
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#bell-wrap")) setBellOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    setBellOpen(false);
    setMoreOpen(false);
    closePalette();
    document.getElementById("keep-help").classList.add("off");
    closeDrawer();
    document.getElementById("install-help").classList.add("off");
  }
});
setInterval(checkViral, VIRAL_POLL_MS);
registerWorker();

// Instalar como app: Chrome/Edge/Android dão o prompt nativo; no iPhone o caminho é pelo Safari.
let installPrompt = null;
function paintInstall() {
  const show = !isStandalone() && (installPrompt !== null || isIos());
  document.getElementById("install-btn").classList.toggle("hidden", !show);
}
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  paintInstall();
});
window.addEventListener("appinstalled", () => {
  installPrompt = null;
  paintInstall();
  toast("XHenry instalado. Abra pelo ícone e ative as notificações no sininho.");
});
document.getElementById("install-btn").addEventListener("click", async () => {
  if (installPrompt) {
    const prompt = installPrompt;
    installPrompt = null;
    prompt.prompt();
    await prompt.userChoice.catch(() => null);
    paintInstall();
  } else document.getElementById("install-help").classList.remove("off");
});
document.getElementById("install-help-close").addEventListener("click", () => {
  document.getElementById("install-help").classList.add("off");
});
paintInstall();

// Troca de tema: o tema novo se abre num círculo a partir do botão (View Transitions). Sem suporte ou
// com "reduzir movimento", troca na hora. As transições de cor ficam desligadas durante a troca, senão
// o retrato do tema novo sairia com as cores do antigo.
// Notificações depois de reiniciar: o aviso chega por push, mas no computador só com o navegador
// rodando (nem que seja escondido). Nenhum site liga isso sozinho, então o guia mostra o caminho certo
// para o navegador e o sistema de quem está abrindo.
function keepHelpContent() {
  const ua = navigator.userAgent;
  if (isIos()) return {
    intro: "No iPhone, com o app instalado, os avisos chegam mesmo com o celular recém-ligado, sem abrir nada.",
    steps: [
      ["Use o XHenry pelo ícone da Tela de Início, não pelo Safari (no Safari o iPhone não manda avisos)."],
      ["Ajustes → Notificações → XHenry: deixe Permitir Notificações ligado."],
      ["Se um dia parar de chegar, abra o app uma vez: ele se inscreve de novo sozinho."]
    ],
    note: "O modo Foco ou Não Perturbe segura os avisos até ser desligado."
  };
  if (/Android/i.test(ua)) return {
    intro: "No Android os avisos chegam mesmo com o celular recém-ligado, sem abrir o app.",
    steps: [
      ["Configurações → Apps → Chrome (e o app XHenry, se aparecer) → Notificações: ligadas."],
      ["Configurações → Apps → Chrome → Bateria: Sem restrições. A economia de bateria agressiva atrasa ou corta os avisos."],
      ["Se um dia parar de chegar, abra o app uma vez: ele se inscreve de novo sozinho."]
    ],
    note: "Avisos que chegarem com o celular desligado ficam guardados por até 6 horas."
  };
  const windows = /Windows/i.test(ua);
  const system = windows ? "Windows" : "computador";
  const notifySteps = windows ? [["No Windows: Configurações → Sistema → Notificações: deixe o navegador ligado e o Não Incomodar desligado."]] : [];
  const done = "Pronto: depois de reiniciar, os avisos chegam sem abrir nada. Os que chegarem com o PC desligado ficam guardados por até 6 horas.";
  if (/Edg\//.test(ua)) return {
    intro: "No computador o aviso só chega com o Edge rodando, nem que seja escondido. Estes dois ajustes fazem o Edge ligar junto com o " + system + ", em segundo plano:",
    steps: [
      ["Abra esta página do Edge e ligue Aumento de inicialização:", "edge://settings/system"],
      ["Na mesma página, ligue Continuar executando extensões e aplicativos em segundo plano quando o Microsoft Edge estiver fechado."],
      ...notifySteps
    ],
    note: done
  };
  if (/Chrome\//.test(ua)) return {
    intro: "No computador o aviso só chega com o Chrome rodando, nem que seja escondido. Com o app instalado, estes ajustes fazem ele abrir junto com o " + system + ":",
    steps: [
      ["Abra esta página do Chrome e ligue Continuar executando apps em segundo plano quando o Google Chrome estiver fechado:", "chrome://settings/system"],
      ["Abra a lista de apps, clique com o botão direito no XHenry e marque Iniciar app ao fazer login (se ele não aparecer, instale pelo botão Instalar do topo):", "chrome://apps"],
      ...notifySteps
    ],
    note: done
  };
  return {
    intro: "Os avisos só chegam com o navegador aberto ou rodando em segundo plano.",
    steps: [["Deixe o navegador abrir junto com o computador, nas opções de inicialização do sistema ou do navegador."], ...notifySteps],
    note: "No Edge e no Chrome há opções próprias para isso: abra este guia por eles para ver os passos."
  };
}
function openKeepHelp() {
  const content = keepHelpContent();
  document.getElementById("keep-help-intro").textContent = content.intro;
  document.getElementById("keep-help-note").textContent = content.note;
  document.getElementById("keep-help-steps").replaceChildren(...content.steps.map(([text, address], index) => {
    const li = el("li", "keep-step");
    li.append(el("span", "keep-num", String(index + 1)));
    const body = el("div", "min-w-0 flex-1");
    body.append(el("p", "text-sm text-white", text));
    if (address) {
      // edge:// e chrome:// não abrem por link de site: copia para colar na barra de endereço.
      const row = el("div", "keep-copy");
      const copy = el("button", "rounded-full border border-neon/40 px-3 py-1 text-xs", "Copiar");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(address); toast("✅ Copiado. Cole na barra de endereço e aperte Enter."); }
        catch { toast("Não deu para copiar. Digite " + address + " na barra de endereço.", "error"); }
      });
      row.append(el("code", "", address), copy);
      body.append(row);
    }
    li.append(body);
    return li;
  }));
  setBellOpen(false);
  document.getElementById("keep-help").classList.remove("off");
}
document.getElementById("keep-link").addEventListener("click", openKeepHelp);
document.getElementById("keep-help-close").addEventListener("click", () => document.getElementById("keep-help").classList.add("off"));
document.getElementById("keep-help").addEventListener("click", (event) => { if (event.target.id === "keep-help") event.target.classList.add("off"); });

document.getElementById("theme-btn").addEventListener("click", (event) => {
  const next = currentTheme() === "light" ? "dark" : "light";
  const root = document.documentElement;
  if (!document.startViewTransition || reduceMotion.matches) { setTheme(next, true); return; }
  const box = event.currentTarget.getBoundingClientRect();
  const x = box.left + box.width / 2;
  const y = box.top + box.height / 2;
  const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  root.classList.add("theme-switching");
  const transition = document.startViewTransition(() => setTheme(next, true));
  transition.ready.then(() => root.animate(
    { clipPath: ["circle(0px at " + x + "px " + y + "px)", "circle(" + radius + "px at " + x + "px " + y + "px)"] },
    { duration: 560, easing: "cubic-bezier(.65, 0, .25, 1)", pseudoElement: "::view-transition-new(root)" }
  )).catch(() => {});
  transition.finished.finally(() => root.classList.remove("theme-switching"));
});

// Vidro vivo: o laser da borda e o reflexo seguem o cursor (--mx/--my) e os cartões inclinam em 3D
// (--rx/--ry). Um ouvinte só para a página toda, pintado uma vez por quadro. Só com mouse; sem
// inclinação com "reduzir movimento". Formulários não se mexem (o laser deles acende ao digitar).
const GLASS_SELECTOR = ".glow, .kpi-card, #pane-dashboard article, #cloaker-results";
const TILT_SELECTOR = ".kpi-card, #pane-dashboard article.glow, .account-card, #folder-cards > .glow, #ops-accounts > .glow, #archived-accounts > .glow, .radar-card";
const finePointer = matchMedia("(hover: hover) and (pointer: fine)");
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");
let litCard = null;
let glassFrame = 0;
let glassPointer = null;
function releaseCard() {
  const card = litCard;
  if (!card) return;
  litCard = null;
  card.classList.remove("is-lit");
  card.style.setProperty("--rx", "0deg");
  card.style.setProperty("--ry", "0deg");
  // Depois de voltar ao lugar, tira o 3D (cartão parado não precisa de camada própria).
  setTimeout(() => { if (card !== litCard) card.classList.remove("tilt"); }, 650);
}
function paintGlass() {
  glassFrame = 0;
  const event = glassPointer;
  const found = event.target instanceof Element ? event.target.closest(GLASS_SELECTOR) : null;
  const card = found && found.tagName !== "FORM" ? found : null;
  if (card !== litCard) {
    releaseCard();
    if (!card) return;
    litCard = card;
    card.classList.add("is-lit");
    if (!reduceMotion.matches && card.matches(TILT_SELECTOR)) card.classList.add("tilt");
  }
  if (!card) return;
  const box = card.getBoundingClientRect();
  const x = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
  const y = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height));
  card.style.setProperty("--mx", (x * 100).toFixed(1) + "%");
  card.style.setProperty("--my", (y * 100).toFixed(1) + "%");
  if (card.classList.contains("tilt")) {
    const max = box.width > 640 ? 2 : box.width > 360 ? 4 : 7;  // cartão grande inclina menos
    card.style.setProperty("--rx", ((0.5 - y) * max * 2).toFixed(2) + "deg");
    card.style.setProperty("--ry", ((x - 0.5) * max * 2).toFixed(2) + "deg");
  }
}
document.addEventListener("pointermove", (event) => {
  if (event.pointerType !== "mouse" || !finePointer.matches) return;
  glassPointer = event;
  if (!glassFrame) glassFrame = requestAnimationFrame(paintGlass);
}, { passive: true });
document.addEventListener("mouseout", (event) => { if (!event.relatedTarget) releaseCard(); });

setPeriod("today");
setAuthMode("login");
// Auto-refresh: a cada 60s, só na aba Dashboard, com a página visível e logado.
// O dashboard é público e nunca dá 401, então a expiração do token é conferida aqui.
const AUTO_REFRESH_MS = 60000;
function autoRefresh() {
  if (tokenExpired()) {
    sessionExpired();
    return;
  }
  if (state.tab === "dashboard" && token() && !document.hidden) load({ silent: true });
}
setInterval(autoRefresh, AUTO_REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) autoRefresh(); });

// ---------- ícones e telas vazias ----------
// Mesmo traço dos ícones das abas (index.html); usados nas telas vazias e na busca rápida.
const ICONS = {
  radar: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12 18.2 5.8"/><circle cx="12" cy="12" r="1.3"/>',
  dashboard: '<rect x="3.5" y="3.5" width="7" height="9" rx="2"/><rect x="13.5" y="3.5" width="7" height="5" rx="2"/><rect x="13.5" y="11.5" width="7" height="9" rx="2"/><rect x="3.5" y="15.5" width="7" height="5" rx="2"/>',
  ops: '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.2h7a2 2 0 0 1 2 2v8.3a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z"/>',
  archived: '<rect x="3" y="4" width="18" height="4.5" rx="1.5"/><path d="M5 8.5v9.5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5"/><path d="M10 12.5h4"/>',
  post: '<path d="M12 3.2 19 6v5.6c0 4.3-2.9 7.6-7 9.2-4.1-1.6-7-4.9-7-9.2V6Z"/><path d="m8.8 12 2.2 2.2 4.2-4.4"/>',
  cloaker: '<path d="m12 3.5 8.5 4.5-8.5 4.5L3.5 8Z"/><path d="m3.5 12 8.5 4.5 8.5-4.5"/><path d="m3.5 16 8.5 4.5 8.5-4.5"/>',
  settings: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2.2"/><circle cx="8" cy="17" r="2.2"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  account: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
  users: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19.5a5.5 5.5 0 0 1 11 0"/><circle cx="17" cy="9.5" r="2.4"/><path d="M15.5 14.6a4.6 4.6 0 0 1 5 4.9"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 8.5-8.5M16 7l2.5 2.5M14 9l2 2"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/>',
  theme: '<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16Z" fill="currentColor"/>'
};
function icon(name, className) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", className || "nav-ico");
  svg.innerHTML = ICONS[name] || ICONS.search;  // marcação fixa daqui, nunca texto de fora
  return svg;
}
// Tela vazia com direção: o que é, por que está vazia e o próximo passo.
function emptyState(iconName, title, text, actionLabel, onAction) {
  const box = el("div", "empty-state col-span-full");
  box.append(icon(iconName, "empty-ico"), el("p", "font-display text-lg text-white", title), el("p", "max-w-md text-sm text-mute", text));
  if (actionLabel) {
    const button = el("button", "mt-1 rounded-full bg-neon px-4 py-2 text-sm font-medium text-ink", actionLabel);
    button.type = "button";
    button.addEventListener("click", onAction);
    box.append(button);
  }
  return box;
}

// ---------- ao vivo + destaque do que mudou ----------
let liveAt = 0;
let liveOk = true;
function paintLive() {
  const pill = document.getElementById("live-pill");
  if (!pill || !liveAt) return;
  const secs = Math.max(0, Math.round((Date.now() - liveAt) / 1000));
  const ago = secs < 15 ? "agora" : secs < 60 ? "há " + secs + " s" : "há " + Math.round(secs / 60) + " min";
  const streaming = liveSource && liveSource.readyState === 1;
  pill.classList.toggle("is-off", !liveOk && !streaming);
  pill.classList.toggle("is-stale", !streaming && liveOk && secs > 180);
  // Canal aberto: tudo chega na hora, então "ao vivo"; sem ele, quanto tempo faz a última leitura.
  document.getElementById("live-text").textContent = streaming ? "ao vivo" : liveOk ? "atualizado " + ago : "reconectando…";
  pill.title = "Última atualização " + ago;
}
function markLive(ok) {
  liveOk = ok;
  if (ok) liveAt = Date.now();
  paintLive();
}
setInterval(paintLive, 10000);
// Painel ao vivo: o servidor avisa (/api/live, app/live.py) quando o robô grava dados ou chega um
// alerta, e só a parte que mudou é recarregada, sem F5. O EventSource reconecta sozinho;
// o auto-refresh de 60 s continua como reserva.
let liveSource = null;
let liveStamp = null;
let dashboardDirty = false;
function startLive() {
  if (liveSource || !window.EventSource) return;
  liveSource = new EventSource("/api/live?token=" + encodeURIComponent(token()));
  liveSource.onopen = () => markLive(true);
  liveSource.onerror = () => { liveOk = false; paintLive(); };
  liveSource.onmessage = (event) => {
    let stamp;
    try { stamp = JSON.parse(event.data); } catch { return; }
    const before = liveStamp;
    liveStamp = stamp;
    markLive(true);
    if (!before || !token()) return;
    if (stamp.data !== before.data) {
      roleSideLoads();
      if (state.tab === "dashboard" && !document.hidden) load({ silent: true });
      else dashboardDirty = true;  // recarrega quando o Dashboard voltar à tela
    }
    if (stamp.alerts !== before.alerts) { checkViral(); loadAlerts(); }
    if (stamp.radar !== before.radar && state.tab === "radar") loadRadar({ silent: true });
  };
}
function flash(node) {
  if (!node) return;
  node.classList.remove("just-changed");
  void node.offsetWidth;
  node.classList.add("just-changed");
  setTimeout(() => node.classList.remove("just-changed"), 1800);
}
function flashChanges(before, data) {
  document.querySelectorAll("#kpis [data-changed]").forEach((node) => flash(node.closest(".kpi-card")));
  const old = new Map((before.accounts || []).map((a) => [a.id, a.views]));
  (data.accounts || []).forEach((account) => {
    if (old.has(account.id) && old.get(account.id) !== account.views) {
      flash(document.querySelector('#accounts [data-account-id="' + account.id + '"]'));
    }
  });
}

// ---------- valor de cada dia na linha de views ----------
function sparkHover(card, series) {
  const svg = card.querySelector(".spark");
  if (!svg || series.length < 2) return;
  const ns = "http://www.w3.org/2000/svg";
  const marker = document.createElementNS(ns, "line");
  marker.setAttribute("class", "spark-marker");
  marker.setAttribute("y1", "0");
  marker.setAttribute("y2", "32");
  svg.append(marker);
  const tip = el("p", "spark-tip");
  card.append(tip);
  svg.addEventListener("pointermove", (event) => {
    const box = svg.getBoundingClientRect();
    const index = Math.round(Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)) * (series.length - 1));
    const x = index / (series.length - 1) * 100;
    marker.setAttribute("x1", x.toFixed(2));
    marker.setAttribute("x2", x.toFixed(2));
    const day = series[index].date;
    tip.textContent = day.slice(8, 10) + "/" + day.slice(5, 7) + ": " + num(series[index].views) + " views";
    tip.style.left = Math.min(Math.max(x, 12), 88) + "%";
    card.classList.add("spark-on");
  });
  svg.addEventListener("pointerleave", () => card.classList.remove("spark-on"));
}

// ---------- gaveta da conta ----------
let drawerFor = null;
let drawerReturn = null;
function drawerOpen() { return document.getElementById("drawer").classList.contains("open"); }
function closeDrawer() {
  const drawer = document.getElementById("drawer");
  if (!drawer.classList.contains("open")) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("drawer-lock");
  drawerFor = null;
  setTimeout(() => { if (!drawerOpen() && drawerChart) { drawerChart.destroy(); drawerChart = null; } }, 400);
  if (drawerReturn && drawerReturn.isConnected) drawerReturn.focus();
}
document.getElementById("drawer").addEventListener("click", (event) => {
  if (event.target.closest("[data-close-drawer]")) closeDrawer();
});
function drawerSection(title, ...children) {
  const box = el("div", "drawer-section");
  box.append(el("h3", "font-display text-base text-white", title), ...children);
  return box;
}
async function openDrawer(accountId) {
  const account = ((state.data && state.data.accounts) || []).find((a) => a.id === accountId);
  if (!account) return;
  const drawer = document.getElementById("drawer");
  if (!drawerOpen()) drawerReturn = document.activeElement;
  drawerFor = accountId;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-lock");
  renderDrawer(account, null);
  document.getElementById("drawer-close").focus();
  try {
    const detail = await api("/api/accounts/" + accountId + "/detail?period=" + encodeURIComponent(state.period));
    if (drawerFor === accountId) renderDrawer(detail.account, detail);
  } catch (error) {
    if (drawerFor !== accountId) return;
    document.getElementById("drawer-extra").replaceChildren(el("p", "text-sm text-mute", "Não deu para carregar os detalhes: " + (error.message || "erro") + "."));
  }
}
function renderDrawer(account, detail) {
  const body = document.getElementById("drawer-body");
  const color = folderColor(account.folder_id);
  const head = el("div", "drawer-head");
  const who = el("p", "text-sm text-mute", "@" + account.username + " · " + account.folder_name);
  if (color) { who.prepend(el("span", "folder-dot")); head.style.setProperty("--folder", color); }
  const title = el("h2", "font-display text-2xl text-white", account.display_name || "@" + account.username);
  title.id = "drawer-title";
  const actions = el("div", "mt-3 flex flex-wrap gap-2");
  const check = el("button", "rounded-full bg-neon px-4 py-1.5 text-sm font-medium text-ink", "Verificar agora");
  check.type = "button";
  check.addEventListener("click", async () => {
    check.disabled = true;
    check.textContent = "Verificando…";
    await verify(account);
    if (drawerFor === account.id) openDrawer(account.id);
  });
  const open = el("a", "rounded-full border border-neon/40 px-4 py-1.5 text-sm", "Abrir no X");
  open.href = "https://x.com/" + encodeURIComponent(account.username);
  open.target = "_blank";
  open.rel = "noopener";
  actions.append(check, open);
  head.append(el("span", "rounded-full border px-2.5 py-0.5 text-xs font-medium st-" + (account.status || "none"), account.status_label), title, who, actions);
  const stats = el("dl", "drawer-stats");
  [["Views", account.views], ["Curtidas", account.likes], ["Retweets", account.retweets], ["Seguidores", account.followers]].forEach(([label, value]) => {
    const item = el("div", "drawer-stat");
    item.append(el("dt", "text-[11px] text-mute", label), el("dd", "font-mono text-lg text-white", num(value)));
    stats.append(item);
  });
  const extra = el("div", "space-y-5");
  extra.id = "drawer-extra";
  if (!detail) {
    extra.append(el("div", "skel", ""), el("div", "skel-stack"));
    extra.firstChild.style.height = "12rem";
    extra.lastChild.append(...[1, 2, 3, 4].map(() => el("i", "skel skel-line")));
  } else {
    const canvasBox = el("div", "h-48");
    const canvas = document.createElement("canvas");
    canvasBox.append(canvas);
    const range = detail.start === detail.end ? "últimos 14 dias" : "do período";
    extra.append(drawerSection("Views por dia", el("p", "-mt-1 mb-2 text-xs text-mute", "Views dos posts " + range), canvasBox));
    const posts = el("ol", "drawer-list");
    detail.top_posts.forEach((post) => {
      const li = el("li", "drawer-post");
      const link = el("a", "drawer-post-text text-sm text-white", post.text || "(sem texto)");
      link.href = post.url;
      link.target = "_blank";
      link.rel = "noopener";
      li.append(link, el("span", "font-mono text-sm text-white", num(post.views)), el("span", "text-[11px] text-mute", num(post.likes) + " ♥"));
      posts.append(li);
    });
    extra.append(drawerSection("Melhores posts", detail.top_posts.length ? posts : el("p", "text-sm text-mute", "Nenhum post lido no período.")));
    const checks = el("ol", "drawer-list");
    detail.checks.forEach((item) => {
      const li = el("li", "drawer-check");
      li.append(
        el("span", "shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium st-" + item.status, item.label),
        el("span", "text-xs text-mute", stampBR(item.checked_at)),
        el("p", "w-full text-xs text-mute", item.detail || "")
      );
      checks.append(li);
    });
    extra.append(drawerSection("Histórico de status", detail.checks.length ? checks : el("p", "text-sm text-mute", "Nenhuma verificação ainda. Use Verificar agora.")));
    if (window.Chart) {
      if (drawerChart) drawerChart.destroy();
      const labels = detail.series.map((row) => row.date.slice(8, 10) + "/" + row.date.slice(5, 7));
      const datasets = [{
        type: "line",
        label: "Views",
        legendColor: "#ff2ec8",
        data: detail.series.map((row) => row.views),
        borderColor: "#ff2ec8",
        backgroundColor: "rgba(255,46,200,.14)",
        fill: true,
        tension: .35,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2,
        yAxisID: "y"
      }];
      if (detail.series.some((row) => row.followers !== null)) datasets.push({
        type: "line",
        label: "Seguidores",
        legendColor: "#67dcff",
        data: detail.series.map((row) => row.followers),
        borderColor: "#67dcff",
        borderDash: [5, 4],
        borderWidth: 1.5,
        pointRadius: 0,
        spanGaps: true,
        yAxisID: "y1"
      });
      drawerChart = new Chart(canvas, {
        type: "line",
        data: { labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { grid: { display: false }, ticks: { maxTicksLimit: 7 } },
            y: { beginAtZero: true, ticks: { precision: 0, maxTicksLimit: 5 } },
            y1: { position: "right", grid: { display: false }, display: datasets.length > 1, ticks: { precision: 0, maxTicksLimit: 5 } }
          }
        }
      });
    }
  }
  body.replaceChildren(head, stats, extra);
}

// ---------- Radar de trend ----------
// O que está em alta no Brasil (X, Google Trends, Google Notícias, Wikipédia), datas sazonais e o que pode
// virar trend. A aba só lê /api/radar; as fontes são lidas pelo servidor. Todo texto das fontes entra por
// textContent (el), nunca como HTML.
const RADAR_SOURCES = { x: "X Brasil", google: "Google", news: "Notícias", wiki: "Wikipédia" };
const RADAR_SHORT = { x: "X", google: "Google", news: "Notícias", wiki: "Wiki" };
const RADAR_CATEGORIES = ["Futebol", "Política", "Famosos/TV", "Música", "Economia", "Outros"];
const RADAR_DIRECTIONS = { novo: ["Novo", "is-new"], subindo: ["▲ Subindo", "is-up"], caindo: ["▼ Caindo", "is-down"] };
let radarData = null;
let radarFilter = "";
let radarRetry = 0;
let radarAll = false;  // lista inteira ou só os 20 primeiros
// Região (Brasil ou Internacional) e o filtro "Bom pra engajar" ficam lembrados neste navegador.
let radarRegion = "br";
let radarEngage = true;
let radarHookAt = 0;  // qual ideia do gancho está na tela ("Outra ideia" gira)
try {
  radarRegion = localStorage.getItem("radar_region") === "world" ? "world" : "br";
  radarEngage = localStorage.getItem("radar_engage") !== "off";
} catch { /* sem storage */ }
const ENGAGE_MIN = 55;
function radarRemember(key, value) { try { localStorage.setItem(key, value); } catch { /* sem storage */ } }
const RADAR_FIRST = 20;
async function loadRadar({ silent = false } = {}) {
  try {
    const data = await api("/api/radar?region=" + radarRegion);
    paintRadar(data);
    // O servidor começou uma leitura (dados velhos ou nenhum): olha de novo em 20 s, até 3 vezes.
    if (data.refreshing && radarRetry < 3) {
      radarRetry += 1;
      setTimeout(() => { if (state.tab === "radar") loadRadar({ silent: true }); }, 20000);
    } else if (!data.refreshing) radarRetry = 0;
  } catch (error) {
    if (error.message === SESSION_EXPIRED || error.message === PENDING) return;
    if (!silent) toast(error.message || "Não leu o radar.");
    if (!radarData) document.getElementById("radar-topics").replaceChildren(el("li", "text-sm text-mute", "Não deu para ler o radar agora."));
  }
}
function radarWhen(item) {
  if (item.kind === "previsao") return "Pode virar trend";
  if (item.in_days === 0) return "Hoje";
  if (item.in_days === 1) return "Amanhã";
  return "Em " + item.in_days + " dias · " + item.date.slice(8, 10) + "/" + item.date.slice(5, 7);
}
function paintRadarSources(data) {
  const host = document.getElementById("radar-sources");
  host.replaceChildren(...Object.entries(RADAR_SOURCES).map(([key, label]) => {
    const info = data.sources[key] || {};
    const status = info.ok === true ? "ok" : info.ok === false ? "bad" : "wait";
    const chip = el("span", "radar-src is-" + status);
    chip.title = info.ok === false ? label + " indisponível: " + (info.error || "erro") : info.at ? label + " lido " + agoText(info.at) : label + ": ainda sem leitura";
    chip.append(el("i", "radar-dot"), el("span", "", RADAR_SHORT[key]));
    return chip;
  }));
}
function radarUpcomingCard(item) {
  const card = el("button", "radar-card" + (item.kind === "previsao" ? " is-hot" : ""));
  card.type = "button";
  card.addEventListener("click", (event) => (item.norm ? openTopic(item.norm, event.currentTarget) : openDateIdeas(item, event.currentTarget)));
  card.append(el("span", "radar-when", radarWhen(item)), el("span", "radar-name font-display", item.name), el("span", "radar-tip", item.tip || ""));
  return card;
}
// Copiar e Postar: o texto vai para a área de transferência ou para a tela de post do X, já escrito.
async function copyIdea(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast("Copiado. Cole no X.", "ok");
}
function ideaActions(text) {
  const box = el("span", "idea-actions");
  const copy = el("button", "idea-btn", "Copiar");
  copy.type = "button";
  copy.addEventListener("click", (event) => { event.stopPropagation(); copyIdea(text); });
  const post = el("a", "idea-btn is-post", "Postar");
  post.href = "https://x.com/intent/post?text=" + encodeURIComponent(text);
  post.target = "_blank";
  post.rel = "noopener noreferrer";
  post.title = "Abre o X com o texto pronto";
  post.addEventListener("click", (event) => event.stopPropagation());
  box.append(copy, post);
  return box;
}
function engageable(topic) { return !topic.blocked && topic.engage >= ENGAGE_MIN; }
function paintRadarHook() {
  const host = document.getElementById("radar-hook");
  const best = (radarData.topics || []).filter((t) => engageable(t) && t.ideas.length)
    .sort((a, b) => (b.engage + b.heat) - (a.engage + a.heat))[0];
  host.hidden = !best;
  if (!best) return;
  const idea = best.ideas[radarHookAt % best.ideas.length];
  const top = el("div", "radar-hook-top");
  const name = el("button", "radar-hook-topic", best.title);
  name.type = "button";
  name.title = "Abrir o assunto";
  name.addEventListener("click", (event) => openTopic(best.norm, event.currentTarget));
  top.append(el("span", "radar-hook-label", "Gancho do momento"), name, el("span", "radar-cat", best.category));
  const line = el("p", "radar-hook-line font-display", idea);
  const actions = ideaActions(idea);
  const next = el("button", "idea-btn", "Outra ideia");
  next.type = "button";
  next.addEventListener("click", () => { radarHookAt += 1; paintRadarHook(); });
  actions.append(next);
  host.replaceChildren(top, line, actions);
}
function radarTopicRow(topic, index) {
  const li = el("li", "radar-row");
  const row = el("button", "radar-topic" + (index < 3 ? " is-top" : ""));
  row.type = "button";
  row.addEventListener("click", (event) => openTopic(topic.norm, event.currentTarget));
  const main = el("span", "radar-main");
  const title = el("span", "radar-title", topic.title);
  const meta = el("span", "radar-meta");
  topic.sources.forEach((name) => meta.append(el("span", "src-badge src-" + name, RADAR_SHORT[name] + (topic.ranks[name] ? " #" + topic.ranks[name] : ""))));
  const dir = RADAR_DIRECTIONS[topic.direction];
  if (dir) meta.append(el("span", "radar-dir " + dir[1], dir[0]));
  if (topic.hours_x > 1) meta.append(el("span", "radar-note", "em alta há " + topic.hours_x + " h"));
  if (topic.volume) meta.append(el("span", "radar-note", topic.volume + " buscas"));
  main.append(title, meta);
  const heat = el("span", "radar-heat");
  heat.title = "Calor " + topic.heat + " de 100";
  const bar = el("i", "");
  bar.style.width = Math.max(4, topic.heat) + "%";
  heat.append(bar);
  row.append(el("span", "radar-pos font-mono", String(index + 1)), main, el("span", "radar-cat", topic.category), heat);
  li.append(row);
  if (topic.ideas && topic.ideas.length) {
    const hook = el("div", "radar-hookline");
    hook.append(el("p", "radar-hookline-text", topic.ideas[0]), ideaActions(topic.ideas[0]));
    li.append(hook);
  }
  return li;
}
function paintRadarTopics() {
  const host = document.getElementById("radar-topics");
  let topics = (radarData.topics || []).filter((topic) => !radarFilter || topic.category === radarFilter);
  // "Bom pra engajar": só o que rende post de texto, o mais engajável primeiro; assunto sensível nunca aparece.
  if (radarEngage) topics = topics.filter(engageable).sort((a, b) => b.engage - a.engage);
  const filters = document.getElementById("radar-filters");
  const present = RADAR_CATEGORIES.filter((name) => radarData.topics.some((topic) => topic.category === name));
  filters.replaceChildren(...[""].concat(present).map((name) => {
    const chip = el("button", "chip" + (radarFilter === name ? " on" : ""), name || "Tudo");
    chip.type = "button";
    chip.addEventListener("click", () => { radarFilter = name; radarAll = false; paintRadarTopics(); });
    return chip;
  }));
  if (!radarData.topics.length) {
    host.replaceChildren(el("li", "", ""));
    host.firstChild.append(emptyState("radar", "Radar ainda vazio", "As fontes ainda não foram lidas. A primeira leitura sai em instantes; ou atualize agora.", "Atualizar agora", refreshRadar));
    return;
  }
  const shown = radarAll ? topics : topics.slice(0, RADAR_FIRST);
  const empty = radarEngage ? "Nada bom pra engajar" + (radarFilter ? " em " + radarFilter : "") + " agora. Desligue o filtro para ver tudo." : "Nada em " + radarFilter + " agora.";
  const rows = shown.length ? shown.map(radarTopicRow) : [el("li", "text-sm text-mute", empty)];
  if (shown.length < topics.length) {
    const more = el("button", "mt-1 w-full rounded-2xl border border-neon/40 px-4 py-2 text-sm", "Ver todos (" + topics.length + ")");
    more.type = "button";
    more.addEventListener("click", () => { radarAll = true; paintRadarTopics(); });
    const li = el("li", "");
    li.append(more);
    rows.push(li);
  }
  host.replaceChildren(...rows);
}
function paintRadarControls() {
  document.getElementById("radar-heading").textContent = radarRegion === "world" ? "Agora no mundo" : "Agora no Brasil";
  document.querySelectorAll("[data-radar-region]").forEach((button) => button.setAttribute("aria-checked", String(button.dataset.radarRegion === radarRegion)));
  document.getElementById("radar-engage").checked = radarEngage;
  document.getElementById("radar-terms-card").hidden = radarRegion === "world";
}
function paintRadar(data) {
  radarData = data;
  paintRadarControls();
  paintRadarHook();
  document.getElementById("radar-updated").textContent = data.updated_at
    ? "Atualizado " + agoText(data.updated_at) + (data.refreshing ? " · lendo as fontes…" : "")
    : data.refreshing ? "Lendo as fontes pela primeira vez…" : "Ainda sem leitura";
  paintRadarSources(data);
  const upcoming = document.getElementById("radar-upcoming");
  upcoming.replaceChildren(...(data.upcoming.length ? data.upcoming.map(radarUpcomingCard) : [el("p", "text-sm text-mute", "Nenhuma data marcada nos próximos 14 dias.")]));
  paintRadarTopics();
  paintRadarTerms(data.my_terms || []);
  const headlines = document.getElementById("radar-headlines");
  headlines.replaceChildren(...(data.headlines.length ? data.headlines.map((item) => {
    const li = el("li", "");
    const link = el("a", "radar-headline", item.title);
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    li.append(link, el("span", "text-[11px] text-mute", item.source || ""));
    return li;
  }) : [el("li", "text-sm text-mute", "Sem manchetes soltas agora.")]));
}
async function refreshRadar() {
  try {
    paintRadar(await api("/api/radar/refresh?region=" + radarRegion, { method: "POST" }));
    toast("Radar atualizado.");
  } catch (error) { toast(error.message || "Não atualizou o radar."); }
}
document.getElementById("radar-refresh").addEventListener("click", refreshRadar);
document.querySelectorAll("[data-radar-region]").forEach((button) => button.addEventListener("click", () => {
  if (radarRegion === button.dataset.radarRegion) return;
  radarRegion = button.dataset.radarRegion;
  radarRemember("radar_region", radarRegion);
  radarFilter = "";
  radarAll = false;
  radarHookAt = 0;
  paintRadarControls();
  loadRadar();
}));
document.getElementById("radar-engage").addEventListener("change", (event) => {
  radarEngage = event.target.checked;
  radarRemember("radar_engage", radarEngage ? "on" : "off");
  radarAll = false;
  if (radarData) paintRadarTopics();
});
// Meus termos: o radar avisa (kind=radar: push, sininho, histórico) quando um deles aparece.
function paintRadarTerms(terms) {
  const host = document.getElementById("radar-terms");
  if (!terms.length) {
    host.replaceChildren(el("li", "text-sm text-mute", "Nenhum termo ainda. Adicione o que você quer pegar primeiro: time, artista, político…"));
    return;
  }
  host.replaceChildren(...terms.map((term) => {
    const li = el("li", "radar-term" + (term.hot ? " is-hot" : ""));
    const name = el(term.topic ? "button" : "span", "radar-term-name", term.term);
    if (term.topic) {
      name.type = "button";
      name.title = "Em alta agora: abrir o assunto";
      name.addEventListener("click", (event) => openTopic(term.topic, event.currentTarget));
    }
    li.append(el("i", "radar-dot"), name);
    if (term.hot) li.append(el("span", "radar-dir is-up", "em alta"));
    const remove = el("button", "radar-term-x", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "Parar de vigiar " + term.term);
    remove.addEventListener("click", async () => {
      try {
        await api("/api/radar/terms/" + term.id, { method: "DELETE" });
        radarData.my_terms = radarData.my_terms.filter((item) => item.id !== term.id);
        paintRadarTerms(radarData.my_terms);
        toast("Termo removido.");
      } catch (error) { toast(error.message || "Não removeu o termo."); }
    });
    li.append(remove);
    return li;
  }));
}
async function addRadarTerm(term) {
  try {
    await api("/api/radar/terms", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ term }) });
    radarData.my_terms = await api("/api/radar/terms");
    paintRadarTerms(radarData.my_terms);
    toast("Pronto: você recebe um aviso quando \"" + term + "\" estiver em alta.");
    return true;
  } catch (error) {
    toast(error.message || "Não salvou o termo.");
    return false;
  }
}
document.getElementById("radar-term-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("radar-term-input");
  const term = input.value.trim();
  if (term && radarData && await addRadarTerm(term)) input.value = "";
});
// Posts que estão engajando: só no clique (gasta Apify de quem clicou; 30 min de cache para todos).
async function loadEngagingPosts(term, box) {
  const button = box.querySelector("button");
  button.disabled = true;
  button.textContent = "Buscando no X…";
  try {
    const data = await api("/api/radar/posts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ term }) });
    const list = el("ol", "drawer-list");
    data.posts.forEach((post) => {
      const li = el("li", "radar-post");
      const link = el("a", "radar-headline", post.text || "(sem texto)");
      link.href = post.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const meta = el("span", "radar-meta");
      meta.append(el("span", "radar-note", "@" + post.author), el("span", "font-mono text-xs text-white", num(post.views) + " views"),
        el("span", "radar-note", num(post.likes) + " ♥ · " + num(post.retweets) + " RT"));
      li.append(link, meta);
      list.append(li);
    });
    const when = el("p", "text-[11px] text-mute", (data.cached ? "Guardado " : "Buscado ") + agoText(data.fetched_at) + (data.cached ? " (sem gastar créditos)" : ""));
    box.replaceChildren(data.posts.length ? list : el("p", "text-sm text-mute", "Nenhum post forte em português sobre isso agora."), when);
  } catch (error) {
    button.disabled = false;
    button.textContent = "Tentar de novo";
    toast(error.message || "Não buscou os posts.", "error");
  }
}
function ideaList(lines) {
  const list = el("ol", "idea-list");
  lines.forEach((line) => {
    const li = el("li", "idea-item");
    li.append(el("p", "idea-text", line), ideaActions(line));
    list.append(li);
  });
  return list;
}
function ideasBox(topic) {
  const box = el("div", "space-y-3");
  if (topic.blocked) {
    box.append(el("p", "text-sm text-mute", "Assunto sensível (morte, tragédia, crime, doença ou criança): sem ideias de post."));
    return box;
  }
  const list = ideaList(topic.ideas || []);
  const grok = el("button", "w-full rounded-2xl border border-neon/40 px-4 py-2.5 text-sm", "Mais ideias com Grok");
  grok.type = "button";
  grok.addEventListener("click", async () => {
    grok.disabled = true;
    grok.textContent = "Pedindo ao Grok…";
    try {
      const data = await api("/api/radar/ideas/grok", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ norm: topic.norm, region: radarRegion }) });
      list.prepend(...ideaList(data.ideas).children);
      grok.textContent = "Mais ideias com Grok";
    } catch (error) {
      toast(error.message || "O Grok não respondeu.", "error");
      grok.textContent = "Tentar de novo";
    }
    grok.disabled = false;
  });
  box.append(list, grok, el("p", "text-[11px] text-mute", "O Grok usa a sua chave da xAI. As frases são sugestivas, nunca explícitas, e nunca sexualizam uma pessoa real."));
  return box;
}
async function openDateIdeas(item, origin) {
  const drawer = document.getElementById("drawer");
  if (!drawerOpen()) drawerReturn = origin || document.activeElement;
  const key = "radar-data:" + item.name;
  drawerFor = key;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-lock");
  document.getElementById("drawer-close").focus();
  const head = el("div", "drawer-head");
  const title = el("h2", "font-display text-2xl text-white", item.name);
  title.id = "drawer-title";
  head.append(el("span", "radar-cat", radarWhen(item)), title, el("p", "text-sm text-mute", item.tip || ""));
  const body = el("div", "space-y-5");
  body.append(drawerSection("Ideias de post", el("p", "text-sm text-mute", "Montando as ideias…")));
  document.getElementById("drawer-body").replaceChildren(head, body);
  try {
    const data = await api("/api/radar/ideas?norm=" + encodeURIComponent(item.name));
    if (drawerFor === key) body.replaceChildren(drawerSection("Ideias de post", ideasBox({ ...data, norm: data.norm })));
  } catch (error) {
    if (drawerFor === key) body.replaceChildren(el("p", "text-sm text-mute", error.message || "Não deu para montar as ideias."));
  }
}
// Gaveta do assunto: a mesma gaveta das contas, com as posições no X nas últimas 24 h e as notícias.
async function openTopic(norm, origin) {
  const drawer = document.getElementById("drawer");
  if (!drawerOpen()) drawerReturn = origin || document.activeElement;
  const key = "radar:" + norm;
  drawerFor = key;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-lock");
  const loading = el("div", "skel-stack");
  loading.append(...[1, 2, 3, 4, 5].map(() => el("i", "skel skel-line")));
  document.getElementById("drawer-body").replaceChildren(loading);
  document.getElementById("drawer-close").focus();
  try {
    const topic = await api("/api/radar/topic?region=" + radarRegion + "&norm=" + encodeURIComponent(norm));
    if (drawerFor === key) renderTopic(topic);
  } catch (error) {
    if (drawerFor === key) document.getElementById("drawer-body").replaceChildren(el("p", "text-sm text-mute", error.message || "Não deu para abrir o assunto."));
  }
}
function linkButton(label, href, primary) {
  const link = el("a", primary ? "rounded-full bg-neon px-4 py-1.5 text-sm font-medium text-ink" : "rounded-full border border-neon/40 px-4 py-1.5 text-sm", label);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}
function renderTopic(topic) {
  const head = el("div", "drawer-head");
  const title = el("h2", "font-display text-2xl text-white", topic.title);
  title.id = "drawer-title";
  const where = topic.sources.map((name) => RADAR_SOURCES[name] + (topic.ranks[name] ? " #" + topic.ranks[name] : "")).join(" · ");
  const actions = el("div", "mt-3 flex flex-wrap gap-2");
  actions.append(
    linkButton("Ver no X", "https://x.com/search?q=" + encodeURIComponent(topic.title) + "&f=live", true),
    linkButton("Pesquisar no Google", "https://www.google.com/search?q=" + encodeURIComponent(topic.title) + "&tbm=nws", false)
  );
  const watch = el("button", "rounded-full border border-neon/40 px-4 py-1.5 text-sm", "Alertar este termo");
  watch.type = "button";
  const already = ((radarData && radarData.my_terms) || []).some((t) => t.topic === topic.norm || t.norm === topic.norm);
  if (already) { watch.disabled = true; watch.textContent = "Termo vigiado"; }
  watch.addEventListener("click", async () => {
    if (await addRadarTerm(topic.title.replace(/^#/, ""))) { watch.disabled = true; watch.textContent = "Termo vigiado"; }
  });
  actions.append(watch);
  head.append(el("span", "radar-cat", topic.category), title, el("p", "text-sm text-mute", where), actions);
  const stats = el("dl", "drawer-stats");
  [["Calor", topic.heat + "/100"], ["No X agora", topic.rank_x ? "#" + topic.rank_x : "—"], ["Em alta há", topic.hours_x ? topic.hours_x + " h" : "—"], ["Buscas", topic.volume || "—"]].forEach(([label, value]) => {
    const item = el("div", "drawer-stat");
    item.append(el("dt", "text-[11px] text-mute", label), el("dd", "font-mono text-lg text-white", value));
    stats.append(item);
  });
  const extra = el("div", "space-y-5");
  extra.append(drawerSection("Ideias de post", ideasBox(topic)));
  const seen = topic.history_x.some((hour) => hour.rank !== null);
  const canvasBox = el("div", "h-44");
  const canvas = document.createElement("canvas");
  canvasBox.append(canvas);
  extra.append(drawerSection("Posição no X nas últimas 24 h", seen ? canvasBox : el("p", "text-sm text-mute", "Não apareceu nos trending topics do X nas últimas 24 h.")));
  const news = el("ol", "drawer-list");
  topic.news.forEach((item) => {
    const li = el("li", "radar-news");
    const link = el("a", "radar-headline", item.title);
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    li.append(link, el("span", "text-[11px] text-mute", item.source || ""));
    news.append(li);
  });
  extra.append(drawerSection("Notícias ligadas", topic.news.length ? news : el("p", "text-sm text-mute", "Nenhuma notícia ligada a esse assunto agora.")));
  const postsBox = el("div", "space-y-2");
  const postsButton = el("button", "w-full rounded-2xl border border-neon/40 px-4 py-2.5 text-sm", "Ver posts que estão engajando");
  postsButton.type = "button";
  postsBox.append(postsButton, el("p", "text-[11px] text-mute", "Busca no X pelo seu Apify (usa créditos). O resultado fica guardado 30 min."));
  postsButton.addEventListener("click", () => loadEngagingPosts(topic.title, postsBox));
  extra.append(drawerSection("Posts que estão engajando", postsBox));
  document.getElementById("drawer-body").replaceChildren(head, stats, extra);
  if (seen && window.Chart) {
    if (drawerChart) drawerChart.destroy();
    const accent = cssVar("--accent") || "#ff2ec8";
    drawerChart = new Chart(canvas, {
      type: "line",
      data: {
        labels: topic.history_x.map((hour) => (hour.at ? new Date(hour.at).toLocaleTimeString("pt-BR", { timeZone: "America/Sao_Paulo", hour: "2-digit", minute: "2-digit" }) : "")),
        datasets: [{ label: "Posição", legendColor: accent, data: topic.history_x.map((hour) => hour.rank), borderColor: accent, backgroundColor: accent,
          tension: .3, pointRadius: 2, pointHoverRadius: 4, borderWidth: 2, spanGaps: false }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => "#" + ctx.parsed.y } } },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 6 } },
          y: { reverse: true, min: 1, suggestedMax: 50, ticks: { precision: 0, maxTicksLimit: 5, callback: (value) => "#" + value } }
        }
      }
    });
  }
}

// ---------- busca rápida (Ctrl+K ou /) ----------
const TAB_LIST = [["dashboard", "Dashboard"], ["radar", "Radar"], ["ops", "Operações"], ["archived", "Arquivadas"], ["post", "Verificar post"], ["cloaker", "Metadados"], ["users", "Usuários"], ["settings", "Configurações"]];
function visibleTabs() { return TAB_LIST.filter(([tab]) => isAdmin() || !ADMIN_TABS.includes(tab)); }
let paletteItems = [];
let paletteIndex = 0;
function paletteOpen() { return document.getElementById("palette").classList.contains("open"); }
function paletteSource() {
  const items = visibleTabs().map(([tab, label], index) => ({ icon: tab, title: label, hint: "Aba · tecla " + (index + 1), run: () => setTab(tab) }));
  items.push(
    { icon: "refresh", title: "Atualizar dados", hint: "Ação · tecla R", run: () => { setTab("dashboard"); document.getElementById("refresh-all").click(); } },
    { icon: "theme", title: currentTheme() === "light" ? "Usar tema escuro" : "Usar tema claro", hint: "Ação · tecla T", run: () => document.getElementById("theme-btn").click() }
  );
  ((state.data && state.data.accounts) || []).filter((a) => !a.is_archived).forEach((account) => items.push({
    icon: "account",
    title: account.display_name || "@" + account.username,
    hint: "Conta · @" + account.username + " · " + account.folder_name,
    run: () => { setTab("dashboard"); openDrawer(account.id); }
  }));
  return items;
}
function paintPalette() {
  const terms = plain(document.getElementById("palette-input").value.trim()).split(/\s+/).filter(Boolean);
  paletteItems = paletteSource().filter((item) => terms.every((term) => plain(item.title + " " + item.hint).includes(term))).slice(0, 30);
  paletteIndex = Math.min(paletteIndex, Math.max(0, paletteItems.length - 1));
  const list = document.getElementById("palette-list");
  if (!paletteItems.length) {
    list.replaceChildren(el("li", "palette-empty", "Nada encontrado. Tente o nome de uma aba ou conta."));
    return;
  }
  list.replaceChildren(...paletteItems.map((item, index) => {
    const li = el("li", "palette-item" + (index === paletteIndex ? " on" : ""));
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", String(index === paletteIndex));
    const text = el("span", "min-w-0 flex-1");
    text.append(el("span", "block truncate text-sm text-white", item.title), el("span", "block truncate text-[11px] text-mute", item.hint));
    li.append(icon(item.icon), text);
    li.addEventListener("mousemove", () => { if (paletteIndex !== index) { paletteIndex = index; paintPalette(); } });
    li.addEventListener("click", () => runPalette(index));
    return li;
  }));
  const active = list.children[paletteIndex];
  if (active) active.scrollIntoView({ block: "nearest" });
}
function openPalette() {
  if (!token()) return;
  const palette = document.getElementById("palette");
  palette.classList.add("open");
  palette.setAttribute("aria-hidden", "false");
  const input = document.getElementById("palette-input");
  input.value = "";
  paletteIndex = 0;
  paintPalette();
  input.focus();
}
function closePalette() {
  const palette = document.getElementById("palette");
  if (!palette.classList.contains("open")) return;
  palette.classList.remove("open");
  palette.setAttribute("aria-hidden", "true");
}
function runPalette(index) {
  const item = paletteItems[index];
  if (!item) return;
  closePalette();
  item.run();
}
document.getElementById("palette-input").addEventListener("input", () => { paletteIndex = 0; paintPalette(); });
document.getElementById("palette-input").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    paletteIndex = (paletteIndex + step + paletteItems.length) % Math.max(1, paletteItems.length);
    paintPalette();
  } else if (event.key === "Enter") {
    event.preventDefault();
    runPalette(paletteIndex);
  }
});
document.getElementById("palette").addEventListener("click", (event) => { if (event.target.id === "palette") closePalette(); });
document.getElementById("palette-btn").addEventListener("click", openPalette);

// ---------- busca e ordem das contas ----------
document.getElementById("acc-search").addEventListener("input", () => { if (state.data) renderAccounts(state.data.accounts); });
document.getElementById("acc-sort").addEventListener("click", (event) => {
  const button = event.target.closest("[data-sort]");
  if (!button) return;
  try { localStorage.setItem(ACC_SORT_KEY, button.dataset.sort); } catch { /* vale só agora */ }
  paintAccountSort();
  if (state.data) renderAccounts(state.data.accounts);
});
paintAccountSort();

// ---------- atalhos de teclado ----------
// 1–7 abas, T tema, R atualizar dados, / ou Ctrl+K busca, ? lista os atalhos. Nunca enquanto se digita.
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (paletteOpen()) closePalette(); else openPalette();
    return;
  }
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
  if (event.target.closest && event.target.closest("input, textarea, select, [contenteditable]")) return;
  if (!token() || !document.getElementById("auth-modal").classList.contains("off") || paletteOpen()) return;
  const key = event.key.toLowerCase();
  if (event.key === "/") { event.preventDefault(); openPalette(); }
  else if (drawerOpen()) return;
  else if (/^[1-9]$/.test(key) && visibleTabs()[Number(key) - 1]) setTab(visibleTabs()[Number(key) - 1][0]);
  else if (key === "t") document.getElementById("theme-btn").click();
  else if (key === "r") { setTab("dashboard"); document.getElementById("refresh-all").click(); }
  else if (event.key === "?") toast("Atalhos: 1 a " + visibleTabs().length + " trocam de aba, T troca o tema, R atualiza os dados, / ou Ctrl+K abre a busca.", "info");
});

// ---------- puxar para atualizar (app instalado, no toque) ----------
// No navegador o próprio Chrome já faz isso; no app instalado não existe, então fazemos aqui.
async function refreshCurrentTab() {
  if (state.tab === "dashboard") await load();
  else if (state.tab === "radar") await loadRadar();
  else if (state.tab === "users") await loadUsers();
  else if (state.tab === "archived") await loadArchived();
  else if (state.tab === "settings") await loadSettings();
}
if (isStandalone() && "ontouchstart" in window) {
  const ptr = document.getElementById("ptr");
  let pullFrom = null;
  const reset = () => { ptr.classList.remove("show", "ready", "spin"); ptr.style.transform = ""; };
  addEventListener("touchstart", (event) => {
    pullFrom = scrollY <= 0 && event.touches.length === 1 && !drawerOpen() && !paletteOpen() ? event.touches[0].clientY : null;
  }, { passive: true });
  addEventListener("touchmove", (event) => {
    if (pullFrom === null) return;
    const pull = Math.min((event.touches[0].clientY - pullFrom) * .45, 84);
    if (pull <= 4) { reset(); return; }
    ptr.classList.add("show");
    ptr.classList.toggle("ready", pull >= 60);
    ptr.style.transform = "translate(-50%, " + pull + "px) rotate(" + pull * 4 + "deg)";
  }, { passive: true });
  addEventListener("touchend", async () => {
    if (pullFrom === null) return;
    pullFrom = null;
    if (!ptr.classList.contains("ready")) { reset(); return; }
    ptr.classList.add("spin");
    ptr.style.transform = "translate(-50%, 60px)";
    try { await refreshCurrentTab(); } finally { reset(); }
  });
}

boot();
