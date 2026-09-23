const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const scripts = document.querySelector("#scripts");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const readingView = document.querySelector("#readingView");
const transcriptView = document.querySelector("#transcriptView");
const transcriptScope = document.querySelector("#transcriptScope");

function paint(list) {
  rows.innerHTML = list
    .map((r) => {
      const scriptLinks = r.transcript_ids.length
        ? r.transcript_ids
            .map(
              (id) =>
                `<a href="#" class="openScripts" data-reading="${r.id}">#${id}</a>`,
            )
            .join(" ")
        : "—";
      // 改正班测仅检查员可用；旁观账号既看不到输入也无法提交。
      const fixCell =
        role === "writer"
          ? `<input class="fixv" type="number" step="0.01" value="${r.ch4_pct}" style="width:72px" />
             <button class="fix" data-id="${r.id}">改正</button>`
          : "—";
      return `<tr>
        <td>${r.site}</td>
        <td class="curv" data-id="${r.id}">${r.ch4_pct}</td>
        <td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td>
        <td>${r.note}</td>
        <td>${scriptLinks}</td>
        <td>${fixCell}</td>
      </tr>`;
    })
    .join("");
}

function paintScripts(list) {
  scripts.innerHTML = list.length
    ? list
        .map(
          (t) => `<tr>
            <td>#${t.id}</td>
            <td>${t.reading_id}</td>
            <td>${t.site}</td>
            <td>${t.ch4_pct.toFixed(2)}%</td>
            <td>${t.note_text}</td>
            <td><code>${t.checksum}</code></td>
            <td>${t.created_at.replace("T", " ").slice(0, 19)} UTC</td>
            <td><button class="dl" data-id="${t.id}" data-sum="${t.checksum}">下载抄本</button></td>
          </tr>`,
        )
        .join("")
    : `<tr><td colspan="8">暂无抄本；只有达到报警线的推送才会生成。</td></tr>`;
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
  connect();
  load();
}

async function load() {
  paint(await api("/api/readings"));
}

function showReadings() {
  transcriptView.hidden = true;
  readingView.hidden = false;
  load();
}

async function openScripts(readingId) {
  readingView.hidden = true;
  transcriptView.hidden = false;
  transcriptScope.textContent = readingId
    ? `班测 ${readingId} 的抄本详情（改正浓度不改变这里的冻结值）`
    : "全部报警推送抄本";
  const query = readingId ? `?reading_id=${readingId}` : "";
  paintScripts(await api(`/api/transcripts${query}`));
}

rows.addEventListener("click", (e) => {
  if (e.target.classList.contains("openScripts")) {
    e.preventDefault();
    openScripts(Number(e.target.dataset.reading));
  }
});

// 检查员改正班测浓度：只改班测，不触发推送、不动已生成抄本。
rows.addEventListener("click", async (e) => {
  if (!e.target.classList.contains("fix")) return;
  const id = Number(e.target.dataset.id);
  const value = Number(e.target.closest("tr").querySelector(".fixv").value);
  try {
    await api(`/api/readings/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ ch4_pct: value }),
    });
    live.textContent = `班测 ${id} 浓度已改正；已有抄本仍保持推送当时的样子。`;
    await load();
  } catch (err) {
    live.textContent = err.message;
  }
});

// 下载必须带令牌，故走 fetch 取文本后另存，旁观账号同样可下载。
scripts.addEventListener("click", async (e) => {
  if (!e.target.classList.contains("dl")) return;
  const id = e.target.dataset.id;
  const res = await fetch(`/api/transcripts/${id}/download`, {
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
  a.download = `transcript-${id}-${e.target.dataset.sum}.txt`;
  a.click();
  URL.revokeObjectURL(url);
});

document.querySelector("#allTranscripts").onclick = () => openScripts(null);
document.querySelector("#backReadings").onclick = showReadings;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}${
      row.transcript_id ? `（已生成抄本 #${row.transcript_id}）` : ""
    }`;
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

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
