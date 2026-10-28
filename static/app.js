/* ============================================================
   A股智能选股工作台 — 前端逻辑
   ============================================================ */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  strategies: [],
  enabled: new Set(),
  rows: [],
  meta: null,
  sort: { key: 'score', dir: -1 },
  chart: null,
  lastPayload: null,
};

const FACTOR_LABELS = {
  liquidity: '流动性',
  valuation: '估值',
  turnover: '换手活跃',
  momentum: '动量',
  size: '市值适中',
  amplitude: '振幅',
};

const PRESETS = {
  balanced: { min_amount_yi: 1, mv_min_yi: 30, pct_min: -9.9, pct_max: 9.9, turnover_min: 0.3, turnover_max: 25, pe_max: 0, pb_max: 0, price_min: 2, top_n: 50, exclude_st: true },
  offense: { min_amount_yi: 3, mv_min_yi: 50, pct_min: 1, pct_max: 9.9, turnover_min: 3, turnover_max: 20, pe_max: 0, pb_max: 0, price_min: 3, top_n: 30, exclude_st: true },
  value: { min_amount_yi: 0.8, mv_min_yi: 100, pct_min: -9.9, pct_max: 6, turnover_min: 0.2, turnover_max: 8, pe_max: 20, pb_max: 2.5, price_min: 2, top_n: 30, exclude_st: true },
  reset: { min_amount_yi: 1, mv_min_yi: 30, pct_min: -9.9, pct_max: 9.9, turnover_min: 0.3, turnover_max: 25, pe_max: 0, pb_max: 0, price_min: 2, top_n: 50, exclude_st: true },
};

/* ------------------------------------------------------------ 工具 */
const fmt = {
  pct: (v) => (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%',
  num: (v, d = 2) => (v === null || v === undefined || v === 0 ? '—' : Number(v).toFixed(d)),
  yi: (v) => (v === null || v === undefined ? '—' : Number(v) >= 100 ? Number(v).toFixed(0) : Number(v).toFixed(2)),
  cls: (v) => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat'),
};

function toast(msg, kind = 'info') {
  const el = document.createElement('div');
  el.textContent = msg;
  el.style.cssText = `position:fixed;left:50%;top:76px;transform:translateX(-50%);z-index:99;
    padding:9px 16px;border-radius:9px;font-size:12.5px;box-shadow:0 10px 30px -10px rgba(9,17,28,.4);
    background:${kind === 'error' ? '#b42318' : '#12263f'};color:#fff;opacity:0;transition:.2s`;
  document.body.appendChild(el);
  requestAnimationFrame(() => (el.style.opacity = '1'));
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 250);
  }, 2600);
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.error || `HTTP ${res.status}`);
  return data;
}

/* ------------------------------------------------------------ 顶栏 */
function renderIndexes(indexes) {
  const box = $('#indexStrip');
  if (!indexes || !indexes.length) {
    box.innerHTML = '<div class="index-chip"><span class="n">指数</span><span class="v">—</span></div>';
    return;
  }
  box.innerHTML = indexes
    .map(
      (i) => `<div class="index-chip">
        <span class="n">${i.name}</span>
        <span class="v">${i.price ? i.price.toFixed(2) : '—'}</span>
        <span class="p ${fmt.cls(i.pct)}" style="color:${i.pct > 0 ? '#ff8a80' : i.pct < 0 ? '#6ee7a8' : '#c3d2e0'}">${fmt.pct(i.pct)}</span>
      </div>`
    )
    .join('');
}

function renderSource(overview) {
  const badge = $('#srcBadge');
  const age = overview.snapshot_age_s;
  badge.className = 'src-badge ' + (overview.stale ? 'warn' : 'ok');
  badge.textContent = overview.stale
    ? `数据源降级 · ${age !== null ? age + 's' : ''}`
    : `实时 · ${overview.breadth.total} 只 · ${age !== null ? age + 's 前' : '已连接'}`;
}

function renderBreadth(b) {
  const total = b.up + b.down + b.flat || 1;
  const upW = (b.up / total) * 100;
  $('#sectorList') && null;
  return `<div style="display:flex;gap:2px;height:5px;border-radius:3px;overflow:hidden;margin-bottom:8px">
      <i style="width:${upW}%;background:var(--up)"></i>
      <i style="width:${100 - upW}%;background:var(--down)"></i>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:10.5px;color:var(--ink-3);margin-bottom:8px">
      <span style="color:var(--up)">涨 ${b.up}</span>
      <span>涨停 ${b.limit_up} · 跌停 ${b.limit_down}</span>
      <span style="color:var(--down)">跌 ${b.down}</span>
    </div>`;
}

function renderSectors(list, breadth) {
  const box = $('#sectorList');
  if (breadth) {
    box.innerHTML = renderBreadth(breadth);
  }
  if (!list || !list.length) {
    box.innerHTML += '<p style="font-size:11px;color:var(--ink-3);margin:4px 0 0">板块数据暂不可用</p>';
    return;
  }
  const maxAbs = Math.max(...list.map((x) => Math.abs(x.pct)), 0.01);
  box.innerHTML += list
    .map((s) => {
      const w = Math.max(4, (Math.abs(s.pct) / maxAbs) * 100);
      const color = s.pct >= 0 ? 'var(--up)' : 'var(--down)';
      return `<div class="sector-row">
        <span class="nm" title="${s.name}">${s.name}</span>
        <span class="bar"><i style="width:${w}%;background:${color}"></i></span>
        <span class="val" style="color:${color}">${fmt.pct(s.pct)}</span>
      </div>`;
    })
    .join('');
}

/* ------------------------------------------------------------ 策略 */
function renderStrategies() {
  const grid = $('#strategyGrid');
  grid.innerHTML = state.strategies
    .map((s) => {
      const on = state.enabled.has(s.key) ? 'on' : '';
      const klineNote = s.need_kline ? '' : ' · 仅用快照';
      return `<button class="strategy-chip ${on}" data-key="${s.key}">
        <span class="w">×${s.weight}</span>
        <span class="t"><b>${s.name}</b><span class="tag">${s.tag}</span></span>
        <span class="d">${s.desc}${klineNote}</span>
      </button>`;
    })
    .join('');

  grid.querySelectorAll('.strategy-chip').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.key;
      if (state.enabled.has(key)) state.enabled.delete(key);
      else state.enabled.add(key);
      btn.classList.toggle('on');
    });
  });
}

/* ------------------------------------------------------------ 筛选表单 */
function readFilters() {
  const num = (id, def = 0) => {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) ? v : def;
  };
  return {
    min_amount_yi: num('#f_min_amount'),
    mv_min_yi: num('#f_mv_min'),
    pct_min: num('#f_pct_min', -100),
    pct_max: num('#f_pct_max', 100),
    turnover_min: num('#f_to_min'),
    turnover_max: num('#f_to_max'),
    pe_max: num('#f_pe_max'),
    pb_max: num('#f_pb_max'),
    min_price: num('#f_price_min'),
    max_price: 0,
    pe_min: 0,
    exclude_st: $('#f_exclude_st').checked,
  };
}

function applyPreset(name) {
  const p = PRESETS[name] || PRESETS.reset;
  $('#f_min_amount').value = p.min_amount_yi;
  $('#f_mv_min').value = p.mv_min_yi;
  $('#f_pct_min').value = p.pct_min;
  $('#f_pct_max').value = p.pct_max;
  $('#f_to_min').value = p.turnover_min;
  $('#f_to_max').value = p.turnover_max;
  $('#f_pe_max').value = p.pe_max;
  $('#f_pb_max').value = p.pb_max;
  $('#f_price_min').value = p.price_min;
  $('#f_top_n').value = p.top_n;
  $('#f_exclude_st').checked = p.exclude_st;
  $$('.preset').forEach((b) => b.classList.toggle('active', b.dataset.preset === name));
}

/* ------------------------------------------------------------ 选股 */
async function runScreen() {
  const btn = $('#runBtn');
  btn.disabled = true;
  btn.classList.add('loading');
  btn.querySelector('.btn-label').textContent = '扫描中…';
  $('#runNote').textContent = '正在拉取全市场快照与候选池日 K…';

  const payload = {
    filters: readFilters(),
    strategies: Array.from(state.enabled),
    top_n: parseInt($('#f_top_n').value, 10) || 50,
  };
  state.lastPayload = payload;

  const t0 = performance.now();
  try {
    const data = await api('/api/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.rows = data.rows || [];
    state.meta = data.meta;
    state.sort = { key: 'score', dir: -1 };
    renderTable();
    renderMeta(data);
    renderSignalSummary(data.signal_summary);
    $('#exportBtn').disabled = !state.rows.length;
    $('#emptyState').hidden = state.rows.length > 0;
    $('#resultTable').hidden = state.rows.length === 0;
  } catch (err) {
    toast('选股失败：' + err.message, 'error');
    console.error(err);
  } finally {
    const dt = ((performance.now() - t0) / 1000).toFixed(1);
    btn.disabled = false;
    btn.classList.remove('loading');
    btn.querySelector('.btn-label').textContent = '开始智能选股';
    $('#runNote').textContent = `本次耗时 ${dt}s · 策略需拉取候选池日 K`;
  }
}

function renderMeta(data) {
  const m = data.meta;
  const t = m.timings_ms;
  $('#metaLine').textContent =
    `全市场 ${m.total_stocks} 只 → 筛选后 ${m.universe} 只 → K线池 ${m.kline_covered} 只 → 命中 ${m.signals_hit} 只` +
    ` · 快照 ${t.snapshot}ms / K线 ${t.kline}ms / 打分 ${t.score}ms`;
  $('#universeHint').textContent = `候选 ${m.universe} 只`;
}

function renderSignalSummary(list) {
  const box = $('#signalSummary');
  if (!list || !list.length) {
    box.innerHTML = '';
    return;
  }
  box.innerHTML = list.map((s) => `<span class="sig-pill">${s.name} ${s.count}</span>`).join('');
}

/* ------------------------------------------------------------ 表格 */
function sortedRows() {
  const { key, dir } = state.sort;
  const rows = [...state.rows];
  rows.sort((a, b) => {
    let va = a[key];
    let vb = b[key];
    if (key === 'signal_names') {
      va = (a.signal_names || []).length;
      vb = (b.signal_names || []).length;
    }
    if (typeof va === 'string' || typeof vb === 'string') {
      return String(va).localeCompare(String(vb), 'zh') * dir;
    }
    return ((va || 0) - (vb || 0)) * dir;
  });
  return rows;
}

function renderTable() {
  const body = $('#resultBody');
  const rows = sortedRows();
  body.innerHTML = rows
    .map((r) => {
      const pctCls = fmt.cls(r.pct);
      const signals = r.signals || [];
      const tags = signals.length
        ? `<div class="sig-tags">${signals
            .slice(0, 2)
            .map((s) => `<span class="sig-tag ${signals.length > 1 ? 'multi' : ''}">${s.name}</span>`)
            .join('')}${signals.length > 2 ? `<span class="sig-tag">+${signals.length - 2}</span>` : ''}</div>`
        : '<span style="color:var(--ink-3);font-size:10.5px">—</span>';
      const topCls = r.rank <= 3 ? `top${r.rank}` : '';
      return `<tr data-code="${r.code}" data-market="${r.market}">
        <td><span class="rank-badge ${topCls}">${r.rank}</span></td>
        <td><div class="cell-name"><b>${r.name}</b><span>${r.code}</span></div></td>
        <td class="num">${r.price.toFixed(2)}</td>
        <td class="num ${pctCls}">${fmt.pct(r.pct)}</td>
        <td class="num">${fmt.yi(r.amount_yi)} 亿</td>
        <td class="num">${fmt.num(r.turnover)}%</td>
        <td class="num">${r.pe > 0 ? r.pe.toFixed(1) : '—'}</td>
        <td class="num">${r.pb > 0 ? r.pb.toFixed(2) : '—'}</td>
        <td class="num">${fmt.yi(r.mv_yi)} 亿</td>
        <td class="num"><div class="score-cell"><span class="n">${r.score.toFixed(1)}</span>
          <span class="score-bar"><i style="width:${Math.min(100, r.score)}%"></i></span></div></td>
        <td>${tags}</td>
      </tr>`;
    })
    .join('');

  body.querySelectorAll('tr').forEach((tr) => {
    tr.addEventListener('click', () => {
      const code = tr.dataset.code;
      const market = parseInt(tr.dataset.market, 10);
      openDrawer(code, market);
    });
  });

  $$('.result-table thead th').forEach((th) => {
    th.classList.toggle('sorted', th.dataset.sort === state.sort.key);
    th.classList.toggle('asc', th.dataset.sort === state.sort.key && state.sort.dir === 1);
  });
}

/* ------------------------------------------------------------ 抽屉 + K线 */
async function openDrawer(code, market) {
  const row = state.rows.find((r) => r.code === code);
  const mask = $('#drawerMask');
  const drawer = $('#drawer');
  mask.hidden = false;
  drawer.hidden = false;

  $('#drawerName').textContent = row ? row.name : code;
  $('#drawerMeta').textContent = `${code} · ${market === 1 ? '沪市' : '深市'}`;
  $('#drawerKpis').innerHTML = '<div class="kpi"><div class="k">加载中…</div><div class="v">—</div></div>';
  $('#klineChart').innerHTML = '';
  $('#drawerSignals').innerHTML = '';
  $('#drawerFactors').innerHTML = '';

  try {
    const data = await api(`/api/kline?code=${encodeURIComponent(code)}&market=${market}&bars=180`);
    const k = data.kline;
    if (!k || !k.klines || !k.klines.length) {
      toast('未取到该股 K 线', 'error');
      return;
    }
    renderDrawerKpis(row, data.metrics);
    drawKline(k, row);
    renderDrawerSignals(row);
    renderDrawerFactors(row);
  } catch (err) {
    toast('K线加载失败：' + err.message, 'error');
  }
}

function closeDrawer() {
  $('#drawerMask').hidden = true;
  $('#drawer').hidden = true;
  if (state.chart) {
    state.chart.dispose();
    state.chart = null;
  }
}

function renderDrawerKpis(row, m) {
  const kpis = [
    ['现价', row ? row.price.toFixed(2) : (m.last_close || '—'), row ? fmt.cls(row.pct) : ''],
    ['涨跌幅', row ? fmt.pct(row.pct) : '—', row ? fmt.cls(row.pct) : ''],
    ['成交额', row ? fmt.yi(row.amount_yi) + ' 亿' : '—', ''],
    ['换手率', row ? fmt.num(row.turnover) + '%' : '—', ''],
    ['PE / PB', row ? `${row.pe > 0 ? row.pe.toFixed(1) : '—'} / ${row.pb > 0 ? row.pb.toFixed(2) : '—'}` : '—', ''],
    ['20日涨幅', m.pct_20d !== null && m.pct_20d !== undefined ? fmt.pct(m.pct_20d) : '—', m.pct_20d > 0 ? 'up' : 'down'],
    ['综合分', row ? row.score.toFixed(1) : '—', ''],
  ];
  $('#drawerKpis').innerHTML = kpis
    .map(
      ([k, v, cls]) => `<div class="kpi${k === '综合分' ? ' hl' : ''}"><div class="k">${k}</div>
      <div class="v ${cls}" style="${cls === 'up' ? 'color:var(--up)' : cls === 'down' ? 'color:var(--down)' : k === '综合分' ? 'color:var(--accent)' : ''}">${v}</div></div>`
    )
    .join('');
}

function drawKline(kline, row) {
  const el = $('#klineChart');
  if (state.chart) state.chart.dispose();
  const chart = echarts.init(el, null, { renderer: 'canvas' });
  state.chart = chart;

  const dates = kline.klines.map((k) => k.date);
  const candles = kline.klines.map((k) => [k.open, k.close, k.low, k.high]);
  const volumes = kline.klines.map((k, i) => ({
    value: k.volume,
    itemStyle: { color: k.close >= k.open ? 'rgba(217,45,32,.55)' : 'rgba(7,148,85,.55)' },
  }));

  const maSeries = [
    { name: 'MA5', data: kline.ma5, color: '#e08c1e' },
    { name: 'MA10', data: kline.ma10, color: '#2f6fb5' },
    { name: 'MA20', data: kline.ma20, color: '#8b5cf6' },
  ].map((m) => ({
    name: m.name,
    type: 'line',
    data: m.data,
    smooth: true,
    showSymbol: false,
    lineStyle: { width: 1.2, color: m.color },
    emphasis: { focus: 'series' },
  }));

  chart.setOption({
    animation: false,
    grid: [
      { left: 54, right: 16, top: 16, height: '58%' },
      { left: 54, right: 16, top: '74%', height: '16%' },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(18,38,63,.94)',
      borderWidth: 0,
      textStyle: { color: '#e8f0f8', fontSize: 11 },
    },
    legend: {
      data: ['MA5', 'MA10', 'MA20'],
      top: 0,
      right: 8,
      itemWidth: 12,
      itemHeight: 2,
      textStyle: { fontSize: 10, color: '#55657a' },
    },
    xAxis: [
      {
        type: 'category',
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisLabel: { fontSize: 10, color: '#8496a9' },
        axisTick: { show: false },
      },
      {
        type: 'category',
        gridIndex: 1,
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisLabel: { show: false },
        axisTick: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        splitLine: { lineStyle: { color: '#f0f4f8' } },
        axisLabel: { fontSize: 10, color: '#8496a9' },
        axisLine: { show: false },
      },
      {
        gridIndex: 1,
        splitLine: { show: false },
        axisLabel: { show: false },
        axisLine: { show: false },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 55, end: 100 },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        height: 16,
        bottom: 2,
        start: 55,
        end: 100,
        borderColor: '#e2e8f0',
        fillerColor: 'rgba(37,99,168,.08)',
        handleStyle: { color: '#2563a8' },
        textStyle: { fontSize: 9, color: '#8496a9' },
      },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: candles,
        itemStyle: {
          color: '#d92d20',
          color0: '#079455',
          borderColor: '#d92d20',
          borderColor0: '#079455',
        },
      },
      ...maSeries,
      {
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes,
      },
    ],
  });
}

function renderDrawerSignals(row) {
  const box = $('#drawerSignals');
  if (!row || !row.signals || !row.signals.length) {
    box.innerHTML = '<p class="no-signal">未命中任何策略信号（该股可能不在 K 线候选池内，或条件不满足）</p>';
    return;
  }
  box.innerHTML = row.signals
    .map(
      (s) => `<div class="signal-item">
        <div class="hd"><b>${s.name} <span style="font-weight:400;color:var(--ink-3);font-size:10.5px">${s.tag}</span></b>
          <span class="sc">置信 ${(s.score * 100).toFixed(0)}%</span></div>
        <ul>${s.reasons.map((r) => `<li>${r}</li>`).join('')}</ul>
      </div>`
    )
    .join('');
}

function renderDrawerFactors(row) {
  const box = $('#drawerFactors');
  if (!row || !row.components) {
    box.innerHTML = '<p class="no-signal">无因子数据</p>';
    return;
  }
  const maxPart = Math.max(...Object.values(row.components));
  box.innerHTML = Object.entries(row.components)
    .map(
      ([k, v]) => `<div class="factor-row">
        <span class="fl">${FACTOR_LABELS[k] || k}</span>
        <span class="fb"><i style="width:${maxPart ? (v / maxPart) * 100 : 0}%"></i></span>
        <span class="fv">${v.toFixed(1)}</span>
      </div>`
    )
    .join('');
}

/* ------------------------------------------------------------ 导出 */
function exportCsv() {
  if (!state.rows.length) return;
  const header = ['排名', '代码', '名称', '现价', '涨跌幅%', '成交额(亿)', '换手%', 'PE', 'PB', '市值(亿)', '综合分', '基准分', '信号分', '命中策略'];
  const lines = sortedRows().map((r) => [
    r.rank, r.code, r.name, r.price, r.pct, r.amount_yi, r.turnover,
    r.pe, r.pb, r.mv_yi, r.score, r.base_score, r.signal_score,
    (r.signal_names || []).join('|'),
  ]);
  const csv = [header, ...lines].map((arr) => arr.map((c) => `"${c ?? ''}"`).join(',')).join('\r\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `选股结果_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ------------------------------------------------------------ 初始化 */
async function loadOverview() {
  try {
    const data = await api('/api/overview');
    renderIndexes(data.indexes);
    renderSource(data);
    renderSectors(data.industries_top, data.breadth);
  } catch (err) {
    $('#srcBadge').className = 'src-badge warn';
    $('#srcBadge').textContent = '数据源异常';
    console.error(err);
  }
}

async function init() {
  try {
    const data = await api('/api/strategies');
    state.strategies = data.strategies;
    state.strategies.forEach((s) => state.enabled.add(s.key));
    renderStrategies();
    if (data.defaults) {
      $('#f_min_amount').value = data.defaults.min_amount_yi;
      $('#f_mv_min').value = data.defaults.mv_min_yi;
    }
  } catch (err) {
    toast('策略列表加载失败：' + err.message, 'error');
  }

  loadOverview();

  $$('.preset').forEach((b) => b.addEventListener('click', () => applyPreset(b.dataset.preset)));
  $('#runBtn').addEventListener('click', runScreen);
  $('#exportBtn').addEventListener('click', exportCsv);
  $('#refreshBtn').addEventListener('click', async (e) => {
    e.currentTarget.classList.add('spin');
    await loadOverview();
    setTimeout(() => e.currentTarget.classList.remove('spin'), 400);
  });
  $('#drawerClose').addEventListener('click', closeDrawer);
  $('#drawerMask').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDrawer();
  });

  $$('.result-table thead th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (state.sort.key === key) state.sort.dir *= -1;
      else state.sort = { key, dir: key === 'name' ? 1 : -1 };
      if (state.rows.length) renderTable();
    });
  });

  window.addEventListener('resize', () => state.chart && state.chart.resize());
}

document.addEventListener('DOMContentLoaded', init);
