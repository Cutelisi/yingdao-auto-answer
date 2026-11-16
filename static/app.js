const state = {
  view: "pending",
  solved: "all",
  sort: "new",
  items: [],
  currentUuid: null,
  draftText: "",
  gate: null,
  stats: null,
  config: null,
  edits: {},
  busy: { collect: false, draft: false, gate: false, send: false },
};

const $ = (id) => document.getElementById(id);

const esc = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

function md(src) {
  const parts = String(src || "").split("```");
  return parts
    .map((p, i) => {
      if (i % 2 === 1) return "<pre>" + esc(p.replace(/^\n/, "")) + "</pre>";
      let s = esc(p);
      s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
      s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/\n/g, "<br>");
      return s;
    })
    .join("");
}

function relTime(str) {
  if (!str) return "";
  const t = new Date(str.replace(/-/g, "/"));
  if (isNaN(t.getTime())) return str;
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
  return str.slice(0, 10);
}

function toast(msg, isErr) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = "toast"), 2600);
}

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opts);
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    throw new Error("服务端返回异常（HTTP " + res.status + "）");
  }
  if (!res.ok || data.ok === false) throw new Error(data.error || "请求失败");
  return data;
}

/* ---------------- 列表 ---------------- */

function statusLabel(s) {
  return {
    new: ["待处理", "wait"],
    drafted: ["已生成", "info"],
    gated: ["质检通过", "ok"],
    needfix: ["待修改", "bad"],
    sent: ["已发送", "ok"],
    skipped: ["已跳过", "some"],
  }[s] || [s || "未知", "some"];
}

function renderList() {
  const box = $("list");
  if (!state.items.length) {
    box.innerHTML =
      '<div class="empty" style="height:220px"><h2>暂无问题</h2><p>点右上角「采集最新问题」，从影刀社区拉取最新提问。</p></div>';
  } else {
    box.innerHTML = state.items
      .map((q) => {
        const [label, cls] = statusLabel(q.status);
        const noAns = q.answer_count === 0;
        const itemCls = [
          "qitem",
          q.uuid === state.currentUuid ? "active" : "",
          noAns ? "no-answer" : "",
          q.is_accept ? "is-solved" : "",
          q.status === "sent" || q.status === "skipped" ? "sent" : "",
        ].join(" ");
        return `<div class="${itemCls}" data-uuid="${q.uuid}">
          <div class="qitem-title">${esc(q.title)}</div>
          <div class="qitem-meta">
            ${
              q.is_accept
                ? '<span class="badge solved">✓ 已解决</span>'
                : '<span class="badge unsolved">未解决</span>'
            }
            <span class="badge ${noAns ? "none" : "some"}">${noAns ? "0 回答" : q.answer_count + " 回答"}</span>
            ${q.status === "new" ? "" : `<span class="badge ${cls}">${label}</span>`}
            <span>${relTime(q.create_time)}</span>
            <span>· ${q.creator || "匿名"}</span>
          </div>
        </div>`;
      })
      .join("");
  }

  $("footLeft").textContent = state.stats
    ? `已采集 ${state.stats.total} · 未解决 ${state.stats.unsolved} · 已解决 ${state.stats.solved}`
    : "未加载";
  $("footRight").textContent = state.stats
    ? `待处理 ${state.stats.pending != null ? state.stats.pending : state.stats.new}`
    : "";
}

function renderChips() {
  const s = state.stats;
  const cfg = state.config;
  const chips = [];
  if (cfg) {
    const hasKey = cfg.llm && cfg.llm.has_key;
    chips.push(
      `<span class="chip ${hasKey ? "on" : "warn"}">${hasKey ? "模型已接通 · " + esc(cfg.llm.model || "") : "演示模式（未填 API Key）"}</span>`
    );
    const ready = cfg.send && cfg.send.auto_ready;
    chips.push(
      `<span class="chip ${ready ? "on" : ""}" title="${ready ? "已配置登录凭证，可一键发送" : "未配置登录凭证，只能复制后手动发布"}">${ready ? "一键发送已就绪" : "仅手动发布"}</span>`
    );
  }
  if (s)
    chips.push(
      `<span class="chip" title="库内待处理问题总数，每次采集后实时刷新">待处理 ${
        s.pending != null ? s.pending : s.new + s.doing
      }</span>`
    );
  $("chips").innerHTML = chips.join("");
}

async function loadStats() {
  try {
    const r = await api("/api/stats");
    state.stats = r.stats;
  } catch (e) {
    /* ignore */
  }
  renderChips();
  renderList();
}

async function loadList() {
  const r = await api(
    `/api/questions?view=${state.view}&solved=${state.solved}&sort=${state.sort}`
  );
  state.items = r.items;
  if (state.currentUuid && !state.items.some((i) => i.uuid === state.currentUuid)) {
    // 当前问题因状态变化离开当前视图，保持选中但不报错
  }
}

async function collect() {
  state.busy.collect = true;
  const btn = $("btnCollect");
  btn.disabled = true;
  btn.innerHTML = '<span class="loading"></span>采集中';
  try {
    const r = await api("/api/collect", {});
    if (r.stats) state.stats = r.stats;
    await loadList();
    renderChips();
    renderList();
    const pending = r.stats ? r.stats.pending : state.items.length;
    toast(`采集完成：新增 ${r.added} 条，更新 ${r.refreshed} 条 · 待处理 ${pending} 条`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.busy.collect = false;
    btn.disabled = false;
    btn.textContent = "采集最新问题";
  }
}

/* ---------------- 工作区 ---------------- */

function renderWork() {
  const work = $("work");
  const item = state.items.find((i) => i.uuid === state.currentUuid);
  const layout = document.querySelector(".layout");
  const hasAny = !!(state.stats && state.stats.total > 0);

  // 窄屏：列表里有内容且没选中任何一条时，让列表占满整屏，不露出下方空白提示
  if (layout) layout.classList.toggle("list-only", !item && state.items.length > 0);

  if (!item) {
    if (!hasAny) {
      // 真的什么都没采到，才显示采集引导
      work.innerHTML = `<div class="empty">
        <h2>还没有采集到问题</h2>
        <p>点下面这个按钮，从影刀社区拉取最新提问。<br>采到的帖子会按「无人回答优先」排进列表。</p>
        <button class="btn primary" id="btnCollectEmpty">采集最新问题</button>
      </div>`;
      const b = $("btnCollectEmpty");
      if (b) b.onclick = collect;
    } else if (!state.items.length) {
      work.innerHTML = `<div class="empty">
        <h2>当前筛选下没有问题</h2>
        <p>换一个左侧标签看看，或者点右上角「采集最新问题」刷新。</p>
      </div>`;
    } else {
      // 库里有数据、当前视图也有列表，只是还没点开某一条：只留轻提示
      work.innerHTML = `<div class="empty hint-only">
        <p>从左侧列表选择一条问题，开始生成与质检。</p>
      </div>`;
    }
    return;
  }

  const noAns = item.answer_count === 0;
  const draftStep = state.busy.draft ? "active" : state.draftText ? "done" : "";
  const gateCls = state.gate
    ? state.gate.verdict === "PASS"
      ? "done"
      : "bad"
    : "";
  const sendCls = item.status === "sent" ? "done" : "";

  const answersHtml = item.draft
    ? ""
    : "";

  work.innerHTML = `
  <article class="card">
    <div class="card-head">
      <div class="step-no done">1</div>
      <div class="card-title">问题原文</div>
      <div class="card-hint">
        ${noAns ? '<span class="badge none">无人回答 · 优先处理</span>' : `<span class="badge some">已有 ${item.answer_count} 个回答</span>`}
      </div>
    </div>
    <div class="card-body">
      <h2 class="q-title">${esc(item.title)}</h2>
      <div class="q-meta">
        ${item.is_accept ? '<span class="badge solved">✓ 已解决</span>' : '<span class="badge unsolved">未解决</span>'}
        <span>${esc(item.creator || "匿名")}</span>
        <span>${esc(item.create_time || "")}</span>
        <span>浏览 ${item.browse_count || 0}</span>
        <a href="${esc(item.url)}" target="_blank" rel="noopener">打开原帖 ↗</a>
      </div>
      ${
        item.content
          ? `<div class="q-content">${esc(item.content)}</div>`
          : `<div class="q-content" style="color:#94a3b8">（作者只发了标题，没有正文。生成答案时应主动索要截图或报错信息）</div>`
      }
      ${item.images && item.images.length ? `<div class="q-images">${item.images
            .slice(0, 4)
            .map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener"><img src="${esc(u)}" alt=""></a>`)
            .join("")}</div>` : ""}
      <div id="answersBox">
        <div class="thinking" style="padding:6px 0"><span class="dot"></span><span class="dot"></span><span class="dot"></span>&nbsp;正在读取该帖已有回答…</div>
      </div>
    </div>
  </article>

  <article class="card">
    <div class="card-head">
      <div class="step-no ${draftStep}">2</div>
      <div class="card-title">AI 生成答案</div>
      <div class="card-hint">${item.draft ? esc(item.draft.model || "") + " · " + relTime(item.draft.created_at) : "读取问题 + 已有回答后生成"}</div>
    </div>
    <div class="card-body">
      ${
        state.busy.draft
          ? `<div class="thinking"><span class="dot"></span><span class="dot"></span><span class="dot"></span>模型正在阅读问题并起草答案…</div>`
          : state.draftText
          ? `<textarea class="editor" id="draftEditor" placeholder="答案正文">${esc(state.draftText)}</textarea>
             <div class="row" style="margin-top:10px">
               <button class="btn" id="btnRedraft">重新生成</button>
               <span class="spacer"></span>
               <span style="font-size:11.5px;color:#94a3b8">可直接编辑，编辑内容会进入质检</span>
               <button class="btn accent" id="btnGate">运行质量闸门 →</button>
             </div>`
          : `<div class="row">
               <button class="btn accent" id="btnDraft">让 AI 生成答案</button>
               <span style="font-size:11.5px;color:#94a3b8">会先读原帖正文和已有回答，避免重复别人已经说过的内容</span>
             </div>`
      }
    </div>
  </article>

  <article class="card">
    <div class="card-head">
      <div class="step-no ${gateCls}">3</div>
      <div class="card-title">质量闸门</div>
      <div class="card-hint">${state.gate ? "模型评审 + 5 项硬规则" : "把生成的答案再交给模型审一遍"}</div>
    </div>
    <div class="card-body" id="gateBody">
      ${
        state.busy.gate
          ? `<div class="thinking"><span class="dot"></span><span class="dot"></span><span class="dot"></span>正在审核答案质量…</div>`
          : state.gate
          ? renderGate(state.gate)
          : `<div class="row"><button class="btn" id="btnGate2" ${state.draftText ? "" : "disabled"}>开始质检</button>
             <span style="font-size:11.5px;color:#94a3b8">${state.draftText ? "" : "先生成答案"}</span></div>`
      }
    </div>
  </article>

  <article class="card">
    <div class="card-head">
      <div class="step-no ${sendCls}">4</div>
      <div class="card-title">确认并发送</div>
      <div class="card-hint">${item.status === "sent" ? "已发送" : "最后一步由你决定"}</div>
    </div>
    <div class="card-body">
      ${state.draftText ? `<div class="preview" id="previewBox">${md(state.draftText)}</div>` : `<div style="color:#94a3b8;font-size:12.5px">生成答案后可在这里预览最终发布效果。</div>`}
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="btnSendAuto" ${state.draftText && !state.busy.send ? "" : "disabled"}>
          ${state.busy.send ? '<span class="loading"></span>发送中…' : "一键发送到社区"}
        </button>
        <button class="btn" id="btnCopy" ${state.draftText ? "" : "disabled"}>复制答案</button>
        <button class="btn" id="btnOpen">打开帖子</button>
        <span class="spacer"></span>
        <button class="btn ghost" id="btnSkip">跳过这条</button>
      </div>
      <div class="row" style="margin-top:8px">
        <button class="btn ghost" id="btnSend" ${state.draftText ? "" : "disabled"}>已手动发布 · 标记为已发送</button>
      </div>
      <div class="send-note" id="sendHint">${sendNote()}</div>
    </div>
  </article>`;

  wireWork(item);
  loadAnswers(item.uuid);
}

function renderGate(g) {
  const cls = g.verdict === "PASS" ? "pass" : g.verdict === "REJECT" ? "reject" : "fix";
  const label =
    g.verdict === "PASS" ? "可以发布" : g.verdict === "REJECT" ? "不建议发布" : "需要修改";
  const rules = (g.rules || [])
    .map(
      (r) =>
        `<div class="rule ${r.pass ? "pass" : "fail"}"><i>${r.pass ? "✓" : "✕"}</i><span>${esc(
          r.name
        )} — ${esc(r.detail)}</span></div>`
    )
    .join("");
  const issues = (g.issues || []).length
    ? `<ul class="issues">${g.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`
    : "";
  return `
    <div class="gate-top">
      <div class="score ${cls}">${g.score}</div>
      <div>
        <div style="font-size:13px;font-weight:500">${label}${g.demo ? ' <span class="badge wait">演示数据</span>' : ""}</div>
        <div style="font-size:11.5px;color:#64748b;margin-top:2px">模型评分已与 5 项硬规则合并计算（每项不通过扣 12 分）</div>
      </div>
      <span class="spacer"></span>
      <button class="btn sm" id="btnRegate">重新质检</button>
    </div>
    <div class="rules">${rules}</div>
    ${issues}
    ${g.suggestion ? `<div class="suggestion">改进建议：${esc(g.suggestion)}</div>` : ""}`;
}

async function loadAnswers(uuid) {
  const box = $("answersBox");
  try {
    const r = await api("/api/question?uuid=" + encodeURIComponent(uuid));
    const list = r.item.answers || [];
    if (state.currentUuid !== uuid) return;
    if (!list.length) {
      box.innerHTML = `<div style="margin-top:12px;font-size:12px;color:#94a3b8">该帖目前没有任何回答。</div>`;
      return;
    }
    box.innerHTML = `<details class="answers" ${list.length <= 2 ? "open" : ""}>
      <summary>已有 ${list.length} 个回答（供参考，避免重复）</summary>
      ${list
        .map(
          (a) =>
            `<div class="answer-item"><b>${esc(a.author)}${a.official ? " · 官方" : ""}</b> · ${relTime(
              a.time
            )}<div>${esc(a.content)}</div></div>`
        )
        .join("")}
    </details>`;
  } catch (e) {
    box.innerHTML = "";
  }
}

function sendNote() {
  const c = state.config && state.config.send;
  if (!c) return "";
  if (!c.has_auth) {
    return '还没配置登录凭证。<a href="#" id="linkToSendCfg">去设置里贴一次 Cookie</a>，或直接用「复制答案 + 打开帖子」手动发布。';
  }
  if (!c.submit_url) {
    return '登录凭证已识别，token 会自动放进请求头。还差最后一步：抓包拿到「发表回答」的真实接口地址，<a href="#" id="linkToSendCfg">填到设置里</a>。';
  }
  if (c.auto_ready) {
    return '已配置：点「一键发送到社区」直接调用接口发布，不用跳转复制。发送成功会自动标记为已发送。';
  }
  return "";
}

function wireWork(item) {
  const uuid = item.uuid;
  const on = (id, fn) => {
    const el = $(id);
    if (el) el.onclick = fn;
  };

  const editor = $("draftEditor");
  if (editor) {
    editor.oninput = () => {
      state.draftText = editor.value;
      state.edits[uuid] = editor.value;
      const pv = $("previewBox");
      if (pv) pv.innerHTML = md(editor.value);
      const g2 = $("btnGate2");
      if (g2) g2.disabled = !editor.value.trim();
    };
  }

  const lnk = $("linkToSendCfg");
  if (lnk)
    lnk.onclick = (e) => {
      e.preventDefault();
      openSettings("send");
    };

  on("btnDraft", () => actDraft(uuid));
  on("btnRedraft", () => actDraft(uuid));
  on("btnGate", () => actGate(uuid));
  on("btnGate2", () => actGate(uuid));
  on("btnRegate", () => actGate(uuid));
  on("btnCopy", () => copyText(state.draftText));
  on("btnOpen", () => window.open(item.url, "_blank"));
  on("btnSendAuto", () => actSendAuto(uuid));
  on("btnSkip", () => actStatus(uuid, "skipped", "已跳过这条"));
  on("btnSend", () => actSend(uuid));
}

/* ---------------- 动作 ---------------- */

async function actDraft(uuid) {
  if (state.busy.draft) return;
  state.busy.draft = true;
  state.gate = null;
  renderWork();
  try {
    const r = await api("/api/draft", { uuid });
    state.draftText = r.text || "";
    await loadList();
    await loadStats();
    toast(r.demo ? "已生成示例答案（演示模式，配置 API Key 后为真实模型输出）" : "答案已生成，共 " + state.draftText.length + " 字");
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.busy.draft = false;
    renderWork();
  }
}

async function actGate(uuid) {
  if (state.busy.gate) return;
  if (!state.draftText.trim()) return toast("答案为空，无法质检", true);
  state.busy.gate = true;
  renderWork();
  try {
    const r = await api("/api/gate", { uuid, text: state.draftText });
    state.gate = r;
    await loadList();
    await loadStats();
    toast(`质检完成：${r.score} 分 / ${r.verdict}`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.busy.gate = false;
    renderWork();
  }
}

async function actSendAuto(uuid) {
  if (!state.draftText.trim()) return toast("答案为空", true);
  const cfg = state.config && state.config.send;
  if (!cfg || !cfg.auto_ready) {
    toast("还没配置登录凭证，先填一下", true);
    openSettings("send");
    return;
  }
  const g = state.gate;
  if (g && g.verdict === "REJECT" && !confirm("质检结论是「不建议发布」，仍然直接发出去？")) return;
  if (!g && !confirm("这条还没过质量闸门，直接发出去？")) return;

  state.busy.send = true;
  renderWork();
  try {
    const r = await api("/api/send", { uuid, text: state.draftText, mode: "http" });
    const res = r.result || {};
    if (res.ok) {
      delete state.edits[uuid];
      toast("已发送到社区");
      state.currentUuid = null;
      state.draftText = "";
      state.gate = null;
      await loadList();
      await loadStats();
    } else {
      toast(res.hint || res.msg || "发送失败", true);
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.busy.send = false;
    renderWork();
  }
}

async function actSend(uuid) {
  if (!state.draftText.trim()) return toast("答案为空", true);
  if (!confirm("确认这条已经在社区手动发布？标记后会移到「已发送」。")) return;
  try {
    await api("/api/send", { uuid, text: state.draftText, mode: "manual" });
    delete state.edits[uuid];
    state.currentUuid = null;
    state.draftText = "";
    state.gate = null;
    await loadList();
    await loadStats();
    renderWork();
    toast("已标记为已发送");
  } catch (e) {
    toast(e.message, true);
  }
}

async function actStatus(uuid, status, msg) {
  try {
    await api("/api/status", { uuid, status });
    if (state.currentUuid === uuid) {
      state.currentUuid = null;
      state.draftText = "";
      state.gate = null;
    }
    await loadList();
    await loadStats();
    renderWork();
    toast(msg);
  } catch (e) {
    toast(e.message, true);
  }
}

function copyText(text, silent) {
  const done = () => !silent && toast("已复制到剪贴板");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    done();
  } catch (e) {
    toast("复制失败，请手动选择文本", true);
  }
  document.body.removeChild(ta);
}

/* ---------------- 选中 ---------------- */

async function selectQuestion(uuid) {
  state.currentUuid = uuid;
  const item = state.items.find((i) => i.uuid === uuid);
  state.draftText =
    state.edits[uuid] != null ? state.edits[uuid] : item && item.draft ? item.draft.text : "";
  state.gate = item ? item.gate : null;
  try {
    history.replaceState(null, "", "#uuid=" + uuid);
  } catch (e) {}
  renderList();
  renderWork();
  // 窄屏是上下分栏，选中后把工作区滚回顶部，避免还停留在半截位置
  if (window.innerWidth <= 860) {
    const w = $("work");
    if (w) w.scrollTop = 0;
  }
}

function collapseQuestion() {
  if (state.currentUuid) {
    const ed = $("draftEditor");
    if (ed) state.edits[state.currentUuid] = ed.value;
  }
  state.currentUuid = null;
  state.draftText = "";
  state.gate = null;
  try {
    history.replaceState(null, "", location.pathname + location.search);
  } catch (e) {}
  renderList();
  renderWork();
}

/* ---------------- 设置 ---------------- */

function tempHint(n) {
  if (n <= 0.2) return "接近 0：输出最稳，几乎每次都一样。要严格准确就选这个。";
  if (n <= 0.5) return "0.3–0.5 平衡档（推荐）：答案稳定、措辞自然，适合技术问答。";
  if (n <= 0.8) return "0.6–0.8：更灵活，同一个问题会有不同说法，偶尔会跑偏。";
  return "0.9–1.0：发散，创意强但容易编，不建议用在这种答疑场景。";
}

function setTempUI(v) {
  const n = Math.max(0, Math.min(1, isNaN(parseFloat(v)) ? 0.4 : parseFloat(v)));
  const r = $("cfgTempRange");
  if (r) r.value = n;
  if ($("tempVal")) $("tempVal").textContent = n.toFixed(1);
  if ($("tempTip")) $("tempTip").textContent = tempHint(n);
}

function setModelUI(current, models) {
  const sel = $("cfgModelSelect");
  if (!sel) return;
  const list = [];
  const seen = {};
  if (current) {
    list.push({ id: current, note: "当前使用" });
    seen[current] = 1;
  }
  (models || []).forEach((m) => {
    if (m && m.id && !seen[m.id]) {
      list.push(m);
      seen[m.id] = 1;
    }
  });
  if (!list.length) list.push({ id: "deepseek-chat", note: "" });
  sel.innerHTML = list
    .map(
      (m) =>
        `<option value="${esc(m.id)}">${esc(m.id)}${m.note ? " · " + esc(m.note) : ""}</option>`
    )
    .join("");
  sel.value = current && seen[current] ? current : list[0].id;
  $("cfgModel").value = sel.value;
}

async function fetchModels(silent) {
  const tip = $("modelTip");
  if (!silent) {
    tip.style.color = "#64748b";
    tip.textContent = "正在查询可用模型…";
  }
  try {
    const r = await api("/api/models", {
      base_url: $("cfgBaseUrl").value.trim(),
      api_key: $("cfgApiKey").value.trim(),
    });
    const models = r.models || [];
    const ids = models.map((m) => m.id);
    const cur = $("cfgModelSelect").value || (state.config && state.config.llm.model) || "";
    let target = cur;
    let switched = false;
    // 当前填的模型如果这个 Key 用不了，直接切到第一个可用模型，省得用户踩坑
    if (r.source === "api" && ids.length && cur && ids.indexOf(cur) < 0) {
      target = ids[0];
      switched = true;
    }
    setModelUI(target, models);

    if (r.source === "api") {
      tip.style.color = switched ? "#854f0b" : "#0f6e56";
      tip.textContent = switched
        ? `原来的 ${cur} 不在你的可用列表里，已切到 ${target}，点「保存设置」生效 · 共 ${ids.length} 个可用`
        : `已获取 ${ids.length} 个可用模型，直接选一个即可`;
    } else {
      tip.style.color = "#854f0b";
      tip.textContent = (r.error || "没拉到列表") + "；下面是常见模型，可以直接选";
    }
  } catch (e) {
    tip.style.color = "#a32d2d";
    tip.textContent = "查询失败：" + e.message;
  }
}

function toggleManualModel(e) {
  if (e) e.preventDefault();
  const sel = $("cfgModelSelect");
  const inp = $("cfgModel");
  if (inp.style.display === "none") {
    inp.value = sel.value;
    inp.style.display = "block";
    sel.style.display = "none";
    inp.focus();
  } else {
    inp.style.display = "none";
    sel.style.display = "block";
  }
}

async function openSettings(focus) {
  try {
    const r = await api("/api/settings");
    state.config = r.config;
    const c = r.config;
    $("cfgBaseUrl").value = c.llm.base_url || "";
    $("cfgApiKey").value = "";
    $("cfgApiKey").placeholder = c.llm.has_key ? "已保存 " + (c.llm.api_key_masked || "") + "（留空则不修改）" : "留空则使用演示模式";
    $("cfgModel").value = c.llm.model || "";
    $("cfgModelSelect").style.display = "block";
    $("cfgModel").style.display = "none";
    setModelUI(c.llm.model || "", []);
    setTempUI(c.llm.temperature);
    if (c.llm.has_key) fetchModels(true);
    $("cfgApiBase").value = c.collect.api_base || "";
    $("cfgListPath").value = c.collect.list_path || "";
    $("cfgDetailPath").value = c.collect.detail_path || "";
    $("cfgAnswerPath").value = c.collect.answer_path || "";
    $("cfgDelay").value = c.collect.delay;
    $("cfgUa").value = c.collect.user_agent || "";
    $("probeResult").textContent = "";
    $("cfgPages").value = c.collect.pages;
    $("cfgSize").value = c.collect.size;
    $("cfgTag").value = c.collect.tag;
    $("cfgSendMode") &&
      ($("cfgSendMode").value = c.send.mode || "manual");
    $("cfgSubmitUrl").value = c.send.submit_url || "";
    $("cfgAuthType").value = c.send.auth_type || "cookie";
    $("cfgContentType").value = c.send.content_type || "html";
    $("cfgAuthValue").value = c.send.auth_value || "";
    $("cfgAuthValue").placeholder = "整行粘贴，不用挑是哪一个";
    $("sendCheck").textContent = "";
    $("cfgHeaders").value = Object.keys(c.send.headers || {}).length
      ? JSON.stringify(c.send.headers, null, 2)
      : "";
    $("cfgBody").value = c.send.body_template || "";
    $("cfgPromptAnswer").value = c.prompt.answer_system || "";
    $("cfgPromptGate").value = c.prompt.gate_system || "";
    $("drawer").classList.add("show");
    $("mask").classList.add("show");
    if (focus === "send") {
      setTimeout(() => {
        const el = $("cfgAuthValue");
        if (el) {
          el.scrollIntoView({ block: "center", behavior: "smooth" });
          el.focus();
        }
      }, 260);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

async function probeApi() {
  const el = $("probeResult");
  el.style.color = "#64748b";
  el.textContent = "正在请求…";
  try {
    // 先把正在编辑的接口配置落盘，测的就是当前填的值
    await api("/api/settings", {
      collect: {
        api_base: $("cfgApiBase").value.trim(),
        list_path: $("cfgListPath").value.trim(),
        detail_path: $("cfgDetailPath").value.trim(),
        answer_path: $("cfgAnswerPath").value.trim(),
        user_agent: $("cfgUa").value.trim(),
        tag: $("cfgTag").value.trim(),
      },
    });
    const r = await api("/api/probe");
    el.style.color = "#0f6e56";
    const names = (r.sample || []).map((s) => s.title).slice(0, 2).join(" / ");
    el.textContent = `连通 · 返回 ${r.count} 条${names ? "：" + names : ""}`;
  } catch (e) {
    el.style.color = "#a32d2d";
    el.textContent = "失败：" + e.message;
  }
}

function closeSettings() {
  $("drawer").classList.remove("show");
  $("mask").classList.remove("show");
}

async function saveSettings() {
  let headers = {};
  const rawHeaders = $("cfgHeaders").value.trim();
  if (rawHeaders) {
    try {
      headers = JSON.parse(rawHeaders);
    } catch (e) {
      return toast("请求头不是合法 JSON", true);
    }
  }
  const payload = {
    llm: {
      base_url: $("cfgBaseUrl").value.trim(),
      model: ($("cfgModelSelect").style.display === "none"
        ? $("cfgModel").value
        : $("cfgModelSelect").value
      ).trim(),
      temperature: parseFloat($("cfgTempRange").value) || 0.4,
    },
    collect: {
      api_base: $("cfgApiBase").value.trim(),
      list_path: $("cfgListPath").value.trim(),
      detail_path: $("cfgDetailPath").value.trim(),
      answer_path: $("cfgAnswerPath").value.trim(),
      user_agent: $("cfgUa").value.trim(),
      delay: parseFloat($("cfgDelay").value) || 0.25,
      pages: parseInt($("cfgPages").value) || 2,
      size: parseInt($("cfgSize").value) || 20,
      tag: $("cfgTag").value.trim(),
    },
    send: {
      submit_url: $("cfgSubmitUrl").value.trim(),
      auth_type: $("cfgAuthType").value,
      content_type: $("cfgContentType").value,
      headers: headers,
      body_template: $("cfgBody").value.trim(),
    },
    prompt: {
      answer_system: $("cfgPromptAnswer").value,
      gate_system: $("cfgPromptGate").value,
    },
  };
  const key = $("cfgApiKey").value.trim();
  if (key) payload.llm.api_key = key;
  const auth = $("cfgAuthValue").value.trim();
  payload.send.auth_value = auth;

  try {
    const r = await api("/api/settings", payload);
    state.config = r.config;
    renderChips();
    renderWork();
    closeSettings();
    toast(auth ? "设置已保存，一键发送已就绪" : "设置已保存");
  } catch (e) {
    toast(e.message, true);
  }
}

async function checkSendCfg() {
  const el = $("sendCheck");
  const url = $("cfgSubmitUrl").value.trim();
  const raw = $("cfgAuthValue").value.trim();
  const kind = $("cfgAuthType").value;
  const hasSaved = state.config && state.config.send && state.config.send.has_auth;

  if (!url) {
    el.style.color = "#a32d2d";
    el.textContent = "缺少提交接口地址";
    return;
  }
  if (!raw && !hasSaved) {
    el.style.color = "#a32d2d";
    el.textContent = "缺少登录凭证，一键发送会失败";
    return;
  }
  if (!raw) {
    el.style.color = "#0f6e56";
    el.textContent = "将使用已保存的凭证；要换新的就粘贴到上面再保存";
    return;
  }

  el.style.color = "#64748b";
  el.textContent = "正在解析…";
  try {
    const parseKind = kind === "bearer" ? "bearer" : "cookie";
    const r = await api("/api/auth/inspect", { raw: raw, kind: parseKind });
    if ((r.kind || parseKind) === "cookie") {
      if (!r.count) {
        el.style.color = "#a32d2d";
        el.textContent = "没识别到 Cookie，检查一下是不是复制漏了";
        return;
      }
      el.style.color = r.token_found ? "#0f6e56" : "#854f0b";
      el.textContent =
        `识别到 ${r.count} 项 Cookie` +
        (r.token_found ? "，已找到登录 token，会放进 Authorization: Bearer" : "，但没找到 token 字段") +
        (r.likely && r.likely.length ? ` · 疑似登录态：${r.likely.join("、")}` : "");
    } else {
      el.style.color = "#0f6e56";
      el.textContent = `Token 长度 ${r.length}，会以 Authorization: Bearer 发送`;
    }
  } catch (e) {
    el.style.color = "#a32d2d";
    el.textContent = "解析失败：" + e.message;
  }
}

/* ---------------- 初始化 ---------------- */

function bind() {
  $("btnCollect").onclick = collect;
  $("btnSettings").onclick = openSettings;
  $("btnCloseDrawer").onclick = closeSettings;
  $("mask").onclick = closeSettings;
  $("btnSaveSettings").onclick = saveSettings;
  $("btnProbe").onclick = probeApi;
  $("btnCheckSend").onclick = checkSendCfg;
  $("btnFetchModels").onclick = () => fetchModels(false);
  $("toggleManualModel").onclick = toggleManualModel;
  $("cfgTempRange").oninput = (e) => setTempUI(e.target.value);
  $("btnResetPrompt").onclick = () =>
    confirm("恢复默认提示词？当前修改会丢失") && location.reload();

  $("tabs").onclick = async (e) => {
    const t = e.target.closest(".tab");
    if (!t) return;
    [...$("tabs").children].forEach((c) => c.classList.remove("active"));
    t.classList.add("active");
    state.view = t.dataset.view;
    await loadList();
    renderList();
  };

  $("filterSolved").onchange = async (e) => {
    state.solved = e.target.value;
    await loadList();
    renderList();
  };

  $("filterSort").onchange = async (e) => {
    state.sort = e.target.value;
    await loadList();
    renderList();
  };

  $("list").onclick = (e) => {
    const el = e.target.closest(".qitem");
    if (!el) return;
    const uuid = el.dataset.uuid;
    // 再点一次已展开的卡片就收起来
    if (uuid === state.currentUuid) collapseQuestion();
    else selectQuestion(uuid);
  };
}

async function boot() {
  bind();
  try {
    await loadStats();
    await api("/api/settings").then((r) => {
      state.config = r.config;
      renderChips();
    });
    await loadList();
    renderList();
    renderWork();
    const m = (location.hash || "").match(/uuid=(\d+)/);
    if (m && state.items.some((i) => i.uuid === m[1])) selectQuestion(m[1]);
    if (location.search.indexOf("settings=1") >= 0) openSettings();
  } catch (e) {
    toast("连接本地服务失败：" + e.message, true);
  }
}

boot();
