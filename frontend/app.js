const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const transRows = document.querySelector("#transcripts");
const detailBox = document.querySelector("#transcript-detail");
const detailBody = document.querySelector("#transcript-body");
const live = document.querySelector("#live");
const form = document.querySelector("#form");

function paint(list) {
  rows.innerHTML = list
    .map((r) => {
      const action =
        role === "writer"
          ? `<button type="button" data-correct="${r.id}" data-site="${r.site}">改正</button>`
          : "";
      return `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td><td>${action}</td></tr>`;
    })
    .join("");
}

function paintTranscripts(list) {
  transRows.innerHTML = list
    .map(
      (t) =>
        `<tr><td>${t.id}</td><td>${t.site}</td><td>${t.ch4_pct}</td><td class="${t.level === "报警" ? "alarm" : "ok"}">${t.level}</td><td><code>${t.checksum}</code></td><td>${t.created_at.replace("T", " ").slice(0, 19)}</td>` +
        `<td><button type="button" data-detail="${t.id}">详情</button> <button type="button" data-download="${t.id}">下载</button></td></tr>`,
    )
    .join("");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  document.querySelector("#correct-col").hidden = role !== "writer";
  connect();
  load();
}

async function load() {
  paint(await api("/api/readings"));
  paintTranscripts(await api("/api/transcripts"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load();
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

rows.addEventListener("click", async (e) => {
  const id = e.target.getAttribute && e.target.getAttribute("data-correct");
  if (!id) return;
  const input = prompt(`改正「${e.target.dataset.site}」班测浓度（甲烷 %）`);
  if (input === null) return;
  const ch4 = Number(input);
  if (!Number.isFinite(ch4) || ch4 < 0) {
    live.textContent = "浓度需为不小于 0 的数字";
    return;
  }
  try {
    await api(`/api/readings/${id}`, { method: "PATCH", body: JSON.stringify({ ch4_pct: ch4 }) });
    live.textContent = "班测浓度已改正；已推送的抄本保持原样";
    load();
  } catch (err) {
    live.textContent = err.message;
  }
});

transRows.addEventListener("click", async (e) => {
  const detailId = e.target.getAttribute && e.target.getAttribute("data-detail");
  const downloadId = e.target.getAttribute && e.target.getAttribute("data-download");
  if (detailId) {
    const t = await api(`/api/transcripts/${detailId}`);
    detailBody.textContent = `${t.body}\n校验短串：${t.checksum}\n`;
    detailBox.hidden = false;
  } else if (downloadId) {
    // 带令牌下载抄本：正文与校验短串均为推送当时的存档
    const res = await fetch(`/api/transcripts/${downloadId}/download`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      live.textContent = "下载失败";
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `transcript-${downloadId}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }
});

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
