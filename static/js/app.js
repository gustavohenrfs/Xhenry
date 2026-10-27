const fmt = new Intl.NumberFormat("pt-BR");
const titles = { dashboard: "Dashboard", ops: "Operações", post: "Verificar Post", cloaker: "Metadados", settings: "Configurações", archived: "Arquivadas", collectors: "Coletores" };
const TOKEN_KEY = "access_token";
const state = {
  tab: "dashboard", period: "today", folderId: null, opsFolderId: null, data: null, openId: null, authMode: "login",
  config: { allow_registration: false, password_reset: false }, resetToken: ""
};
const SESSION_EXPIRED = "Sua sessão expirou. Entre novamente.";
let lineChart = null;
let barChart = null;
let mixChart = null;
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
  document.querySelectorAll(".nav-btn").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  document.querySelectorAll(".pane").forEach((pane) => pane.classList.toggle("on", pane.id === "pane-" + tab));
  document.getElementById("heading").textContent = titles[tab];
  const controls = document.getElementById("dashboard-controls");
  if (controls) controls.classList.toggle("hidden", tab !== "dashboard");
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
function heroCard(kpis, series) {
  const card = el("article", "kpi-card kpi-left kpi-hero col-span-2 rounded-3xl border bg-card p-5");
  const top = el("div", "flex items-center justify-between gap-3");
  top.append(el("p", "text-sm text-mute", "Views"), deltaChip(kpis.views, prevOf("views")));
  const label = state.data.compare_label;
  card.append(
    top,
    el("p", "mt-1 font-mono text-5xl tracking-tight text-white", num(kpis.views)),
    el("p", "mt-1 text-xs text-mute", label ? label + ": " + num(prevOf("views")) + " views" : "Desde o primeiro post coletado")
  );
  if (series.length > 1) {
    const days = series.length;
    card.append(sparkline(series), el("p", "spark-caption text-[10px] text-mute", days <= 14 ? "últimos " + days + " dias" : "views por dia"));
  }
  return card;
}
function perPostCard(kpis) {
  const card = el("article", "kpi-card kpi-left col-span-2 rounded-3xl border bg-card p-5");
  const top = el("div", "flex items-center justify-between gap-3");
  top.append(el("p", "text-sm text-mute", "Views por post"), deltaChip(kpis.views_per_post, prevOf("views_per_post")));
  const posts = kpis.posts || 0;
  card.append(
    top,
    el("p", "mt-1 font-mono text-4xl tracking-tight text-white", num(kpis.views_per_post)),
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
  const gained = kpis.followers_gained;
  const tone = gained > 0 ? "gain-up" : gained < 0 ? "gain-down" : "text-white";
  card.append(
    el("p", "text-sm text-mute", "Seguidores no período"),
    el("p", "mt-1 font-mono text-4xl tracking-tight " + tone, signed(gained)),
    el("p", "mt-1 text-xs text-mute", "Total agora: " + num(kpis.followers))
  );
  return card;
}
function tile(label, value, current, previous, hint) {
  const card = el("article", "kpi-card flex min-h-24 flex-col items-center justify-center rounded-3xl border bg-card px-4 py-4 text-center");
  card.append(el("p", "text-xs text-mute", label), el("p", "mt-1 font-mono text-2xl tracking-tight text-white", value));
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
      borderColor: "rgba(8,12,25,.9)",
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
  if (mixChart) { mixChart.data = mixData; mixChart.options = options; mixChart.update("none"); }
  else mixChart = new Chart(document.getElementById("mix-chart"), { type: "doughnut", data: mixData, options });
}
function renderTopPost(post) {
  const host = document.getElementById("top-post-body");
  if (!post) {
    host.replaceChildren(el("p", "text-sm text-mute", "Nenhum post coletado no período. Clique em Atualizar Dados para ler as contas agora."));
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
function renderHealth(health) {
  const host = document.getElementById("health-body");
  const note = document.getElementById("health-note");
  const late = health.late_after_min >= 120 ? Math.round(health.late_after_min / 60) + " h" : health.late_after_min + " min";
  note.textContent = health.enabled
    ? "Atrasada é a conta sem coleta há mais de " + late + "."
    : "Coleta automática desligada: só coleta quando você clica em Atualizar Dados.";
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
  }
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
      label: "Views",
      data: data.series.map((row) => row.views),
      borderColor: "#ff2ec8",
      backgroundColor: "rgba(255,46,200,.16)",
      fill: true,
      tension: 0.35,
      pointRadius: data.series.length > 40 ? 0 : 3,
      borderWidth: 2
    }]
  };
  const names = data.by_account.map((row) => "@" + row.username);
  const barData = {
    labels: names.length ? names : ["—"],
    datasets: [
      { label: "Views", data: names.length ? data.by_account.map((row) => row.views) : [0], backgroundColor: "#ff2ec8", borderRadius: 8, maxBarThickness: 28 }
    ]
  };
  // Atualiza os gráficos no lugar (sem destruir/recriar), para não piscar no auto-refresh.
  if (lineChart) { lineChart.data = lineData; lineChart.update("none"); }
  else lineChart = new Chart(document.getElementById("line-chart"), { type: "line", data: lineData, options: chartOptions() });
  if (barChart) { barChart.data = barData; barChart.update("none"); }
  else barChart = new Chart(document.getElementById("bar-chart"), { type: "bar", data: barData, options: chartOptions() });
}
// Top 3 horários do build_suggestions (vem junto no /api/dashboard, com o mesmo período e pasta).
function renderSuggestions(suggestions) {
  const host = document.getElementById("best-times");
  const items = (suggestions && suggestions.items) || [];
  document.getElementById("best-times-note").textContent = (suggestions && suggestions.note) || "";
  host.replaceChildren(...items.map((item, index) => {
    const li = document.createElement("li");
    li.className = "rounded-2xl border px-4 py-4 text-center " + (index === 0 ? "border-neon/50 bg-neon/10" : "border-neon/20 bg-ink");
    const rank = document.createElement("p");
    rank.className = "text-[11px] uppercase tracking-[0.16em] " + (index === 0 ? "text-neon" : "text-mute");
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
    const card = document.createElement("button");
    card.type = "button";
    card.className = "glow flex w-full items-center justify-between gap-3 rounded-3xl border border-neon/20 bg-card px-5 py-6 text-left transition hover:border-neon hover:bg-neon/10";
    const info = document.createElement("div");
    info.className = "min-w-0 flex-1";
    const name = document.createElement("p");
    name.className = "truncate font-display text-2xl text-white";
    name.textContent = folder.name;
    const meta = document.createElement("p");
    meta.className = "mt-2 text-xs uppercase tracking-[0.14em] text-mute";
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
    const empty = document.createElement("p");
    empty.className = "text-sm text-mute";
    empty.textContent = "Nenhuma conta ativa nesta pasta. Adicione a primeira acima.";
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
    handle.textContent = "@" + account.username;
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
    loadHealth();                           // rota com login, fora da assinatura do dashboard
    renderLastCollect(data.last_collected_at);
    const sig = JSON.stringify(data);
    if (silent && sig === lastSig) return;  // nada mudou: não mexe no DOM (zero flicker)
    lastSig = sig;
    state.data = data;
    document.getElementById("banner").classList.add("hidden");
    document.getElementById("range-label").textContent = data.start + " → " + data.end;
    renderKpis(data.kpis);
    renderTopPost(data.top_post);
    renderCharts(data);
    renderSuggestions(data.suggestions);
    renderAccounts(data.accounts);
    renderFolders(data.folders);
    renderOpsView();
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
  badge.className = "shrink-0 rounded-full border px-2.5 py-1 text-[11px] uppercase tracking-[0.12em] st-" + (account.status || "none");
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
    dt.className = "text-[10px] uppercase tracking-[0.12em] text-mute";
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
    host.replaceChildren(...(accounts.length ? accounts.map(archivedCard) : [message("Nenhuma conta arquivada.")]));
  } catch (error) {
    host.replaceChildren(message(error.message || "Erro ao carregar."));
  }
}
// Coletores: contas descartáveis que o robô usa para ler. Proxy e cookies chegam mascarados.
const COLLECTOR_STATES = { ready: ["Pronto", "st-healthy"], paused: ["Pausado", "st-search_ban"], off: ["Desligado", "st-none"] };
const COLLECTOR_RESULTS = {
  ok: "Última leitura ok", limite: "X limitou (429)", bloqueio: "X bloqueou a leitura",
  sessao: "Sessão caiu: cole cookies novos", proxy: "Proxy não respondeu", erro: "Erro na última leitura"
};
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
  const head = document.createElement("div");
  head.className = "flex items-start justify-between gap-3";
  const name = document.createElement("p");
  name.className = "min-w-0 truncate font-medium text-white";
  name.textContent = "@" + collector.handle;
  const [label, style] = COLLECTOR_STATES[collector.state] || COLLECTOR_STATES.off;
  const badge = document.createElement("span");
  badge.className = "shrink-0 rounded-full border px-2.5 py-1 text-[11px] uppercase tracking-[0.12em] " + style;
  badge.textContent = collector.state === "paused" ? label + " até " + timeBR(collector.paused_until) : label;
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

  const actions = document.createElement("div");
  actions.className = "mt-4 flex flex-wrap gap-2";
  actions.append(
    collectorButton("Editar", () => editCollector(collector)),
    collectorButton(collector.is_active ? "Desligar" : "Ligar", () => saveCollector(collector.id, { is_active: !collector.is_active })),
  );
  if (collector.proxy) actions.append(collectorButton("Tirar proxy", () => saveCollector(collector.id, { proxy: "" })));
  actions.append(collectorButton("Remover", () => removeCollector(collector)));
  card.append(actions);
  return card;
}
async function loadCollectors() {
  const host = document.getElementById("collector-list");
  if (!host) return;
  const message = (text) => {
    const p = document.createElement("p");
    p.className = "text-sm text-mute";
    p.textContent = text;
    return p;
  };
  try {
    const collectors = await api("/api/collectors");
    host.replaceChildren(...(collectors.length ? collectors.map(collectorCard) : [message("Nenhum coletor. Sem coletores o robô não lê nenhuma timeline.")]));
  } catch (error) {
    host.replaceChildren(message(error.message || "Erro ao carregar."));
  }
}
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
// em app_settings a cada minuto (cron). "Atualizar Dados" grava um pedido e espera a rodada terminar.
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
  await loadConfig();
  setAuthMode("login");
  // Link do Telegram: /#reset=<token>. No hash para o token não ir ao servidor nem aos logs.
  const reset = location.hash.match(/^#reset=([\w\-.]+)$/);
  if (reset) {
    state.resetToken = reset[1];
    setAuthMode("reset");
    showAuth(true);
    return;
  }
  if (!token()) {
    showAuth(true);
    return;
  }
  if (tokenExpired()) {
    sessionExpired();
    return;
  }
  try {
    paintUser(await api("/api/me"));
    showAuth(false);
    setTab("dashboard");
    await load();
  } catch (err) {
    if (err.message === SESSION_EXPIRED) return;
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

boot();
