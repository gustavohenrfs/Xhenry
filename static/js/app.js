const fmt = new Intl.NumberFormat("pt-BR");
const titles = { dashboard: "Dashboard", ops: "Operações", post: "Verificar post", cloaker: "Metadados", settings: "Configurações", archived: "Arquivadas", collectors: "Coletores" };
const TOKEN_KEY = "access_token";
const state = {
  tab: "dashboard", period: "today", folderId: null, opsFolderId: null, data: null, openId: null, authMode: "login",
  config: { allow_registration: false, password_reset: false }, resetToken: ""
};
const SESSION_EXPIRED = "Sua sessão expirou. Entre novamente.";
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
// Avisos com tipo: ícone e cor de sucesso, erro, coletor (🛡️) ou informação, e uma barrinha que mostra
// quanto falta para sumir. O tipo vem do próprio texto quando quem chama não diz.
const TOAST_ICONS = { ok: "✓", error: "!", shield: "🛡️", info: "i" };
const TOAST_MS = 4200;
function toastKind(message) {
  if (/^🛡/.test(message)) return "shield";
  if (/^(✅|Salv|Pronto|Teste (enviado|mostrado|pedido)|Criad|Removid|Excluíd|Atualizad|Arquivad|Desarquivad|Senha (alterada|atualizada)|XHenry instalado|Notificações ligadas|Coletor (adicionado|salvo))/i.test(message)) return "ok";
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
  if (mode === "register" && !state.config.allow_registration) mode = "login";
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
  document.getElementById("auth-title").textContent = AUTH_MODES[mode].title;
  document.querySelector("#auth-form button[type=submit]").textContent = AUTH_MODES[mode].submit;
  document.getElementById("forgot-link").classList.toggle("hidden", mode !== "login" || !state.config.password_reset);
  const sw = document.getElementById("auth-switch");
  sw.textContent = mode === "login" ? "Criar conta" : "Voltar para o login";
  sw.classList.toggle("hidden", mode === "login" && !state.config.allow_registration);
  authMessage("auth-info", "");
  authMessage("auth-error", "");
}
async function loadConfig() {
  try {
    state.config = Object.assign(state.config, await api("/api/config"));
  } catch (error) {
    // Sem config: fica o padrão fechado (sem cadastro, sem recuperação).
  }
}
// Envia o Bearer e lança o detail do servidor em caso de erro; devolve o Response cru.
async function apiResponse(path, options) {
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
  if (tab === "collectors") loadCollectors();
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
    el("p", "mt-1 text-xs text-mute", label ? label + ": " + num(prevOf("views")) + " views" : "Desde o primeiro post coletado")
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
  if (mixChart) { mixChart.data = mixData; mixChart.options = options; mixChart.update(); }
  else mixChart = new Chart(document.getElementById("mix-chart"), { type: "doughnut", data: mixData, options });
}
function renderTopPost(post) {
  const host = document.getElementById("top-post-body");
  if (!post) {
    host.replaceChildren(el("p", "text-sm text-mute", "Nenhum post coletado no período. Clique em Atualizar dados para ler as contas agora."));
    return;
  }
  const initial = post.username.slice(0, 1).toUpperCase();
  const avatar = el("div", "post-avatar");
  if (post.profile_image_url) {
    const img = el("img");
    img.src = post.profile_image_url;
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
    host.replaceChildren(el("li", "text-sm text-mute", "Nenhum post coletado no período."));
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
  coletor: ["Coletor", "#ffc46b"], erro: ["Erro", "#ffc46b"], silencio: ["Robô parado", "#ffc46b"]
};
const VIEW_LEVEL_TAGS = [
  [/MILHÃO/i, "👑 1M de views", "#ff63c3"],
  [/100K/i, "💯 100K de views", "#ffd36b"],
  [/BOMBANDO/i, "🔥 Post bombando", "#ff9f5a"]
];
function renderAlerts(alerts) {
  const host = document.getElementById("alert-list");
  if (!alerts.length) {
    host.replaceChildren(el("li", "text-sm text-mute", "Nenhum alerta enviado ainda."));
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
    renderAlerts(await api("/api/alerts?limit=8"));
  } catch (error) {
    if (error.message === SESSION_EXPIRED) return;
    document.getElementById("alert-list").replaceChildren(el("li", "text-sm text-mute", "Não deu para ler os alertas: " + (error.message || "erro") + "."));
  }
}
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
// Coletores = ligados de castigo ou com erro. A soma vai também para o ícone do app instalado
// (barra de tarefas do Windows, tela de início do celular).
const tabBadges = { alerts: 0, collectors: 0 };
function paintTabBadge(name, count) {
  tabBadges[name] = count || 0;
  document.querySelectorAll('[data-badge="' + name + '"]').forEach((badge) => {
    badge.textContent = count > 9 ? "9+" : String(count || "");
    badge.classList.toggle("hidden", !count);
  });
  const total = tabBadges.alerts + tabBadges.collectors;
  try {
    if (total && navigator.setAppBadge) navigator.setAppBadge(total).catch(() => {});
    else if (!total && navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
  } catch { /* navegador sem selo no ícone */ }
}
function paintCollectorBadge(count) { paintTabBadge("collectors", count); }
function renderHealth(health) {
  paintCollectorBadge((health.collectors && health.collectors.attention) || 0);
  const host = document.getElementById("health-body");
  const note = document.getElementById("health-note");
  const late = health.late_after_min >= 120 ? Math.round(health.late_after_min / 60) + " h" : health.late_after_min + " min";
  note.textContent = health.enabled
    ? "Atrasada é a conta sem coleta há mais de " + late + "."
    : "Coleta automática desligada: só coleta quando você clica em Atualizar dados.";
  const gauge = el("div", "gauge-wrap");
  gauge.append(ring(health.fresh, health.accounts), el("p", "gauge-label font-mono text-lg text-white", health.fresh + "/" + health.accounts));
  const behind = health.stale.length;
  const last = health.last_run;
  const summary = el("div", "min-w-0 flex-1 text-left");
  summary.append(
    el("p", "text-sm text-white", behind ? behind + (behind === 1 ? " conta atrasada" : " contas atrasadas") : "Todas as contas em dia"),
    el("p", "mt-1 text-xs text-mute", last ? "Última rodada " + agoText(last.finished_at) + ", às " + stampBR(last.finished_at) : "O robô ainda não rodou")
  );
  const top = el("div", "flex items-center gap-4");
  top.append(gauge, summary);
  const c = health.collectors;
  const seg = el("div", "seg-bar mt-2");
  [["free", c.free], ["benched", c.benched], ["off", c.off]].forEach(([kind, count]) => {
    if (!count) return;
    const part = el("span", "seg seg-" + kind);
    part.style.flexGrow = String(count);
    seg.append(part);
  });
  const legend = el("div", "mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-mute");
  [["free", "livres", c.free], ["benched", "de castigo", c.benched], ["off", "desligados", c.off]].forEach(([kind, label, count]) => {
    const item = el("span", "inline-flex items-center gap-1.5");
    item.append(el("span", "seg-dot seg-" + kind), el("span", "", count + " " + label));
    legend.append(item);
  });
  const parts = [top, el("p", "mt-4 text-xs text-mute", c.total ? c.total + (c.total === 1 ? " coletor" : " coletores") : "Nenhum coletor cadastrado. Cadastre na aba Coletores.")];
  if (c.total) parts.push(seg, legend);
  if (c.next_free_at) parts.push(el("p", "mt-2 text-xs text-mute", "Próximo coletor livre em " + stampBR(c.next_free_at)));
  if (behind) {
    const list = el("ul", "mt-4 flex flex-wrap gap-2");
    health.stale.slice(0, 6).forEach((item) => list.append(el("li", "stale-chip", "@" + item.username + " " + (item.collected_at ? agoText(item.collected_at) : "sem coleta"))));
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
    document.getElementById("health-note").textContent = "Não deu para ler a saúde da coleta: " + (error.message || "erro") + ".";
    document.getElementById("health-body").replaceChildren();  // tira o esqueleto de carregamento
  }
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
    : "Views por dia dos posts coletados";
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
  if (lineChart) { lineChart.data = { labels, datasets }; lineChart.options = options; lineChart.update(); }
  else lineChart = new Chart(document.getElementById("line-chart"), { type: "line", data: { labels, datasets }, options });
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
  if (barChart) { barChart.data = barData; barChart.options = barOptions; barChart.update(); }
  else barChart = new Chart(canvas, { type: "bar", data: barData, options: barOptions });
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
  if (!iso) { el.textContent = "Última coleta: nenhuma ainda"; return; }
  const when = new Date(iso);
  const mins = Math.max(0, Math.round((Date.now() - when.getTime()) / 60000));
  const stamp = when.toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const ago = mins < 1 ? "agora" : mins < 60 ? "há " + mins + " min" : mins < 1440 ? "há " + Math.floor(mins / 60) + " h" : "há " + Math.floor(mins / 1440) + " d";
  el.textContent = "Última coleta: " + stamp + " (" + ago + ")";
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
      loadHealth();
      loadAlerts();
      checkViral();
    }
    renderLastCollect(data.last_collected_at);
    renderDashboard(data, sig, !silent && !cached && lastSig !== "", silent);
    markLive(true);
    startLive();
    if (!silent) prefetchPeriods();
  } catch (error) {
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
    "@" + account.username + " sai do dashboard e o robô para de coletar. O histórico fica guardado na aba Arquivadas."
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
const MORE_TABS = ["post", "cloaker", "settings"];
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
async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    document.getElementById("goal").value = settings.impression_goal;
    const tg = document.getElementById("tg-status");
    tg.textContent = settings.telegram_ready
      ? "Telegram: usa o bot do .env do VPS, o mesmo do robô."
      : "Telegram: falta TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env do VPS (os alertas e a recuperação de senha dependem dele).";
    tg.classList.toggle("text-amber-300", !settings.telegram_ready);
    loadRobo();
    document.getElementById("xai-key").placeholder = settings.xai_key_set ? "Chave já salva" : "xai-...";
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
  const span = account.first_post_at ? "posts de " + dateBR(account.first_post_at) + " a " + dateBR(account.last_post_at) : "nenhum post coletado";
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
    host.replaceChildren(...(accounts.length ? accounts.map(archivedCard) : [emptyState("archived", "Nenhuma conta arquivada", "Arquivar tira a conta do Dashboard e da coleta, mas o histórico fica guardado aqui.")]));
  } catch (error) {
    host.replaceChildren(message(error.message || "Erro ao carregar."));
  }
}
// Coletores: contas descartáveis que o robô usa para ler. Proxy e cookies chegam mascarados.
const COLLECTOR_STATES = { ready: ["Pronto", "st-healthy"], paused: ["Pausado", "st-search_ban"], off: ["Desligado", "st-none"] };
const COLLECTOR_RESULTS = {
  ok: "Última leitura ok", limite: "X limitou (429)", bloqueio: "X bloqueou a leitura",
  verificacao: "🛡️ X pediu verificação de humanidade (Cloudflare): entre no x.com com este coletor, resolva e aperte Testar",
  sessao: "Sessão caiu: cole cookies novos", proxy: "Proxy não respondeu", erro: "Erro na última leitura"
};
// Resultado do botão Testar (pending = o robô ainda vai ler; o resto é o que ele gravou).
const COLLECTOR_TESTS = {
  pending: ["⏳ Testando", false], ok: ["✅ Funcionando", false], conta: ["✅ Coletor ok, a conta lida deu erro", false],
  proxy: ["❌ Proxy fora do ar", true], verificacao: ["🛡️ X pediu verificação", true], sessao: ["🔑 Sessão caiu", true],
  limite: ["⛔ X limitou (429)", true], bloqueio: ["🚫 X bloqueou a leitura", true], erro: ["⚠️ Erro no teste", true]
};
let collectorsPolledAt = 0;
let collectorsSig = "";
let collectorsPending = new Set();
let collectorEditing = null;
function timeBR(iso) {
  return iso ? new Date(iso).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
}
function collectorLine(label, value, warn) {
  const p = document.createElement("p");
  p.className = "mt-1 break-all text-xs " + (warn ? "text-amber-200" : "text-mute");
  const b = document.createElement("span");
  b.className = "text-white/70";
  b.textContent = label + ": ";
  p.append(b, document.createTextNode(value));
  return p;
}
function collectorButton(text, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "rounded-full border border-neon/40 px-3 py-1 text-xs";
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}
function collectorCard(collector) {
  const card = document.createElement("article");
  card.className = "glow rounded-2xl border border-neon/20 bg-card p-4";
  card.dataset.collectorId = String(collector.id);
  const head = document.createElement("div");
  head.className = "flex items-start justify-between gap-3";
  const name = document.createElement("p");
  name.className = "min-w-0 truncate font-medium text-white";
  name.textContent = "@" + collector.handle;
  const [label, style] = COLLECTOR_STATES[collector.state] || COLLECTOR_STATES.off;
  const badge = document.createElement("span");
  badge.className = "shrink-0 rounded-full border px-2.5 py-1 text-xs font-medium " + style;
  const pausedAs = collector.last_status === "verificacao" ? "🛡️ Verificação" : label;
  badge.textContent = collector.state === "paused" ? pausedAs + " até " + timeBR(collector.paused_until) : label;
  head.append(name, badge);

  card.append(
    head,
    collectorLine("Proxy", collector.proxy || "nenhum: sai pelo IP do VPS", !collector.proxy),
    collectorLine("Cookies", collector.cookies || "nenhum: usa o login do anyx no VPS", false),
    collectorLine("Última leitura", timeBR(collector.last_used_at), false)
  );
  if (collector.last_status && collector.last_status !== "ok") {
    const detail = COLLECTOR_RESULTS[collector.last_status] || collector.last_status;
    card.append(collectorLine("Aviso", collector.last_error ? detail + " — " + collector.last_error : detail, true));
  }
  if (collector.test_status || collector.test_detail) {
    const [title, warn] = COLLECTOR_TESTS[collector.test_status || "pending"] || [collector.test_status, true];
    const when = collector.test_at ? " (" + timeBR(collector.test_at) + ")" : "";
    card.append(collectorLine("Teste" + when, title + (collector.test_detail ? ": " + collector.test_detail : ""), warn));
  }

  const actions = document.createElement("div");
  actions.className = "mt-4 flex flex-wrap gap-2";
  const testing = collector.test_status === "pending";
  const test = collectorButton(testing ? "Testando…" : "Testar", (event) => testCollector(collector, event.currentTarget));
  test.disabled = testing;
  test.classList.add("collector-test");
  actions.append(
    test,
    collectorButton("Editar", () => editCollector(collector)),
    collectorButton(collector.is_active ? "Desligar" : "Ligar", () => saveCollector(collector.id, { is_active: !collector.is_active })),
  );
  if (collector.proxy) actions.append(collectorButton("Tirar proxy", () => saveCollector(collector.id, { proxy: "" })));
  actions.append(collectorButton("Remover", () => removeCollector(collector)));
  card.append(actions);
  return card;
}
async function loadCollectors({ silent = false } = {}) {
  const host = document.getElementById("collector-list");
  if (!host) return;
  const message = (text) => {
    const p = document.createElement("p");
    p.className = "text-sm text-mute";
    p.textContent = text;
    return p;
  };
  collectorsPolledAt = Date.now();
  try {
    const collectors = await api("/api/collectors");
    // Teste que acabou de sair do "pending": avisa o resultado sem precisar olhar o card.
    const pending = new Set();
    collectors.forEach((collector) => {
      if (collector.test_status === "pending") pending.add(collector.id);
      else if (collectorsPending.has(collector.id)) {
        const [title] = COLLECTOR_TESTS[collector.test_status] || ["Teste concluído"];
        toast("@" + collector.handle + ": " + title + (collector.test_detail ? " — " + collector.test_detail : ""));
      }
    });
    collectorsPending = pending;
    paintCollectorBadge(collectors.filter((c) => c.is_active && (c.state === "paused" || (c.last_status && c.last_status !== "ok"))).length);
    const sig = JSON.stringify(collectors);
    if (silent && sig === collectorsSig) return;  // nada mudou: não mexe no DOM
    collectorsSig = sig;
    host.replaceChildren(...(collectors.length ? collectors.map(collectorCard) : [emptyState("collectors", "Nenhum coletor ainda", "Sem coletores o robô não lê nenhuma timeline. Cadastre uma conta descartável com o proxy dela.", "Adicionar coletor", () => document.getElementById("collector-handle").focus())]));
  } catch (error) {
    if (!silent) host.replaceChildren(message(error.message || "Erro ao carregar."));
  }
}
async function testCollector(collector, button) {
  button.disabled = true;
  button.textContent = "Testando…";
  try {
    const result = await api("/api/collectors/" + collector.id + "/test", { method: "POST" });
    toast(result.test_status === "proxy"
      ? "❌ Proxy de @" + collector.handle + " fora do ar: " + (result.test_detail || "")
      : "✅ " + (result.test_detail || "Teste pedido."));
  } catch (error) {
    toast(error.message || "Não foi possível testar.");
  }
  await loadCollectors();
}
// Aba Coletores aberta: atualiza sozinha a cada 30 s, ou a cada 5 s enquanto um teste está na fila.
setInterval(() => {
  if (state.tab !== "collectors" || document.hidden || !token()) return;
  if (Date.now() - collectorsPolledAt >= (collectorsPending.size ? 5000 : 30000)) loadCollectors({ silent: true });
}, 5000);
function resetCollectorForm() {
  collectorEditing = null;
  const handle = document.getElementById("collector-handle");
  handle.value = "";
  handle.disabled = false;
  document.getElementById("collector-proxy").value = "";
  clearCollectorFile();
  document.getElementById("collector-proxy").placeholder = "Proxy: ip:porta:usuario:senha";
  document.getElementById("collector-submit").textContent = "Adicionar coletor";
  document.getElementById("collector-cancel").classList.add("hidden");
  document.getElementById("collector-hint").textContent = "Proxy e cookies ficam mascarados no painel; só o robô lê os valores.";
}
function editCollector(collector) {
  collectorEditing = collector;
  const handle = document.getElementById("collector-handle");
  handle.value = "@" + collector.handle;
  handle.disabled = true;
  document.getElementById("collector-proxy").value = "";
  clearCollectorFile();
  document.getElementById("collector-proxy").placeholder = collector.proxy ? "Atual: " + collector.proxy + " (vazio = mantém)" : "Proxy: ip:porta:usuario:senha";
  document.getElementById("collector-submit").textContent = "Salvar @" + collector.handle;
  document.getElementById("collector-cancel").classList.remove("hidden");
  document.getElementById("collector-hint").textContent = "Campos vazios mantêm o que está salvo.";
  document.getElementById("collector-form").scrollIntoView({ behavior: "smooth", block: "start" });
}
async function saveCollector(id, body) {
  try {
    await api("/api/collectors/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    await loadCollectors();
    return true;
  } catch (error) {
    toast(error.message || "Não salvou o coletor.");
    return false;
  }
}
async function removeCollector(collector) {
  const ok = await showConfirm("Remover coletor", "O robô deixa de usar @" + collector.handle + ". Proxy e cookies salvos são apagados.");
  if (!ok) return;
  try {
    await api("/api/collectors/" + collector.id, { method: "DELETE" });
    if (collectorEditing && collectorEditing.id === collector.id) resetCollectorForm();
    await loadCollectors();
  } catch (error) { toast(error.message || "Não removeu."); }
}
// Cookies: só o arquivo .json exportado pela extensão (arrastar ou escolher). O servidor filtra x.com e exige auth_token.
let collectorCookies = "";
const COLLECTOR_FILE_HINT = document.getElementById("collector-file-label").textContent;
function clearCollectorFile() {
  collectorCookies = "";
  document.getElementById("collector-file").value = "";
  const label = document.getElementById("collector-file-label");
  label.textContent = COLLECTOR_FILE_HINT;
  label.classList.add("text-mute");
  document.getElementById("collector-file-clear").classList.add("hidden");
}
function countXCookies(data) {
  if (data && !Array.isArray(data) && Array.isArray(data.cookies)) data = data.cookies;
  if (Array.isArray(data)) {
    const mine = data.filter((c) => c && c.name && /(^|\.)(x|twitter)\.com$/.test(String(c.domain || ".x.com")));
    return { total: mine.length, token: mine.some((c) => c.name === "auth_token" && c.value) };
  }
  if (data && typeof data === "object") return { total: Object.keys(data).length, token: Boolean(data.auth_token) };
  return { total: 0, token: false };
}
async function loadCollectorFile(file) {
  if (!file) return;
  clearCollectorFile();
  if (file.size > 200000) { toast("Arquivo grande demais para ser de cookies."); return; }
  const text = (await file.text()).trim();
  let data;
  try { data = JSON.parse(text); } catch { toast("Esse arquivo não é um JSON válido."); return; }
  const { total, token } = countXCookies(data);
  if (!token) { toast("O arquivo não tem o cookie auth_token do x.com. Exporte os cookies com a conta logada."); return; }
  collectorCookies = text;
  const label = document.getElementById("collector-file-label");
  label.textContent = "✓ " + file.name + " — " + total + " cookies do x.com, com auth_token";
  label.classList.remove("text-mute");
  document.getElementById("collector-file-clear").classList.remove("hidden");
}
const collectorDrop = document.getElementById("collector-drop");
document.getElementById("collector-file").addEventListener("change", (event) => loadCollectorFile(event.target.files[0]));
document.getElementById("collector-file-clear").addEventListener("click", (event) => { event.preventDefault(); clearCollectorFile(); });
["dragenter", "dragover"].forEach((name) => collectorDrop.addEventListener(name, (event) => {
  event.preventDefault();
  collectorDrop.classList.add("border-neon");
}));
["dragleave", "drop"].forEach((name) => collectorDrop.addEventListener(name, () => collectorDrop.classList.remove("border-neon")));
collectorDrop.addEventListener("drop", (event) => {
  event.preventDefault();
  loadCollectorFile(event.dataTransfer.files[0]);
});
document.getElementById("collector-cancel").addEventListener("click", resetCollectorForm);
document.getElementById("collector-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const proxy = document.getElementById("collector-proxy").value.trim();
  const cookies = collectorCookies;
  if (collectorEditing) {
    const body = {};
    if (proxy) body.proxy = proxy;
    if (cookies) body.cookies = cookies;
    if (!Object.keys(body).length) { resetCollectorForm(); return; }
    if (await saveCollector(collectorEditing.id, body)) {
      toast("@" + collectorEditing.handle + " atualizado.");
      resetCollectorForm();
    }
    return;
  }
  try {
    const created = await api("/api/collectors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ handle: document.getElementById("collector-handle").value.trim(), proxy, cookies })
    });
    toast("@" + created.handle + " adicionado.");
    resetCollectorForm();
    await loadCollectors();
  } catch (error) { toast(error.message || "Não adicionou o coletor."); }
});
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
document.getElementById("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = { impression_goal: Number(document.getElementById("goal").value) };
  const xai = document.getElementById("xai-key").value.trim();
  if (xai) payload.xai_api_key = xai;
  try {
    await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
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

// Robô: a aba Configurações liga/desliga a coleta automática e escolhe o intervalo; o robo.py lê isso
// em app_settings a cada minuto (cron). "Atualizar dados" grava um pedido e espera a rodada terminar.
function intervalLabel(minutes) {
  return minutes < 60 ? minutes + " min" : minutes / 60 + (minutes === 60 ? " hora" : " horas");
}
function stampBR(iso) {
  return new Date(iso).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function paintRobo(robo) {
  const toggle = document.getElementById("robo-enabled");
  const select = document.getElementById("robo-interval");
  toggle.checked = robo.enabled;
  select.replaceChildren(...robo.intervals.map((minutes) => {
    const option = document.createElement("option");
    option.value = String(minutes);
    option.textContent = intervalLabel(minutes);
    return option;
  }));
  select.value = String(robo.interval_min);
  select.disabled = !robo.enabled;
  const parts = [];
  if (robo.last_run) {
    const run = robo.last_run;
    let text = "Última rodada: " + stampBR(run.finished_at);
    if (run.contas !== null && run.contas !== undefined) text += " · " + run.contas + " contas, " + (run.tweets || 0) + " tweets";
    if (run.status === "failed") text += " · falhou";
    else if (run.status === "partial") text += " · com falhas";
    parts.push(text);
  } else parts.push("O robô ainda não rodou.");
  if (robo.requested_at) parts.push("Coleta pedida às " + stampBR(robo.requested_at) + ", aguardando o robô.");
  else if (!robo.enabled) parts.push("Coleta automática desligada.");
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
    toast(body.enabled === false ? "Coleta automática desligada." : body.enabled ? "Coleta automática ligada: a primeira rodada sai em até 1 minuto." : "Intervalo salvo: a cada " + intervalLabel(robo.interval_min) + ".");
  } catch (error) {
    toast(error.message || "Não salvou o robô.");
    loadRobo();
  }
}
document.getElementById("robo-enabled").addEventListener("change", (event) => saveRobo({ enabled: event.target.checked }));
document.getElementById("robo-interval").addEventListener("change", (event) => saveRobo({ interval_min: Number(event.target.value) }));

const ROBO_PICKUP_MS = 3 * 60000;   // cron a cada minuto: passou disso, o robô não está pegando os pedidos
const ROBO_RUN_MS = 12 * 60000;     // uma rodada manual pode levar alguns minutos (um Chrome por coletor)
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
document.getElementById("refresh-all").addEventListener("click", async () => {
  const btn = document.getElementById("refresh-all");
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Chamando os coletores...";
  try {
    const asked = await api("/api/robo/run", { method: "POST" });
    const askedAt = Date.parse(asked.requested_at);
    const started = Date.now();
    let robo = asked;
    for (;;) {
      await wait(5000);
      robo = await api("/api/robo");
      const finished = robo.last_run ? Date.parse(robo.last_run.finished_at) : 0;
      if (!robo.requested_at && finished >= askedAt) break;
      if (robo.requested_at) {
        if (Date.now() - started > ROBO_PICKUP_MS) throw new Error("O robô não pegou o pedido. Confira no VPS se o cron roda o robo.py a cada minuto.");
      } else btn.textContent = "Coletando...";
      if (Date.now() - started > ROBO_RUN_MS) throw new Error("A coleta está demorando. Os dados aparecem sozinhos quando ela terminar.");
    }
    await load();
    const run = robo.last_run;
    toast(run.status === "failed" ? "A coleta falhou. Veja o aviso no Telegram." : "Coleta feita: " + (run.contas || 0) + " contas, " + (run.tweets || 0) + " tweets.");
    if (state.tab === "settings") paintRobo(robo);
  } catch (error) { toast(error.message || "Falha ao atualizar."); }
  btn.disabled = false;
  btn.textContent = orig;
});
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
    const [user] = await Promise.all([api("/api/me"), load()]);
    paintUser(user);
  } catch (err) {
    if (err.message === SESSION_EXPIRED) return;
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
    showNotice("Notificações ligadas", "Você vai ver aqui cada post que viralizar, bombar, passar de 100K ou de 1M.", "/", "xhenry-hello").catch(() => {});
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
  if (!item.username) return item.display_name || "Conta removida";
  return (item.display_name ? item.display_name + " " : "") + "@" + item.username;
}
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
    host.replaceChildren(el("li", "bell-empty text-sm text-mute", "Nenhum post viralizou ainda. Quando um passar do limite, ele aparece aqui."));
    return;
  }
  const seen = storedId(VIRAL_SEEN_KEY);
  host.replaceChildren(...viralItems.map((item) => {
    const li = el("li", "bell-item lvl-" + (item.level || "viral") + (item.id > seen ? " is-new" : ""));
    const avatar = el("div", "post-avatar bell-avatar");
    const initial = (item.username || "?").slice(0, 1).toUpperCase();
    if (item.profile_image_url) {
      const img = el("img");
      img.src = item.profile_image_url;
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
const TILT_SELECTOR = ".kpi-card, #pane-dashboard article.glow, .account-card, #folder-cards > .glow, #ops-accounts > .glow, #collector-list > .glow, #archived-accounts > .glow";
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
  dashboard: '<rect x="3.5" y="3.5" width="7" height="9" rx="2"/><rect x="13.5" y="3.5" width="7" height="5" rx="2"/><rect x="13.5" y="11.5" width="7" height="9" rx="2"/><rect x="3.5" y="15.5" width="7" height="5" rx="2"/>',
  ops: '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.2h7a2 2 0 0 1 2 2v8.3a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z"/>',
  archived: '<rect x="3" y="4" width="18" height="4.5" rx="1.5"/><path d="M5 8.5v9.5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5"/><path d="M10 12.5h4"/>',
  collectors: '<circle cx="12" cy="12" r="2"/><path d="M7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4M5 19a10 10 0 0 1 0-14M19 5a10 10 0 0 1 0 14"/>',
  post: '<path d="M12 3.2 19 6v5.6c0 4.3-2.9 7.6-7 9.2-4.1-1.6-7-4.9-7-9.2V6Z"/><path d="m8.8 12 2.2 2.2 4.2-4.4"/>',
  cloaker: '<path d="m12 3.5 8.5 4.5-8.5 4.5L3.5 8Z"/><path d="m3.5 12 8.5 4.5 8.5-4.5"/><path d="m3.5 16 8.5 4.5 8.5-4.5"/>',
  settings: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2.2"/><circle cx="8" cy="17" r="2.2"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  account: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
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
// Painel ao vivo: o servidor avisa (/api/live, app/live.py) quando o robô grava dados, chega um alerta
// ou um coletor muda, e só a parte que mudou é recarregada, sem F5. O EventSource reconecta sozinho;
// o auto-refresh de 60 s continua como reserva.
let liveSource = null;
let liveStamp = null;
let dashboardDirty = false;
function startLive() {
  if (liveSource || !window.EventSource) return;
  liveSource = new EventSource("/api/live");
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
      loadHealth();
      if (state.tab === "dashboard" && !document.hidden) load({ silent: true });
      else dashboardDirty = true;  // recarrega quando o Dashboard voltar à tela
    }
    if (stamp.alerts !== before.alerts) { checkViral(); loadAlerts(); }
    if (stamp.collectors !== before.collectors) {
      loadHealth();
      if (state.tab === "collectors") loadCollectors({ silent: true });
    }
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
    extra.append(drawerSection("Melhores posts", detail.top_posts.length ? posts : el("p", "text-sm text-mute", "Nenhum post coletado no período.")));
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

// ---------- busca rápida (Ctrl+K ou /) ----------
const TAB_LIST = [["dashboard", "Dashboard"], ["ops", "Operações"], ["archived", "Arquivadas"], ["collectors", "Coletores"], ["post", "Verificar post"], ["cloaker", "Metadados"], ["settings", "Configurações"]];
let paletteItems = [];
let paletteIndex = 0;
let paletteCollectors = null;
function paletteOpen() { return document.getElementById("palette").classList.contains("open"); }
function paletteSource() {
  const items = TAB_LIST.map(([tab, label], index) => ({ icon: tab, title: label, hint: "Aba · tecla " + (index + 1), run: () => setTab(tab) }));
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
  (paletteCollectors || []).forEach((collector) => items.push({
    icon: "collectors",
    title: "@" + collector.handle,
    hint: "Coletor · " + ({ ready: "pronto", paused: "pausado", off: "desligado" }[collector.state] || collector.state),
    run: () => { setTab("collectors"); revealCollector(collector.id); }
  }));
  return items;
}
function paintPalette() {
  const terms = plain(document.getElementById("palette-input").value.trim()).split(/\s+/).filter(Boolean);
  paletteItems = paletteSource().filter((item) => terms.every((term) => plain(item.title + " " + item.hint).includes(term))).slice(0, 30);
  paletteIndex = Math.min(paletteIndex, Math.max(0, paletteItems.length - 1));
  const list = document.getElementById("palette-list");
  if (!paletteItems.length) {
    list.replaceChildren(el("li", "palette-empty", "Nada encontrado. Tente o nome de uma aba, conta ou coletor."));
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
  api("/api/collectors").then((list) => { paletteCollectors = list; if (paletteOpen()) paintPalette(); }).catch(() => {});
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
function revealCollector(id) {
  let tries = 0;
  const look = () => {
    const card = document.querySelector('#collector-list [data-collector-id="' + id + '"]');
    if (card) { card.scrollIntoView({ behavior: "smooth", block: "center" }); flash(card); return; }
    if (++tries < 20) setTimeout(look, 100);
  };
  look();
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
  else if (/^[1-7]$/.test(key)) setTab(TAB_LIST[Number(key) - 1][0]);
  else if (key === "t") document.getElementById("theme-btn").click();
  else if (key === "r") { setTab("dashboard"); document.getElementById("refresh-all").click(); }
  else if (event.key === "?") toast("Atalhos: 1 a 7 trocam de aba, T troca o tema, R atualiza os dados, / ou Ctrl+K abre a busca.", "info");
});

// ---------- puxar para atualizar (app instalado, no toque) ----------
// No navegador o próprio Chrome já faz isso; no app instalado não existe, então fazemos aqui.
async function refreshCurrentTab() {
  if (state.tab === "dashboard") await load();
  else if (state.tab === "collectors") await loadCollectors();
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
