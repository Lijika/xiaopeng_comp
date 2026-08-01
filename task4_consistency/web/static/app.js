/* 任务4 业务向演示 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const VERDICT_ZH = {
  consistent: "一致",
  inconsistent: "不一致",
  uncertain: "存疑",
  skipped: "跳过",
};

const FIELD_ZH = {
  vin: "车辆识别代号 VIN",
  engine_no: "发动机号",
  owner_name: "所有人/姓名",
  lessee_name: "承租人",
  insured_name: "被保险人",
  buyer_name: "购买人",
  plate_no: "号牌号码",
  plate_list: "号牌列表",
  id_number: "证件号",
  financed_amount: "融资金额",
  invoice_amount: "发票金额",
  reg_cert_no: "登记证编号",
  reg_date: "登记日期",
  contract_date: "合同日期",
  address: "地址",
  vehicle_brand: "车辆品牌",
  vehicle_model: "车辆型号",
};

/** 业务场景：固定友好名称，避免用户面对上百个英文文件名 */
const SCENARIOS = [
  {
    id: "ok",
    file: "app_demo_step2_ok.json",
    title: "① 赛题样例绑定 · 字段一致",
    desc: "绑定真实 step2 登记证样例 ID；多单据 VIN/姓名/金额对齐 → 应「一致」。",
  },
  {
    id: "vin",
    file: "app_demo_step2_bad_vin.json",
    title: "② 赛题样例绑定 · VIN 不一致",
    desc: "合同 VIN 与登记证不同 → 「不一致」（资金安全关键项）。",
  },
  {
    id: "fmt",
    file: "app_demo_step2_fmt.json",
    title: "③ 书写变体 + 知识库",
    desc: "日期中文/金额「万」/车牌带点/地址别名 → 标准化后应「一致」。",
  },
  {
    id: "amt",
    file: "app_inconsistent_amount.json",
    title: "④ 融资金额不一致",
    desc: "合同与发票金额超出容差 → 「不一致」。",
  },
  {
    id: "name",
    file: "app_inconsistent_name_id.json",
    title: "⑤ 姓名/证件问题",
    desc: "主体信息不对齐 → 姓名或证件规则告警。",
  },
];

let currentApp = null;
let currentScenario = null;

function formatDetail(detail, fallback = "请求失败") {
  if (detail == null || detail === "") return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (typeof d === "string") return d;
        const loc = Array.isArray(d.loc) ? d.loc.join(".") : "";
        const msg = d.msg || d.message || JSON.stringify(d);
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("; ");
  }
  if (typeof detail === "object") {
    const msg = detail.message || detail.msg || detail.error || "";
    const hint = detail.hint ? `（${detail.hint}）` : "";
    return msg ? `${msg}${hint}` : JSON.stringify(detail);
  }
  return String(detail);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(formatDetail(data.detail, res.statusText));
    err.status = res.status;
    throw err;
  }
  return data;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fieldLabel(name) {
  return FIELD_ZH[name] || name;
}

function showTab(name) {
  $$(".tab").forEach((el) => el.classList.add("hidden"));
  $$("nav button").forEach((b) => b.classList.remove("active"));
  const tab = $(`#tab-${name}`);
  if (tab) tab.classList.remove("hidden");
  const btn = $(`nav button[data-tab="${name}"]`);
  if (btn) btn.classList.add("active");
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("#health-meta").textContent = `规则包 ${h.package || "?"}@${h.version || "?"} · ${h.rules_path || ""}`;
  } catch (e) {
    $("#health-meta").textContent = "后端未连接: " + e.message;
  }
}

function renderScenarios() {
  const grid = $("#scenario-grid");
  grid.innerHTML = SCENARIOS.map(
    (s) => `<button type="button" class="scenario" data-id="${s.id}">
      <b>${escapeHtml(s.title)}</b>
      <span>${escapeHtml(s.desc)}</span>
    </button>`
  ).join("");
  $$(".scenario").forEach((btn) => {
    btn.onclick = () => selectScenario(btn.dataset.id);
  });
}

async function selectScenario(id) {
  currentScenario = SCENARIOS.find((s) => s.id === id) || SCENARIOS[0];
  $$(".scenario").forEach((b) => b.classList.toggle("active", b.dataset.id === currentScenario.id));
  const data = await api(`/api/fixtures/${encodeURIComponent(currentScenario.file)}`);
  currentApp = data;
  if ($("#app-json")) $("#app-json").value = JSON.stringify(data, null, 2);
  renderDocMatrix(data);
  $("#kpis").innerHTML = `<div class="kpi muted">已加载「${escapeHtml(currentScenario.title)}」，请点击运行校验</div>`;
  $("#check-cards").innerHTML = "";
  $("#check-msg").textContent = "";
}

/** 多单据字段矩阵：行=字段，列=单据 */
function renderDocMatrix(app) {
  const docs = app.documents || [];
  const fieldSet = new Set();
  docs.forEach((d) => Object.keys(d.fields || {}).forEach((k) => fieldSet.add(k)));
  // 优先展示关键字段顺序
  const preferred = [
    "vin",
    "engine_no",
    "owner_name",
    "lessee_name",
    "insured_name",
    "plate_no",
    "id_number",
    "financed_amount",
    "invoice_amount",
    "reg_cert_no",
    "reg_date",
    "address",
    "vehicle_brand",
  ];
  const fields = [
    ...preferred.filter((f) => fieldSet.has(f)),
    ...[...fieldSet].filter((f) => !preferred.includes(f)).sort(),
  ];

  const thead = $("#doc-matrix thead");
  const tbody = $("#doc-matrix tbody");
  thead.innerHTML = `<tr><th>字段</th>${docs
    .map((d) => `<th>${escapeHtml(d.doc_type || d.doc_id)}</th>`)
    .join("")}</tr>`;
  tbody.innerHTML = fields
    .map((f) => {
      const cells = docs
        .map((d) => {
          const fv = (d.fields || {})[f];
          if (!fv) return `<td class="muted">—</td>`;
          const raw = fv.raw == null ? "" : String(fv.raw);
          return `<td><span class="raw">${escapeHtml(raw)}</span></td>`;
        })
        .join("");
      return `<tr><td class="doc-type">${escapeHtml(fieldLabel(f))}</td>${cells}</tr>`;
    })
    .join("");
}

function renderReport(report) {
  const s = report.summary || {};
  $("#kpis").innerHTML = `
    <div class="kpi ok">一致<b>${s.consistent ?? 0}</b></div>
    <div class="kpi bad">不一致<b>${s.inconsistent ?? 0}</b></div>
    <div class="kpi warn">存疑<b>${s.uncertain ?? 0}</b></div>
    <div class="kpi">跳过<b>${s.skipped ?? 0}</b></div>
    <div class="kpi">覆盖率<b>${((s.coverage || 0) * 100).toFixed(1)}%</b></div>
  `;

  const order = { inconsistent: 0, uncertain: 1, consistent: 2, skipped: 3 };
  const checks = [...(report.checks || [])].sort(
    (a, b) => (order[a.verdict] ?? 9) - (order[b.verdict] ?? 9)
  );

  $("#check-cards").innerHTML = checks
    .map((c) => {
      const v = c.verdict || "uncertain";
      const snaps = (c.snapshots || [])
        .map(
          (x) => `<div class="snap">
          <div class="doc">${escapeHtml(x.doc_type || "")} · ${escapeHtml(fieldLabel(x.field))}</div>
          <div class="raw">原始：${escapeHtml(x.raw == null ? "—" : String(x.raw))}</div>
          <div class="norm">标准化：${escapeHtml(x.normalized == null ? "—" : String(x.normalized))}</div>
        </div>`
        )
        .join("");
      let diff = "";
      if (c.diff_highlight) {
        const d = c.diff_highlight;
        diff = `<div class="diff-box">差异高亮：位置 ${d.pos ?? "?"} · 左「${escapeHtml(
          d.left ?? ""
        )}」vs 右「${escapeHtml(d.right ?? "")}」</div>`;
      }
      const name = c.name || c.rule_id;
      return `<article class="check-card ${v}">
        <div class="check-head">
          <span class="badge ${v}">${VERDICT_ZH[v] || v}</span>
          <strong>${escapeHtml(name)}</strong>
          <span class="msg">${escapeHtml(c.rule_id || "")} · ${escapeHtml(c.severity || "")}</span>
        </div>
        <div>${escapeHtml(c.message || "")}</div>
        <div class="snap-grid">${snaps || "<div class='msg'>无字段快照</div>"}</div>
        ${diff}
      </article>`;
    })
    .join("");
}

async function runCheck() {
  const msg = $("#check-msg");
  msg.classList.remove("err", "ok");
  msg.textContent = "校验中…";
  try {
    let application = currentApp;
    if ($("#app-json") && $("#app-json").value.trim()) {
      try {
        application = JSON.parse($("#app-json").value);
      } catch (pe) {
        msg.classList.add("err");
        msg.textContent = "JSON 无效: " + pe.message;
        return;
      }
    }
    if (!application) {
      msg.classList.add("err");
      msg.textContent = "请先选择场景";
      return;
    }
    const data = await api("/api/check", {
      method: "POST",
      body: JSON.stringify({ application }),
    });
    renderReport(data.report);
    const frame = $("#report-html-frame");
    if (frame) frame.srcdoc = data.html || "";
    msg.classList.add("ok");
    const s = data.report.summary || {};
    msg.textContent = `完成：一致 ${s.consistent || 0} · 不一致 ${s.inconsistent || 0} · 存疑 ${s.uncertain || 0}`;
    $("#result-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    msg.classList.add("err");
    msg.textContent = "失败: " + e.message;
  }
}

/* —— 规则 / KB / 高级（保留能力） —— */

async function loadFixturesList() {
  const data = await api("/api/fixtures");
  const sel = $("#fixture-select");
  if (!sel) return;
  sel.innerHTML = "";
  for (const f of data.fixtures || []) {
    const opt = document.createElement("option");
    opt.value = f.file;
    opt.textContent = `${f.file} (${f.application_id || "?"})`;
    sel.appendChild(opt);
  }
}

async function loadSelectedFixture() {
  const name = $("#fixture-select")?.value;
  if (!name) return;
  const data = await api(`/api/fixtures/${encodeURIComponent(name)}`);
  currentApp = data;
  $("#app-json").value = JSON.stringify(data, null, 2);
  renderDocMatrix(data);
}

let rulesContent = null;

function renderRulesForm(content) {
  rulesContent = content || {};
  const rules = rulesContent.rules || [];
  const box = $("#rules-form");
  if (!box) return;
  const typeHelp = {
    exact: "完全一致",
    fuzzy: "模糊匹配",
    numeric_tolerance: "数值容差",
    list_contains: "列表包含",
    conditional_required: "条件必填",
  };
  box.innerHTML = rules
    .map((r, i) => {
      const t = r.type || "exact";
      return `<div class="rule-card" data-idx="${i}">
        <div class="rule-title">${escapeHtml(r.name || r.id || "规则")}
          <span class="chip">${escapeHtml(typeHelp[t] || t)}</span>
          <span class="chip">${escapeHtml(r.id || "")}</span>
        </div>
        <div class="grid">
          <div>
            <label>类型 type</label>
            <select data-k="type">
              ${["exact", "fuzzy", "numeric_tolerance", "list_contains", "conditional_required"]
                .map((x) => `<option value="${x}" ${t === x ? "selected" : ""}>${x}</option>`)
                .join("")}
            </select>
          </div>
          <div>
            <label>字段 field</label>
            <input data-k="field" value="${escapeHtml(r.field || "")}" />
          </div>
          <div>
            <label>严重级别 severity</label>
            <select data-k="severity">
              ${["critical", "major", "minor"]
                .map(
                  (x) =>
                    `<option value="${x}" ${(r.severity || "major") === x ? "selected" : ""}>${x}</option>`
                )
                .join("")}
            </select>
          </div>
          <div>
            <label>缺字段 on_missing</label>
            <select data-k="on_missing">
              ${["uncertain", "inconsistent", "skip"]
                .map(
                  (x) =>
                    `<option value="${x}" ${(r.on_missing || "uncertain") === x ? "selected" : ""}>${x}</option>`
                )
                .join("")}
            </select>
          </div>
          <div style="grid-column:1/-1">
            <label>单据类型 docs（逗号分隔）</label>
            <input data-k="docs" value="${escapeHtml((r.docs || []).join(", "))}" />
          </div>
          ${
            t === "fuzzy"
              ? `<div><label>threshold</label><input data-k="threshold" value="${r.threshold ?? 0.88}"/></div>`
              : ""
          }
          ${
            t === "numeric_tolerance"
              ? `<div><label>abs_tol</label><input data-k="abs_tol" value="${r.abs_tol ?? 1}"/>
                 </div><div><label>rel_tol</label><input data-k="rel_tol" value="${r.rel_tol ?? 0.001}"/></div>`
              : ""
          }
        </div>
      </div>`;
    })
    .join("");
}

function collectRulesForm() {
  if (!rulesContent) return null;
  const cards = $$("#rules-form .rule-card");
  const rules = rulesContent.rules || [];
  cards.forEach((card) => {
    const i = Number(card.dataset.idx);
    if (!rules[i]) return;
    card.querySelectorAll("[data-k]").forEach((el) => {
      const k = el.getAttribute("data-k");
      let v = el.value;
      if (k === "docs") {
        v = v
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean);
      } else if (k === "threshold" || k === "abs_tol" || k === "rel_tol") {
        v = Number(v);
      } else if (k === "require_all_docs") {
        v = v === "true";
      }
      rules[i][k] = v;
    });
  });
  rulesContent.rules = rules;
  return rulesContent;
}

async function loadRules() {
  const data = await api("/api/rules");
  $("#rules-yaml").value = data.yaml_text;
  $("#rules-meta").textContent = `${data.path} ${data.is_runtime ? "（运行时）" : "（默认包）"}`;
  renderRulesForm(data.content || {});
}

async function saveRulesForm() {
  const msg = $("#rules-msg");
  msg.classList.remove("err", "ok");
  try {
    const content = collectRulesForm();
    if (!content) throw new Error("无规则内容");
    const data = await api("/api/rules", {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
    msg.classList.add("ok");
    msg.textContent = `表单已保存并生效 · ${data.n_rules} 条规则`;
    await loadRules();
    await loadHealth();
  } catch (e) {
    msg.classList.add("err");
    msg.textContent = "保存失败: " + e.message;
  }
}

async function loadStep2List() {
  const sel = $("#step2-select");
  if (!sel) return;
  try {
    const data = await api("/api/step2/samples");
    sel.innerHTML = (data.samples || [])
      .map((s) => {
        return `<option value="${escapeHtml(s.sample_id)}">${escapeHtml(s.sample_id)} · ${s.n_pages || 0}页 · 关联演示${s.n_linked_fixtures || 0}个</option>`;
      })
      .join("");
    if (data.note) {
      const d = $("#step2-detail");
      if (d && !d.dataset.ready) d.textContent = data.note;
    }
    // OCR inbox fill status
    try {
      const inbox = await api("/api/ocr_inbox");
      const el = $("#ocr-inbox-summary");
      if (el) {
        const items = inbox.items || [];
        const total = items.reduce((a, x) => a + (x.n_slots || 0), 0);
        const filled = items.reduce((a, x) => a + (x.n_filled || 0), 0);
        el.innerHTML = `<b>OCR 待填槽位：</b>${items.length} 个样例 · ${filled}/${total} 已填字。
          <span class="msg">（step2 有框无字；填字后可走 semi 套件。详见 STEP2_TO_TASK4_PIPELINE.md）</span>`;
      }
    } catch (_) {
      /* optional */
    }
  } catch (e) {
    sel.innerHTML = `<option>加载失败: ${escapeHtml(e.message)}</option>`;
  }
}

async function loadStep2Detail() {
  const sid = $("#step2-select")?.value;
  const box = $("#step2-detail");
  if (!sid || !box) return;
  try {
    const data = await api(`/api/step2/${encodeURIComponent(sid)}`);
    const pages = (data.pages || [])
      .map(
        (p) =>
          `<div class="snap"><div class="doc">第${p.order}页 · ${escapeHtml(p.page_type || "")}</div>
           <div class="raw">检出区域：${escapeHtml((p.detected_fields || []).join("、") || "—")}</div></div>`
      )
      .join("");
    // find linked fixture scenario
    const list = await api("/api/fixtures");
    const linked = (list.fixtures || []).filter((f) => f.step2_sample_id === sid);
    const linkHtml = linked.length
      ? `<p class="hint">关联的任务4演示申请：${linked
          .slice(0, 5)
          .map((f) => escapeHtml(f.file))
          .join("、")}${linked.length > 5 ? "…" : ""}。可在「高级」加载后校验。</p>`
      : `<p class="hint">尚无 fixture 绑定该 sample_id；可用 meta.step2_sample_id 关联。</p>`;
    box.innerHTML = `<p class="hint">${escapeHtml(data.note || "")}</p>${linkHtml}<div class="snap-grid">${pages}</div>`;
  } catch (e) {
    box.textContent = "加载失败: " + e.message;
  }
}

async function validateRulesOnly() {
  const msg = $("#rules-msg");
  msg.classList.remove("err", "ok");
  try {
    const data = await api("/api/rules/validate", {
      method: "POST",
      body: JSON.stringify({ yaml_text: $("#rules-yaml").value }),
    });
    msg.classList.add("ok");
    msg.textContent = `校验通过 · ${data.n_rules} 条规则 · ${data.package}@${data.version}（未写入）`;
  } catch (e) {
    msg.classList.add("err");
    msg.textContent = "校验失败: " + e.message;
  }
}

async function saveRules() {
  const msg = $("#rules-msg");
  msg.classList.remove("err", "ok");
  try {
    const data = await api("/api/rules", {
      method: "PUT",
      body: JSON.stringify({ yaml_text: $("#rules-yaml").value }),
    });
    msg.classList.add("ok");
    msg.textContent = `已保存并生效 · ${data.path} · ${data.n_rules} 条`;
    await loadHealth();
  } catch (e) {
    msg.classList.add("err");
    msg.textContent = "保存失败: " + e.message;
  }
}

async function resetRules() {
  await api("/api/rules/reset", { method: "POST", body: "{}" });
  await loadRules();
  await loadHealth();
  $("#rules-msg").textContent = "已恢复默认规则包";
}

async function loadKb() {
  const kb = await api("/api/kb");
  $("#kb-json").textContent = JSON.stringify(kb, null, 2);
  renderKbTable(kb, $("#kb-section").value);
}

function renderKbTable(kb, section) {
  const map = kb[section] || {};
  const rows = Object.entries(map)
    .map(
      ([k, v]) => `<tr>
      <td>${escapeHtml(k)}</td><td>${escapeHtml(v)}</td>
      <td><button class="btn danger" data-del-section="${section}" data-del-key="${escapeHtml(k)}">删除</button></td>
    </tr>`
    )
    .join("");
  $("#kb-table tbody").innerHTML = rows || `<tr><td colspan="3">暂无别名</td></tr>`;
  $$("#kb-table [data-del-key]").forEach((btn) => {
    btn.onclick = async () => {
      await api(
        `/api/kb/${encodeURIComponent(btn.dataset.delSection)}/${encodeURIComponent(btn.dataset.delKey)}`,
        { method: "DELETE" }
      );
      await loadKb();
    };
  });
}

async function addKb() {
  const section = $("#kb-section").value;
  const key = $("#kb-key").value.trim();
  const value = $("#kb-value").value.trim();
  const msg = $("#kb-msg");
  msg.classList.remove("err", "ok");
  if (!key || !value) {
    msg.classList.add("err");
    msg.textContent = "key/value 不能为空";
    return;
  }
  try {
    await api("/api/kb", { method: "POST", body: JSON.stringify({ section, key, value }) });
    $("#kb-key").value = "";
    $("#kb-value").value = "";
    msg.classList.add("ok");
    msg.textContent = "已添加（地址标准化时会应用）";
    await loadKb();
  } catch (e) {
    msg.classList.add("err");
    msg.textContent = "失败: " + e.message;
  }
}

function bind() {
  $$("nav button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  $("#btn-run-check").onclick = runCheck;
  $("#btn-run-check-adv") && ($("#btn-run-check-adv").onclick = runCheck);
  $("#btn-load-fixture") && ($("#btn-load-fixture").onclick = loadSelectedFixture);
  $("#btn-load-rules") && ($("#btn-load-rules").onclick = loadRules);
  $("#btn-validate-rules") && ($("#btn-validate-rules").onclick = validateRulesOnly);
  $("#btn-save-rules") && ($("#btn-save-rules").onclick = saveRules);
  $("#btn-save-rules-form") && ($("#btn-save-rules-form").onclick = saveRulesForm);
  $("#btn-reset-rules") && ($("#btn-reset-rules").onclick = resetRules);
  $("#btn-load-kb") && ($("#btn-load-kb").onclick = loadKb);
  $("#btn-add-kb") && ($("#btn-add-kb").onclick = addKb);
  $("#btn-load-step2") && ($("#btn-load-step2").onclick = loadStep2Detail);
  $("#kb-section") &&
    ($("#kb-section").onchange = async () => {
      const kb = await api("/api/kb");
      renderKbTable(kb, $("#kb-section").value);
    });
}

async function boot() {
  bind();
  renderScenarios();
  await loadHealth();
  await loadFixturesList();
  await loadStep2List();
  await selectScenario("ok");
  try {
    await loadRules();
    await loadKb();
  } catch (_) {
    /* optional tabs */
  }
}

boot();
