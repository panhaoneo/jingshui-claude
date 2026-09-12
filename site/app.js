/* 景气α · 每日选股研究页（静态，读取 data/*.json） */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const S = { index: null, day: null, charts: null, latest: null, view: "today", stockTab: "cand", sorts: {}, ec: {} };
  const W = { C: 20, A: 15, N: 20, S: 10, L: 20, I: 10 };
  const FNAME = { C: "当季业绩", A: "年度成长", N: "新高", S: "量能", L: "领军", I: "机构(代理)" };

  // ---------- 格式化 ----------
  const nz = (x) => x !== null && x !== undefined && !Number.isNaN(x);
  const pct = (x, d = 1, sign = false) => (nz(x) ? `${sign && x > 0 ? "+" : ""}${(x * 100).toFixed(d)}%` : "—");
  const pp = (x, d = 2) => (nz(x) ? `${x > 0 ? "+" : ""}${x.toFixed(d)}%` : "—"); // 已是百分数
  const num = (x, d = 2) => (nz(x) ? Number(x).toFixed(d) : "—");
  const yi = (x) => (nz(x) ? (Math.abs(x) >= 1e8 ? `${(x / 1e8).toFixed(2)}亿` : `${(x / 1e4).toFixed(0)}万`) : "—");
  const cls = (x) => (nz(x) ? (x > 0 ? "up" : x < 0 ? "down" : "") : "");
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const STATE_CN = { OFFENSE: "进攻", CAUTION: "观望", DEFENSE: "防守" };

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${url} ${r.status}`);
    return r.json();
  }

  // ---------- 启动 ----------
  async function boot() {
    initTheme();
    try {
      S.index = await getJSON("data/index.json");
    } catch (e) {
      $("main").innerHTML = `<div class="card"><div class="empty"><b>还没有数据</b>等待 GitHub Actions 首次运行生成 data/latest.json。</div></div>`;
      return;
    }
    const days = S.index.days || [];
    S.latest = days[0]?.date;
    $("#datePick").innerHTML = days
      .map((d) => `<option value="${d.date}">${d.date} · ${STATE_CN[d.m_state] || d.m_state} · 买点 ${d.buy}</option>`)
      .join("");
    $("#datePick").addEventListener("change", (e) => loadDay(e.target.value));
    window.addEventListener("hashchange", route);
    $("#drawerBg").addEventListener("click", closeDetail);
    document.addEventListener("keydown", (e) => e.key === "Escape" && closeDetail());
    window.addEventListener("resize", () => Object.values(S.ec).forEach((c) => c && c.resize()));
    await loadDay(S.latest);
  }

  async function loadDay(date) {
    S.day = await getJSON(date === S.latest ? "data/latest.json" : `data/daily/${date}.json`);
    if (!S.charts) S.charts = await getJSON("data/charts.json").catch(() => ({}));
    const m = S.day.meta;
    const warns = [...(m.warnings || [])];
    if (date !== S.latest) warns.unshift(`正在查看历史日期 ${date}；K 线图只保留最新交易日的数据。`);
    $("#warn").innerHTML = warns.length ? `<div class="warnbox">${warns.map(esc).join("<br>")}</div>` : "";
    $("#foot").innerHTML = `数据日期 ${m.date} · 生成于 ${m.generated_at}（北京时间）· 数据源 ${m.provider} · v${m.version} · 耗时 ${m.elapsed_s}s${
      m.api_calls ? ` · API ${m.api_calls} 次` : ""
    }<br>研究工具，不构成投资建议。`;
    route();
  }

  function route() {
    const v = (location.hash || "#today").slice(1);
    S.view = $(`#v-${v}`) ? v : "today";
    closeDetail();
    document.querySelectorAll("section.view").forEach((s) => s.classList.toggle("on", s.id === `v-${S.view}`));
    document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("on", a.dataset.v === S.view));
    if (!S.day) return;
    ({ today: renderToday, stocks: renderStocks, sectors: renderSectors, tracking: renderTracking, portfolio: renderPortfolio }[S.view] || (() => {}))();
  }

  function chart(id, el) {
    if (S.ec[id]) S.ec[id].dispose();
    if (!window.echarts) return null;
    S.ec[id] = echarts.init(el, null, { renderer: "canvas" });
    return S.ec[id];
  }

  // ---------- 今日 ----------
  function renderToday() {
    const d = S.day, m = d.market;
    $("#gate").className = `card gate bar-${m.state}`;
    $("#gate").innerHTML = `
      <div class="state"><span class="tag">L0 大盘闸门 M</span><span class="big st-${m.state}">${m.label} ${m.state}</span></div>
      <div class="why"><b>${esc(m.reason)}</b><br>${
        m.state === "OFFENSE" ? "多头排列且派发日少，可按规则开新仓。" :
        m.state === "CAUTION" ? "观望：最高 50% 仓位，只加已盈利头寸，不开新仓。" :
        "防守：0~20% 仓位，只留 3 周内涨幅 >20% 的强势票；止损收紧至 3%。框架此时的最优操作是空仓。"
      }</div>
      <div class="cap"><span class="muted small">仓位上限</span><div class="big">${Math.round(m.position_cap * 100)}%</div></div>`;

    $("#idxCards").innerHTML = m.indices.map((x, i) => `
      <div class="card idx-card"><div class="card-b">
        <div class="row"><span class="name">${esc(x.name)}</span><span class="pill st-${x.state}">${STATE_CN[x.state]}</span></div>
        <div class="row"><span class="px num">${num(x.close)}</span><span class="num ${cls(x.chg_pct)}">${pp(x.chg_pct)}</span></div>
        <div class="meta"><span>MA50 <b class="num">${num(x.ma50)}</b></span><span>MA200 <b class="num">${num(x.ma200)}</b></span><span>派发日 <b class="num">${x.dd_count}</b>/25</span></div>
        <div class="mini" id="mini${i}"></div>
      </div></div>`).join("");
    m.indices.forEach((x, i) => miniIndex(`mini${i}`, x.chart));

    const buys = d.buy_signals || [];
    $("#buyHint").textContent = m.allow_new ? `按 L0 仓位上限，每只目标 ≤${pct(buys[0]?.position?.target ?? Math.min(0.25, m.position_cap / 6), 0)}` : "大盘闸门未放行：以下信号只记录不下单";
    $("#buyList").innerHTML = buys.length ? stockTable("buy", buys, buyCols(), 8) :
      `<div class="empty"><b>今日没有满足 L3 买点的标的</b>${
        m.state === "DEFENSE" ? "大盘处于防守状态，框架建议空仓等待；" : ""
      }候选池中的股票在等待回调或起涨确认。</div>`;

    const f = d.funnel;
    const steps = [["全市场", f.universe], ["主线成分股", f.mainline_members], ["进入评分", f.scored], ["L2 入选", f.passed], ["L3 可下单", f.buy]];
    $("#funnel").innerHTML = `<div class="funnel">${steps.map(([l, n]) => `<div class="step"><div class="n">${n ?? "—"}</div><div class="l">${l}</div></div>`).join("")}</div>
      <div class="excl">${Object.entries(f.excluded || {}).filter(([, n]) => n).map(([k, n]) => `<span class="pill">${esc(k)} · ${n}</span>`).join("")}</div>`;

    const main = d.sectors.filter((s) => s.mainline);
    $("#mainline").innerHTML = main.length ? `<table class="tbl"><thead><tr><th>板块</th><th class="r">综合分</th><th class="r">120日</th><th class="r">距新高</th><th class="r">今日</th></tr></thead><tbody>${
      main.map((s) => `<tr onclick="location.hash='#sectors'"><td class="name">${esc(s.name)}</td><td class="r score">${num(s.score, 1)}</td><td class="r num ${cls(s.ret_rs)}">${pct(s.ret_rs, 1, true)}</td><td class="r num">${pct(s.dist_high)}</td><td class="r num ${cls(s.chg_pct)}">${pp(s.chg_pct)}</td></tr>`).join("")
    }</tbody></table><p class="note">必要条件：板块指数距 250 日高点 ≤5%。今日满足的板块 ${d.sectors.filter((s) => s.eligible).length} 个。</p>` :
      `<div class="empty"><b>没有板块满足主线条件</b>没有接近新高的行业，说明市场缺少景气主线。</div>`;

    const watch = (d.candidates || []).filter((c) => !(c.l3 && c.l3.ok));
    $("#watchList").innerHTML = watch.length ? stockTable("watch", watch, candCols(), 10) :
      `<div class="empty"><b>候选池为空</b>主线板块内没有同时满足 C/A/N/L 且总分 ≥70 的股票。可到「选股 → 全市场领军」查看非主线的强势股研究池。</div>`;
  }

  function miniIndex(id, c) {
    const el = document.getElementById(id);
    const ch = chart(id, el);
    if (!ch || !c) return;
    const n = c.d.length;
    const dd = c.dd.map((f, i) => (f && i >= n - 25 ? [i, c.c[i]] : null)).filter(Boolean); // 只标 25 日计数窗口
    ch.setOption({
      animation: false, grid: { left: 2, right: 2, top: 6, bottom: 2 },
      xAxis: { type: "category", data: c.d, show: false }, yAxis: { type: "value", scale: true, show: false },
      tooltip: { trigger: "axis", confine: true, valueFormatter: (v) => (v == null ? "—" : v.toFixed(2)) },
      series: [
        { name: "收盘", type: "line", data: c.c, symbol: "none", lineStyle: { width: 1.4, color: css("--ink") } },
        { name: "MA50", type: "line", data: c.ma50, symbol: "none", lineStyle: { width: 1, color: css("--amber") } },
        { name: "MA200", type: "line", data: c.ma200, symbol: "none", lineStyle: { width: 1, color: css("--blue") } },
        { name: "派发日", type: "scatter", data: dd, symbolSize: 5, itemStyle: { color: css("--down") } },
      ],
    });
  }

  // ---------- 表格 ----------
  const fbar = (r) => `<span class="fbar" title="${Object.keys(W).map((k) => `${k} ${num(r.factors[k], 1)}/${W[k]}`).join("  ")}">${
    Object.keys(W).map((k) => `<i class="${r.factors[k] <= 0 && ["C", "A", "N", "L"].includes(k) ? "zero" : ""}"><b style="height:${Math.max(0, Math.min(1, r.factors[k] / W[k])) * 100}%"></b></i>`).join("")
  }</span>`;

  const l3tag = (r) => {
    const l = r.l3;
    if (!l) return `<span class="muted">—</span>`;
    return l.ok ? `<span class="pill ok">${esc(l.kind || "起涨确认")}</span>` : `<span class="muted small">${esc(l.status)}</span>`;
  };

  const baseCols = () => [
    { k: "name", label: "股票", fmt: (r) => `<span class="name">${esc(r.name)}</span> <span class="code">${r.code}</span>`, sort: (r) => r.code },
    { k: "sector", label: "行业", fmt: (r) => `${esc(r.sector || "—")}${r.mainline ? "" : ' <span class="pill">非主线</span>'}` },
    { k: "total", label: "总分", r: 1, fmt: (r) => `<span class="score">${num(r.total, 1)}</span>` },
    { k: "f", label: "C A N S L I", fmt: fbar, sort: (r) => r.total },
    { k: "rs", label: "RS", r: 1, fmt: (r) => `<span class="num">${num(r.rs, 0)}</span>` },
    { k: "dist_high", label: "距新高", r: 1, fmt: (r) => `<span class="num">${pct(r.dist_high)}</span>` },
    { k: "np", label: "单季净利", r: 1, fmt: (r) => `<span class="num ${cls(r.details?.C?.np_yoy)}">${pct(r.details?.C?.np_yoy, 0, true)}</span>`, sort: (r) => r.details?.C?.np_yoy ?? -9 },
    { k: "chg_pct", label: "今日", r: 1, fmt: (r) => `<span class="num ${cls(r.chg_pct)}">${pp(r.chg_pct)}</span>` },
  ];
  const candCols = () => [...baseCols(), { k: "l3", label: "L3 状态", fmt: l3tag, sort: (r) => (r.l3?.ok ? 1 : 0) },
    { k: "streak", label: "近20日入选", r: 1, fmt: (r) => `<span class="num">${r.streak || 0}</span>` }];
  const buyCols = () => [...baseCols().slice(0, 4),
    { k: "kind", label: "信号", fmt: (r) => `<span class="pill ok">${esc(r.kind)}</span>${r.signal_date !== S.day.meta.date ? ` <span class="muted small">${r.signal_date}</span>` : ""}` },
    { k: "entry", label: "参考买入", r: 1, fmt: (r) => `<span class="num">${num(r.close)}</span>` },
    { k: "stops", label: "止损 7% / 8%", r: 1, fmt: (r) => `<span class="num down">${(r.stops || []).map((x) => num(x * (r.close / r.entry))).join(" / ")}</span>` },
    { k: "pos", label: "首批仓位", r: 1, fmt: (r) => r.actionable ? `<span class="num">${pct(r.position?.batches?.[0], 1)}</span>` : `<span class="pill warn">不开新仓</span>` },
  ];

  function stockTable(id, rows, cols, limit) {
    const st = S.sorts[id] || { k: null, dir: -1 };
    let data = [...rows];
    if (st.k) {
      const col = cols.find((c) => c.k === st.k);
      const key = col?.sort || ((r) => r[st.k]);
      data.sort((a, b) => { const x = key(a), y = key(b); return (x > y ? 1 : x < y ? -1 : 0) * st.dir; });
    }
    const more = limit && data.length > limit ? data.length - limit : 0;
    if (more) data = data.slice(0, limit);
    S.rows = S.rows || {};
    S.rows[id] = rows;
    const html = `<div class="tbl-wrap"><table class="tbl" data-id="${id}"><thead><tr>${cols.map((c) =>
      `<th class="${c.r ? "r" : ""}" data-k="${c.k}">${c.label}${st.k === c.k ? `<span class="arr">${st.dir > 0 ? " ▲" : " ▼"}</span>` : ""}</th>`).join("")}</tr></thead>
      <tbody>${data.map((r) => `<tr data-code="${r.code}">${cols.map((c) => `<td class="${c.r ? "r" : ""}">${c.fmt(r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
      ${more ? `<p class="note">另有 ${more} 只，见「选股」页。</p>` : ""}`;
    setTimeout(() => bindTable(id, cols, limit), 0);
    return html;
  }

  function bindTable(id, cols, limit) {
    const t = document.querySelector(`table.tbl[data-id="${id}"]`);
    if (!t) return;
    t.querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
      const st = S.sorts[id] || { k: null, dir: -1 };
      S.sorts[id] = { k: th.dataset.k, dir: st.k === th.dataset.k ? -st.dir : -1 };
      t.closest(".tbl-wrap").outerHTML = stockTable(id, S.rows[id], cols, limit).replace(/<p class="note">.*<\/p>/, "");
    }));
    t.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => openDetail(tr.dataset.code, S.rows[id])));
  }

  // ---------- 选股 ----------
  function renderStocks() {
    const d = S.day;
    const tabs = [
      ["cand", "候选池（主线·已入选）", d.candidates || [], "主线板块成分股中总分 ≥70 且 C/A/N/L 均不为 0 的股票，含 L3 买点状态。"],
      ["near", "接近入选（主线）", d.near_miss || [], "主线板块内通过硬性排除与技术面预筛、但未达入选线的股票，按总分排序（前 40）。"],
      ["research", "全市场领军（非主线·研究）", d.research || [], "不属于当期主线板块、但全市场 RS ≥90 且距新高 ≤10% 的股票，同样打分。框架规定 L2 只在主线内选股，此列表仅供研究，不产生买点。"],
    ];
    $("#stockTabs").innerHTML = tabs.map(([k, l, rows]) => `<button data-k="${k}" class="${S.stockTab === k ? "on" : ""}">${l}<span class="cnt">${rows.length}</span></button>`).join("");
    $("#stockTabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { S.stockTab = b.dataset.k; renderStocks(); }));
    const [, , rows, note] = tabs.find((t) => t[0] === S.stockTab);
    $("#stockTable").innerHTML = rows.length ? stockTable(`s-${S.stockTab}`, rows, candCols()) : `<div class="empty"><b>空</b>今日该列表没有股票。</div>`;
    $("#stockNote").textContent = `${note} 点击行查看详情。色块从左到右为 C A N S L I 六因子得分占比，绿框表示必需因子为 0。`;
  }

  // ---------- 板块 ----------
  function renderSectors() {
    const secs = S.day.sectors || [];
    const top = secs.slice(0, 20).reverse();
    const ch = chart("sec", $("#secChart"));
    if (ch) ch.setOption({
      animation: false, grid: { left: 96, right: 30, top: 28, bottom: 20 },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      legend: { top: 0, right: 0, textStyle: { color: css("--ink-2") } },
      xAxis: { type: "value", max: 100, axisLabel: { color: css("--ink-3") }, splitLine: { lineStyle: { color: css("--line") } } },
      yAxis: { type: "category", data: top.map((s) => (s.mainline ? "★ " : "") + s.name), axisLabel: { color: css("--ink-2"), interval: 0, fontSize: 11 } },
      series: [
        ["板块RS", "rs_pct", 0.4, "--accent"], ["创新高", "high_score", 1, "--amber"], ["动能", "mom_pct", 0.2, "--blue"], ["趋势", "trend_ok", 10, "--ink-3"],
      ].map(([n, k, w, c]) => ({
        name: n, type: "bar", stack: "s", barWidth: 12, itemStyle: { color: css(c) },
        data: top.map((s) => +(k === "trend_ok" ? (s.trend_ok ? 10 : 0) : s[k] * w).toFixed(1)),
      })),
    });
    const cols = [
      { k: "rank", label: "#", r: 1, fmt: (s) => `<span class="num">${s.rank}</span>` },
      { k: "name", label: "板块", fmt: (s) => `<span class="name">${esc(s.name)}</span> <span class="code">${s.code}</span> ${s.mainline ? '<span class="pill main">主线</span>' : ""}` },
      { k: "score", label: "综合分", r: 1, fmt: (s) => `<span class="score">${num(s.score, 1)}</span>` },
      { k: "rs_pct", label: "RS百分位", r: 1, fmt: (s) => `<span class="num">${num(s.rs_pct, 0)}</span>` },
      { k: "ret_rs", label: "120日", r: 1, fmt: (s) => `<span class="num ${cls(s.ret_rs)}">${pct(s.ret_rs, 1, true)}</span>` },
      { k: "ret_mom", label: "60日", r: 1, fmt: (s) => `<span class="num ${cls(s.ret_mom)}">${pct(s.ret_mom, 1, true)}</span>` },
      { k: "ret_20", label: "20日", r: 1, fmt: (s) => `<span class="num ${cls(s.ret_20)}">${pct(s.ret_20, 1, true)}</span>` },
      { k: "dist_high", label: "距250日高", r: 1, fmt: (s) => `<span class="num">${pct(s.dist_high)}</span>${s.eligible ? "" : ' <span class="muted small">×</span>'}` },
      { k: "trend_ok", label: "趋势", fmt: (s) => (s.trend_ok ? '<span class="up">多头</span>' : '<span class="muted">—</span>') },
      { k: "chg_pct", label: "今日", r: 1, fmt: (s) => `<span class="num ${cls(s.chg_pct)}">${pp(s.chg_pct)}</span>` },
    ];
    const st = S.sorts.sec || { k: null, dir: -1 };
    const rows = [...secs];
    if (st.k) rows.sort((a, b) => ((a[st.k] > b[st.k] ? 1 : -1) * st.dir));
    $("#secTable").innerHTML = `<div class="tbl-wrap"><table class="tbl"><thead><tr>${cols.map((c) => `<th class="${c.r ? "r" : ""}" data-k="${c.k}">${c.label}</th>`).join("")}</tr></thead><tbody>${
      rows.map((s) => `<tr>${cols.map((c) => `<td class="${c.r ? "r" : ""}">${c.fmt(s)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
      <p class="note">综合分 = 板块RS 40 + 创新高 30 + 中期动能 20 + 趋势健康 10。「×」表示距 250 日高点 >5%，不满足主线必要条件。</p>`;
    $("#secTable").querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
      S.sorts.sec = { k: th.dataset.k, dir: st.k === th.dataset.k ? -st.dir : -1 };
      renderSectors();
    }));
  }

  // ---------- 追踪 ----------
  function renderTracking() {
    const t = S.day.tracking || { stats: {}, items: [] };
    const s = t.stats || {};
    $("#trkStats").innerHTML = [
      ["历史信号数", s.signals ?? 0, ""],
      ["胜率（按止损结算）", nz(s.win_rate) ? pct(s.win_rate, 0) : "—", ""],
      ["平均收益", nz(s.avg_ret) ? pct(s.avg_ret, 1, true) : "—", cls(s.avg_ret)],
    ].map(([l, v, c]) => `<div class="card"><div class="card-b"><div class="muted small">${l}</div><div class="num ${c}" style="font-size:26px;font-weight:700">${v}</div></div></div>`).join("");
    const bs = s.by_state || {};
    if (Object.keys(bs).length) {
      const rows = ["OFFENSE", "CAUTION", "DEFENSE"].map((st) => {
        const x = bs[st] || {};
        return `<tr><td><span class="pill st-${st}">${STATE_CN[st]} ${st}</span>${st === "OFFENSE" ? ' <span class="muted small">框架只在此状态开新仓</span>' : ""}</td><td class="r num">${x.signals ?? 0}</td><td class="r num">${nz(x.win_rate) ? pct(x.win_rate, 0) : "—"}</td><td class="r num ${cls(x.avg_ret)}">${nz(x.avg_ret) ? pct(x.avg_ret, 1, true) : "—"}</td><td class="r num">${x.stopped ?? 0}</td></tr>`;
      }).join("");
      $("#trkByState").innerHTML = `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>信号当日大盘状态</th><th class="r">信号数</th><th class="r">胜率</th><th class="r">平均收益</th><th class="r">止损次数</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
    const items = t.items || [];
    if (!items.length) {
      $("#trkTable").innerHTML = `<div class="empty"><b>暂无历史信号</b>系统每天运行后会自动累积「可下单」信号并追踪其后续表现。</div>`;
      return;
    }
    $("#trkTable").innerHTML = `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>信号日</th><th>股票</th><th>行业</th><th>信号</th><th>当日 M</th><th class="r">持有天数</th><th class="r">当前/结算收益</th><th class="r">最大浮盈</th><th class="r">最大回撤</th><th>状态</th></tr></thead><tbody>${
      items.map((x) => {
        const r = x.stopped ? x.ret_at_stop : x.ret;
        return `<tr data-code="${x.code}"><td class="num">${x.signal_date}</td><td><span class="name">${esc(x.name)}</span> <span class="code">${x.code}</span></td><td>${esc(x.sector || "")}</td><td>${esc(x.kind || "")}</td><td><span class="pill st-${x.m_state}">${STATE_CN[x.m_state] || ""}</span></td><td class="r num">${x.days}</td><td class="r num ${cls(r)}">${pct(r, 1, true)}</td><td class="r num up">${pct(x.max_gain, 1, true)}</td><td class="r num down">${pct(x.max_dd, 1, true)}</td><td>${x.stopped ? `<span class="pill">止损 ${x.stop_date}</span>` : '<span class="pill ok">持有中</span>'}</td></tr>`;
      }).join("")}</tbody></table></div>`;
  }

  // ---------- 持仓 ----------
  function renderPortfolio() {
    const pf = S.day.portfolio || [];
    if (!pf.length) {
      $("#pfBox").innerHTML = `<div class="empty"><b>未配置持仓</b>在仓库根目录的 <code>portfolio.yaml</code> 填写持仓（代码、每批买入日期与价格），每日运行时会逐一检查止损线与卖出触发表。</div>
      <pre class="num small" style="background:var(--chip);padding:12px;border-radius:8px;overflow:auto">holdings:
  - code: 300308.SZ
    batches:
      - {date: 2026-08-20, price: 520.0, weight: 0.10}
      - {date: 2026-08-25, price: 535.0, weight: 0.08}</pre>`;
      return;
    }
    $("#pfBox").innerHTML = `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>股票</th><th class="r">收盘</th><th class="r">浮动盈亏</th><th>结论</th><th>触发</th><th>提示</th></tr></thead><tbody>${
      pf.map((h) => `<tr data-code="${h.code}"><td><span class="name">${esc(h.name || "")}</span> <span class="code">${h.code}</span></td><td class="r num">${num(h.close)}</td><td class="r num ${cls(h.pnl)}">${pct(h.pnl, 1, true)}</td><td><span class="pill ${h.actions?.length ? "warn" : "ok"}">${esc(h.verdict)}</span></td><td class="small">${(h.actions || []).map(esc).join("<br>") || "—"}</td><td class="small muted">${(h.notes || []).map(esc).join("<br>") || "—"}</td></tr>`).join("")
    }</tbody></table></div><p class="note">止损基准是每一批的买入价（已换算为前复权口径），不是持仓均价。</p>`;
    $("#pfBox").querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => openDetail(tr.dataset.code, [])));
  }

  // ---------- 详情 ----------
  function findStock(code) {
    const d = S.day;
    for (const list of [d.buy_signals, d.candidates, d.near_miss, d.research]) {
      const r = (list || []).find((x) => x.code === code);
      if (r) return r;
    }
    return null;
  }

  function openDetail(code) {
    const r = findStock(code);
    const dr = $("#drawer");
    const chartData = S.charts?.[code];
    const fresh = S.day.meta.date === S.latest;
    if (!r) {
      dr.innerHTML = `<div class="dh"><span class="t">${code}</span><button class="btn x" id="dx">关闭</button></div><div class="db"><div class="kline" id="kline"></div></div>`;
    } else {
      const c = r.details?.C || {}, a = r.details?.A || {}, l3 = r.l3 || {};
      const fd = {
        C: c.note || `单季净利 ${pct(c.np_yoy, 0, true)} · 营收 ${pct(c.rev_yoy, 0, true)} · 上季 ${pct(c.prev_np_yoy, 0, true)}${c.turnaround ? " · 扭亏（无同比）" : ""}`,
        A: a.note || `近3年 ≥25%：${a.years_ok ?? 0}/3 年 · ROE ${pct(a.roe)}`,
        N: `距 250 日高点 ${pct(r.dist_high)}`,
        S: `起涨日量比 ${num(r.details?.S?.up_vol_ratio)} · 流通市值 ${nz(r.details?.S?.float_mcap_yi) ? num(r.details.S.float_mcap_yi, 0) + "亿" : "—"}`,
        L: `RS ${num(r.rs, 0)} · 板块内前 ${pct(r.details?.L?.sector_rank_pct, 0)}`,
        I: `60日机构净额 ${r.details?.I?.org_supported ? yi(r.details?.I?.org_net_60d) : "无数据"} · 成交额分位 ${num(r.details?.I?.turnover_pct, 0)}`,
      };
      const hist = r.c_history || [];
      dr.innerHTML = `
        <div class="dh"><span class="t">${esc(r.name)}</span><span class="code">${r.code}</span><span class="pill">${esc(r.sector || "—")}</span>${r.mainline ? '<span class="pill main">主线</span>' : '<span class="pill">非主线·研究</span>'}
          <span class="score" style="margin-left:8px">${num(r.total, 1)}<span class="muted small"> / 95</span></span>${r.passed ? '<span class="pill ok">入选</span>' : ""}
          <button class="btn x" id="dx">关闭</button></div>
        <div class="db stack">
          <div class="card"><div class="card-b">${fresh && chartData ? '<div class="kline" id="kline"></div>' : '<div class="empty">K 线仅保留最新交易日的数据。</div>'}</div></div>
          <div class="factors">${Object.keys(W).map((k) => `<div class="factor ${r.factors[k] <= 0 && ["C", "A", "N", "L"].includes(k) ? "zero" : ""}"><div><span class="k">${k}</span> <span class="w">${FNAME[k]}</span></div><div class="v">${num(r.factors[k], 1)}<span class="w"> / ${W[k]}</span></div><div class="d">${esc(fd[k])}</div></div>`).join("")}</div>
          <div class="card"><div class="card-h"><h3>L3 买点</h3>${l3.ok ? '<span class="pill ok">可下单</span>' : ""}</div><div class="card-b">
            ${l3.status ? `<p style="margin:0 0 10px"><b>${esc(l3.status)}</b></p>` : '<p class="muted">未进入 L3（L2 未入选）。</p>'}
            ${l3.stage_high ? `<div class="kv">
              <div><span>阶段高点</span><span class="num">${num(l3.stage_high)}（${l3.stage_high_date}）</span></div>
              <div><span>回调低点</span><span class="num">${num(l3.pullback_low)}</span></div>
              <div><span>回调深度</span><span class="num">${pct(l3.depth)}</span></div>
              <div><span>平台高点</span><span class="num">${num(l3.platform_high)}</span></div>
              <div><span>买点(pivot)</span><span class="num">${num(l3.pivot)}</span></div>
              <div><span>今日量比</span><span class="num">${num(l3.vol_ratio)}</span></div>
              <div><span>MA50</span><span class="num">${num(l3.ma50)}</span></div>
              <div><span>MA200</span><span class="num">${num(l3.ma200)}</span></div>
              ${l3.stops ? `<div><span>止损线</span><span class="num down">${l3.stops.map((x) => num(x)).join(" / ")}</span></div>` : ""}
            </div><p class="note">L3 价位为前复权口径。</p>` : ""}
          </div></div>
          <div class="grid g2">
            <div class="card"><div class="card-h"><h3>单季业绩（累计口径单季化）</h3><span class="muted small">${esc(r.report_period || "")} 披露 ${esc(r.report_date || "")}</span></div><div class="card-b"><div class="tbl-wrap"><table class="tbl"><thead><tr><th>季度</th><th class="r">营收</th><th class="r">同比</th><th class="r">归母净利</th><th class="r">同比</th></tr></thead><tbody>${
              hist.slice().reverse().map((h) => `<tr><td class="num">${h.period}</td><td class="r num">${yi(h.revenue)}</td><td class="r num ${cls(h.rev_yoy)}">${pct(h.rev_yoy, 0, true)}</td><td class="r num">${yi(h.np)}</td><td class="r num ${cls(h.np_yoy)}">${pct(h.np_yoy, 0, true)}</td></tr>`).join("")
            }</tbody></table></div></div></div>
            <div class="card"><div class="card-h"><h3>年度与其他</h3></div><div class="card-b"><div class="kv">
              ${(r.a_growth || []).map((g) => `<div><span>${g.year} 净利</span><span class="num ${cls(g.yoy)}">${yi(g.np)} · ${pct(g.yoy, 0, true)}</span></div>`).join("")}
              <div><span>ROE</span><span class="num">${pct(r.roe)}</span></div>
              <div><span>RS 百分位</span><span class="num">${num(r.rs, 1)}</span></div>
              <div><span>250日涨幅</span><span class="num ${cls(r.ret250)}">${pct(r.ret250, 0, true)}</span></div>
              <div><span>60日涨幅</span><span class="num ${cls(r.ret60)}">${pct(r.ret60, 0, true)}</span></div>
              <div><span>20日均成交额</span><span class="num">${yi(r.avg_turnover20)}</span></div>
              <div><span>PE(TTM)</span><span class="num">${num(r.valuation?.pe_ttm, 1)}</span></div>
              <div><span>PB</span><span class="num">${num(r.valuation?.pb_mrq, 2)}</span></div>
              <div><span>近20日入选</span><span class="num">${r.streak || 0} 天</span></div>
            </div><p class="note">估值只做记录，不参与筛选。</p></div></div>
          </div>
        </div>`;
    }
    $("#dx").addEventListener("click", closeDetail);
    dr.classList.add("on");
    $("#drawerBg").classList.add("on");
    if (chartData && fresh) setTimeout(() => kline(chartData, r?.l3), 30);
  }

  function closeDetail() {
    $("#drawer").classList.remove("on");
    $("#drawerBg").classList.remove("on");
    if (S.ec.kline) { S.ec.kline.dispose(); S.ec.kline = null; }
  }

  function ma(arr, n) {
    const out = [];
    let s = 0;
    for (let i = 0; i < arr.length; i++) {
      s += arr[i];
      if (i >= n) s -= arr[i - n];
      out.push(i >= n - 1 ? +(s / n).toFixed(3) : null);
    }
    return out;
  }

  function kline(c, l3) {
    const el = document.getElementById("kline");
    const ch = chart("kline", el);
    if (!ch) return;
    const up = css("--up"), dn = css("--down");
    const ohlc = c.d.map((_, i) => [c.o[i], c.c[i], c.l[i], c.h[i]]);
    const marks = [];
    if (l3?.stage_high) marks.push({ yAxis: l3.stage_high, name: "阶段高点", lineStyle: { color: css("--ink-3"), type: "dashed" } });
    if (l3?.stops) marks.push({ yAxis: l3.stops[l3.stops.length - 1], name: "止损", lineStyle: { color: dn, type: "dashed" } });
    ch.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" }, confine: true },
      grid: [{ left: 50, right: 16, top: 16, height: "62%" }, { left: 50, right: 16, top: "78%", height: "16%" }],
      xAxis: [
        { type: "category", data: c.d, axisLabel: { color: css("--ink-3") }, axisLine: { lineStyle: { color: css("--line-2") } } },
        { type: "category", data: c.d, gridIndex: 1, axisLabel: { show: false } },
      ],
      yAxis: [
        { scale: true, axisLabel: { color: css("--ink-3") }, splitLine: { lineStyle: { color: css("--line") } } },
        { scale: true, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
      ],
      dataZoom: [{ type: "inside", xAxisIndex: [0, 1], start: 45, end: 100 }],
      series: [
        { name: "K", type: "candlestick", data: ohlc, itemStyle: { color: up, color0: dn, borderColor: up, borderColor0: dn },
          markLine: marks.length ? { symbol: "none", label: { formatter: "{b} {c}", position: "insideEndTop", color: css("--ink-2") }, data: marks } : undefined },
        { name: "MA50", type: "line", data: ma(c.c, 50), symbol: "none", lineStyle: { width: 1.2, color: css("--amber") } },
        { name: "MA200", type: "line", data: ma(c.c, 200), symbol: "none", lineStyle: { width: 1.2, color: css("--blue") } },
        { name: "量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: c.v.map((v, i) => ({ value: v, itemStyle: { color: c.c[i] >= c.o[i] ? up : dn } })) },
      ],
    });
  }

  // ---------- 主题 ----------
  function initTheme() {
    let t = null;
    try { t = localStorage.getItem("theme"); } catch (e) {}
    if (t) document.documentElement.dataset.theme = t;
    $("#themeBtn").addEventListener("click", () => {
      const dark = document.documentElement.dataset.theme
        ? document.documentElement.dataset.theme === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      const next = dark ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("theme", next); } catch (e) {}
      route();
    });
  }

  boot();
})();
