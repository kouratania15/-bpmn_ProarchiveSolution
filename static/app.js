const API_BASE = "";

const state = {
  processes: [],
  currentProcessId: null,
  currentProcessName: null,
  conversation: [], // { role: 'user' | 'assistant', text: str, invalid?: bool, pending?: bool }
  status: null,      // 'valid' | 'invalid' | null
  bpmnXml: null,
  pendingDeleteId: null, // id du process en attente de confirmation de suppression
};

let viewer = null;

function getViewer() {
  if (!viewer) {
    viewer = new BpmnJS({ container: "#canvas" });
  }
  return viewer;
}

const CANVAS_EMPTY_DEFAULT_TEXT = "Aucun diagramme à afficher pour l'instant";

async function renderDiagram(xml) {
  const empty = document.getElementById("canvas-empty");
  if (!xml) {
    // Sans ce clear(), le SVG du diagramme précédemment affiché reste visible
    // à l'écran : cacher/montrer le message vide ne touche jamais au contenu
    // déjà importé dans le viewer bpmn-js, qui persiste tant qu'on ne lui dit
    // pas explicitement de se vider.
    if (viewer) {
      viewer.clear();
    }
    empty.textContent = CANVAS_EMPTY_DEFAULT_TEXT;
    empty.style.display = "flex";
    return;
  }
  empty.textContent = CANVAS_EMPTY_DEFAULT_TEXT;
  empty.style.display = "none";
  try {
    await getViewer().importXML(xml);
    // requestAnimationFrame : laisse le navigateur terminer la mise en page du
    // conteneur avant de calculer le zoom — appeler zoom('fit-viewport')
    // immédiatement après importXML() peut mesurer un conteneur encore à 0x0
    // et caler le diagramme en haut à gauche au lieu de le centrer.
    requestAnimationFrame(() => {
      getViewer().get("canvas").zoom("fit-viewport");
    });
  } catch (err) {
    console.error("Erreur de rendu BPMN :", err);
    empty.textContent = "Le diagramme n'a pas pu être affiché.";
    empty.style.display = "flex";
  }
}

function setStatusBadge(status) {
  const badge = document.getElementById("status-badge");
  badge.className = "badge";
  if (status === "valid") {
    badge.textContent = "valide";
    badge.classList.add("badge-valid");
  } else if (status === "invalid") {
    badge.textContent = "invalide";
    badge.classList.add("badge-invalid");
  } else {
    badge.textContent = "";
  }
}

function setActionsEnabled(enabled) {
  document.getElementById("export-btn").disabled = !enabled || !state.bpmnXml;
  document.getElementById("regenerate-btn").disabled = !enabled || !state.currentProcessId;
}

function renderConversation() {
  const list = document.getElementById("conversation-list");
  list.innerHTML = "";
  if (state.conversation.length === 0) {
    const p = document.createElement("p");
    p.className = "conversation-empty";
    p.textContent = "Décrivez un processus métier pour générer votre premier diagramme BPMN.";
    list.appendChild(p);
    return;
  }
  for (const msg of state.conversation) {
    const bubble = document.createElement("div");
    bubble.className = `bubble bubble-${msg.role}`;
    if (msg.invalid) bubble.classList.add("bubble-invalid");
    if (msg.pending) bubble.classList.add("bubble-pending");
    bubble.textContent = msg.text;
    list.appendChild(bubble);
  }
  list.scrollTop = list.scrollHeight;
}

function dateGroupLabel(isoDate) {
  const date = new Date(isoDate);
  const now = new Date();
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((startOfDay(now) - startOfDay(date)) / 86400000);
  if (diffDays <= 0) return "Aujourd'hui";
  if (diffDays === 1) return "Hier";
  if (diffDays <= 7) return "7 derniers jours";
  return "Plus ancien";
}

function renderProcessList() {
  const container = document.getElementById("process-list");
  container.innerHTML = "";
  let lastGroup = null;
  for (const p of state.processes) {
    const group = dateGroupLabel(p.created_at);
    if (group !== lastGroup) {
      const label = document.createElement("div");
      label.className = "process-group-label";
      label.textContent = group;
      container.appendChild(label);
      lastGroup = group;
    }
    const row = document.createElement("div");
    row.className = "process-row";

    if (state.pendingDeleteId === p.id) {
      // Pas de window.confirm() : confirm()/prompt() se sont révélés non
      // supportés dans le contexte navigateur/webview utilisé ici — une
      // confirmation inline dans la sidebar évite le même problème pour une
      // action destructive et irréversible.
      row.classList.add("confirm-delete");
      const label = document.createElement("span");
      label.className = "process-confirm-label";
      label.textContent = "Supprimer ce chat ?";
      const yesBtn = document.createElement("button");
      yesBtn.className = "process-confirm-btn process-confirm-yes";
      yesBtn.textContent = "Supprimer";
      yesBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteProcess(p.id);
      });
      const noBtn = document.createElement("button");
      noBtn.className = "process-confirm-btn process-confirm-no";
      noBtn.textContent = "Annuler";
      noBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        state.pendingDeleteId = null;
        renderProcessList();
      });
      row.appendChild(label);
      row.appendChild(yesBtn);
      row.appendChild(noBtn);
    } else {
      const item = document.createElement("button");
      item.className = "process-item" + (p.id === state.currentProcessId ? " active" : "");
      item.textContent = p.name;
      item.title = p.name;
      item.addEventListener("click", () => selectProcess(p.id, p.name));
      row.appendChild(item);

      const delBtn = document.createElement("button");
      delBtn.className = "process-delete-btn";
      delBtn.textContent = "×";
      delBtn.title = "Supprimer ce chat";
      delBtn.setAttribute("aria-label", "Supprimer ce chat");
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        state.pendingDeleteId = p.id;
        renderProcessList();
      });
      row.appendChild(delBtn);
    }

    container.appendChild(row);
  }
}

async function deleteProcess(id) {
  try {
    const res = await fetch(`${API_BASE}/processes/${id}`, { method: "DELETE" });
    if (!res.ok) {
      console.error("Impossible de supprimer le processus", id, res.status);
      state.pendingDeleteId = null;
      renderProcessList();
      return;
    }
  } catch (err) {
    console.error("Erreur réseau lors de la suppression :", err);
    state.pendingDeleteId = null;
    renderProcessList();
    return;
  }
  state.pendingDeleteId = null;
  const wasCurrent = state.currentProcessId === id;
  await loadProcessList();
  if (wasCurrent) {
    startNewProcess();
  }
}

async function loadProcessList() {
  try {
    const res = await fetch(`${API_BASE}/processes`);
    state.processes = await res.json();
  } catch (err) {
    console.error("Impossible de charger la liste des processus :", err);
    state.processes = [];
  }
  renderProcessList();
}

async function selectProcess(id, name) {
  state.currentProcessId = id;
  state.currentProcessName = name;
  document.getElementById("process-title").textContent = name;
  renderProcessList();

  const res = await fetch(`${API_BASE}/processes/${id}`);
  if (!res.ok) {
    console.error("Impossible de charger le processus", id);
    return;
  }
  const data = await res.json();
  state.status = data.status;
  state.bpmnXml = data.bpmn_xml;
  state.conversation = [];
  for (const entry of data.conversation) {
    state.conversation.push({ role: "user", text: entry.instruction_text });
    state.conversation.push({ role: "assistant", text: entry.message });
  }
  setStatusBadge(state.status);
  setActionsEnabled(true);
  renderConversation();
  await renderDiagram(state.bpmnXml);
}

function startNewProcess() {
  state.currentProcessId = null;
  state.currentProcessName = null;
  state.conversation = [];
  state.status = null;
  state.bpmnXml = null;
  document.getElementById("process-title").textContent = "Nouveau processus";
  setStatusBadge(null);
  setActionsEnabled(true);
  renderConversation();
  renderProcessList();
  renderDiagram(null);
  document.getElementById("message-input").focus();
}

async function handleSubmit(event) {
  event.preventDefault();
  const input = document.getElementById("message-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  input.disabled = true;
  document.querySelector("#message-form button").disabled = true;

  state.conversation.push({ role: "user", text });
  const pending = { role: "assistant", text: "Génération en cours...", pending: true };
  state.conversation.push(pending);
  renderConversation();

  try {
    let res, data;
    if (!state.currentProcessId) {
      // Le nom du processus est entièrement généré côté serveur (à partir du
      // Logic-Core produit par le pipeline) — jamais saisi ni suggéré côté client.
      res = await fetch(`${API_BASE}/processes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      data = await res.json();
      if (res.ok && data.process_id) {
        state.currentProcessId = data.process_id;
        state.currentProcessName = data.process_name;
        document.getElementById("process-title").textContent = data.process_name;
        loadProcessList();
      }
    } else {
      res = await fetch(`${API_BASE}/processes/${state.currentProcessId}/amend`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction: text }),
      });
      data = await res.json();
    }

    pending.pending = false;
    if (!res.ok) {
      pending.text = `Erreur : ${data.detail || "une erreur inattendue est survenue."}`;
      pending.invalid = true;
    } else {
      pending.text = data.message;
      pending.invalid = data.status !== "valid";
      state.status = data.status;
      setStatusBadge(state.status);
      if (data.bpmn_xml) {
        state.bpmnXml = data.bpmn_xml;
        await renderDiagram(state.bpmnXml);
      }
      setActionsEnabled(true);
    }
  } catch (err) {
    pending.pending = false;
    pending.invalid = true;
    pending.text = `Erreur réseau : ${err.message}`;
  } finally {
    renderConversation();
    input.disabled = false;
    document.querySelector("#message-form button").disabled = false;
    input.focus();
  }
}

function exportBpmn() {
  if (!state.bpmnXml) return;
  const blob = new Blob([state.bpmnXml], { type: "application/xml" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${(state.currentProcessName || "process").replace(/\s+/g, "_")}.bpmn`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function regenerate() {
  // Renvoie la dernière instruction utilisateur comme un nouvel amendement —
  // "Régénérer" = redemander une génération, sans dupliquer la logique
  // d'amendement déjà gérée par handleSubmit.
  if (!state.currentProcessId) return;
  const lastUser = [...state.conversation].reverse().find((m) => m.role === "user");
  if (!lastUser) return;
  const input = document.getElementById("message-input");
  input.value = lastUser.text;
  document.getElementById("message-form").requestSubmit();
}

function togglePanel(panelEl, expandBtnEl) {
  const collapsed = panelEl.classList.toggle("collapsed");
  expandBtnEl.hidden = !collapsed;
}

document.getElementById("new-process-btn").addEventListener("click", startNewProcess);
document.getElementById("message-form").addEventListener("submit", handleSubmit);
document.getElementById("export-btn").addEventListener("click", exportBpmn);
document.getElementById("regenerate-btn").addEventListener("click", regenerate);

// Repli/dépli de la sidebar et de la conversation, chacun via son propre
// bouton flèche — libère de l'espace pour le canvas du diagramme.
const sidebarEl = document.getElementById("sidebar");
const sidebarExpandBtn = document.getElementById("sidebar-expand");
document.getElementById("sidebar-toggle").addEventListener("click", () => togglePanel(sidebarEl, sidebarExpandBtn));
sidebarExpandBtn.addEventListener("click", () => togglePanel(sidebarEl, sidebarExpandBtn));

const conversationEl = document.getElementById("conversation-panel");
const conversationExpandBtn = document.getElementById("conversation-expand");
document.getElementById("conversation-toggle").addEventListener("click", () => togglePanel(conversationEl, conversationExpandBtn));
conversationExpandBtn.addEventListener("click", () => togglePanel(conversationEl, conversationExpandBtn));

setActionsEnabled(false);
loadProcessList();
startNewProcess();
