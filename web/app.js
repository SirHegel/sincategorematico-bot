const $ = (s) => document.querySelector(s),
  login = $("#login"),
  dashboard = $("#dashboard"),
  error = $("#login-error");
const escapeText = (value) => String(value ?? "");
async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = {};
  try {
    data = await response.json();
  } catch {}
  if (!response.ok)
    throw new Error(data.error || "No fue posible completar la operación");
  return data;
}
const ESTADOS = {
  pending: "por revisar",
  approved: "aprobado",
  publishing: "enviando",
  uncertain: "verificar en LinkedIn",
  published: "publicado",
  rejected: "descartado",
  failed: "fallido",
  discarded: "en reescritura",
};
function renderCounters(counts) {
  const list = $("#counters");
  list.replaceChildren();
  [
    ["Noticias sin redactar", counts.news],
    ["Por revisar", counts.pending],
    ["Aprobados", counts.approved],
    ["Publicados", counts.published],
    ["Fallidos", counts.failed],
    ["Resultado incierto", counts.uncertain],
  ].forEach(([label, value]) => {
    const li = document.createElement("li"),
      name = document.createElement("span"),
      amount = document.createElement("b");
    name.textContent = label;
    amount.textContent = value;
    li.append(name, amount);
    list.append(li);
  });
}
function renderQueue(drafts, pending) {
  $("#queue-count").textContent = `${pending} por revisar`;
  const list = $("#queue-list");
  list.replaceChildren();
  if (!drafts.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "Todavía no hay borradores. El motor los redactará al encontrar noticias.";
    list.append(li);
    return;
  }
  drafts.forEach((draft) => {
    const li = document.createElement("li");
    li.className = `draft ${draft.state}`;
    const head = document.createElement("div");
    head.className = "draft-head";
    const title = document.createElement("b");
    title.textContent = `#${draft.id} · ${escapeText(draft.title)}`;
    const state = document.createElement("span");
    state.className = "pill";
    state.textContent = ESTADOS[draft.state] || draft.state;
    head.append(title, state);
    const body = document.createElement("p");
    body.textContent = escapeText(draft.preview);
    li.append(head, body);
    if (draft.link) {
      const link = document.createElement("a");
      link.href = draft.link;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = draft.link;
      li.append(link);
    }
    if (draft.url) {
      const posted = document.createElement("a");
      posted.href = draft.url;
      posted.target = "_blank";
      posted.rel = "noreferrer noopener";
      posted.textContent = "Ver la publicación en LinkedIn";
      li.append(posted);
    }
    if (draft.error) {
      const error = document.createElement("small");
      error.className = "draft-error";
      error.textContent = escapeText(draft.error);
      li.append(error);
    }
    if (draft.state === "pending") {
      const actions = document.createElement("div");
      actions.className = "draft-actions";
      const approve = document.createElement("button");
      approve.className = "primary";
      approve.dataset.draft = draft.id;
      approve.dataset.action = "approve";
      approve.textContent = "Publicar";
      const reject = document.createElement("button");
      reject.className = "secondary";
      reject.dataset.draft = draft.id;
      reject.dataset.action = "reject";
      reject.textContent = "Descartar";
      actions.append(approve, reject);
      li.append(actions);
    } else if (["failed", "uncertain"].includes(draft.state)) {
      const actions = document.createElement("div");
      actions.className = "draft-actions";
      const retry = document.createElement("button");
      retry.className = "secondary";
      retry.dataset.draft = draft.id;
      retry.dataset.action = "retry";
      retry.textContent = draft.state === "uncertain"
        ? "Ya verifiqué: reintentar"
        : "Reintentar";
      actions.append(retry);
      if (draft.state === "uncertain") {
        const confirm = document.createElement("button");
        confirm.className = "primary";
        confirm.dataset.draft = draft.id;
        confirm.dataset.action = "confirm";
        confirm.textContent = "Ya existe: confirmar";
        actions.append(confirm);
      }
      li.append(actions);
    }
    list.append(li);
  });
}
function render(data) {
  const linkedin = data.linkedin || {};
  const counts = data.counts || {};
  const engine = data.engine || { alive: false, status: "detenido" };
  login.hidden = true;
  dashboard.hidden = false;
  $("#bot-state").textContent = data.initialized ? "Conectado" : "Pendiente";
  $("#owner-state").textContent = data.owner
    ? "Propietario verificado"
    : "Sin propietario";
  $("#publish-state").textContent = !engine.alive
    ? "Motor detenido"
    : data.paused
      ? "En pausa"
      : "En marcha";
  $("#max-posts").textContent = `${data.max_posts} piezas`;
  $("#timezone").textContent = `${data.timezone} · ${data.window}`;
  $("#linkedin-state").textContent = linkedin.linked
    ? data.dry_run
      ? "Vinculado (simulación)"
      : "Publicando"
    : "Sin vincular";
  $("#linkedin-detail").textContent = linkedin.linked
    ? `${linkedin.kind} · caduca en ${linkedin.days_left} días`
    : "Ejecuta configure_linkedin.py en el equipo";
  $("#gate").textContent = `Ahora mismo: ${data.gate}`;
  $("#setting-max").value = data.max_posts;
  $("#setting-timezone").value = data.timezone;
  const [inicio, fin] = data.window.split("–");
  $("#setting-window-start").value = inicio;
  $("#setting-window-end").value = fin;
  $("#setting-approval").value = data.approval_required ? "1" : "0";
  $("#setting-mode").value = data.dry_run ? "1" : "0";
  $("#pause").disabled = data.paused;
  $("#resume").disabled = !data.paused;
  renderCounters(counts);
  renderQueue(data.drafts || [], counts.pending || 0);
  const list = $("#activity-list");
  list.replaceChildren();
  if (!data.activity.length) {
    const li = document.createElement("li");
    li.textContent = "Aún no hay actividad registrada";
    list.append(li);
  }
  data.activity.forEach((item) => {
    const li = document.createElement("li"),
      dot = document.createElement("i"),
      text = document.createElement("span"),
      time = document.createElement("time");
    text.textContent = escapeText(item.message);
    time.textContent = new Date(item.created_at * 1000).toLocaleString(
      "es-CO",
      { dateStyle: "short", timeStyle: "short" },
    );
    li.append(dot, text, time);
    list.append(li);
  });
}
async function load() {
  try {
    render(await request("/api/status"));
  } catch {
    login.hidden = false;
    dashboard.hidden = true;
  }
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  error.textContent = "";
  try {
    await request("/api/login", {
      method: "POST",
      body: JSON.stringify({ token: $("#token").value }),
    });
    $("#token").value = "";
    await load();
  } catch (err) {
    error.textContent = err.message;
  }
});
async function control(action) {
  await request("/api/control", {
    method: "POST",
    body: JSON.stringify({ action }),
  });
  await load();
}
$("#queue-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-draft]");
  if (!button) return;
  let reference = "";
  if (button.dataset.action === "confirm") {
    reference = window.prompt(
      "Pega la URN o URL de la publicación que comprobaste en LinkedIn:",
    )?.trim() || "";
    if (!reference) return;
  }
  button.disabled = true;
  try {
    await request("/api/draft", {
      method: "POST",
      body: JSON.stringify({
        id: Number(button.dataset.draft),
        action: button.dataset.action,
        reference,
      }),
    });
  } finally {
    await load();
  }
});
$("#pause").addEventListener("click", () => control("pause"));
$("#resume").addEventListener("click", () => control("resume"));
$("#refresh").addEventListener("click", load);
$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("#settings-status");
  status.textContent = "Guardando…";
  try {
    await request("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        max_posts: Number($("#setting-max").value),
        timezone: $("#setting-timezone").value.trim(),
        window_start: $("#setting-window-start").value,
        window_end: $("#setting-window-end").value,
        approval_required: $("#setting-approval").value === "1",
        dry_run: $("#setting-mode").value === "1",
      }),
    });
    status.textContent = "Configuración guardada correctamente.";
    await load();
  } catch (error) {
    status.textContent = error.message;
  }
});
load();
setInterval(() => {
  if (!dashboard.hidden) load();
}, 15000);
