// app.js — ProcessLens dashboard

const API = '';  // same origin
const WS_URL = `ws://${location.host}/ws/live`;

// ---------- State ----------
let chart;
let currentWindowSec = 300;
let availableMetrics = {};
let allAnomalies = [];
let lastChartUpdate = 0;

// ---------- Boot ----------
window.addEventListener('DOMContentLoaded', async () => {
    await loadHealth();
    await initChart();
    connectWebSocket();
    setupWindowControls();
    setupModalClose();

    // Polling loops
    setInterval(refreshChart, 2000);
    setInterval(refreshHealth, 3000);
    setInterval(refreshProcesses, 2000);
    refreshAnomalies();
    refreshProcesses();
});

// ---------- Health & status bar ----------
async function loadHealth() {
    try {
        const r = await fetch('/api/health');
        const data = await r.json();
        availableMetrics = data.available_metrics || {};
        document.getElementById('uptime').textContent = formatDuration(data.uptime_sec);
        document.getElementById('sample-count').textContent = data.history_samples;
        document.getElementById('anomaly-count').textContent = data.anomaly_count;
    } catch (e) {
        console.error('health failed', e);
    }
}

async function refreshHealth() {
    try {
        const r = await fetch('/api/health');
        const data = await r.json();
        document.getElementById('uptime').textContent = formatDuration(data.uptime_sec);
        document.getElementById('sample-count').textContent = data.history_samples;
        document.getElementById('anomaly-count').textContent = data.anomaly_count;
    } catch (e) { /* silent */ }
}

// ---------- Chart ----------
const METRIC_DEFS = [
    { key: 'cpu_pct',      label: 'CPU %',      color: '#2e5cff' },
    { key: 'ram_pct',      label: 'RAM %',      color: '#19a974' },
    { key: 'cpu_load_lhm', label: 'CPU Load',   color: '#9966ff' },
    { key: 'cpu_core_max', label: 'Hottest Core', color: '#ff6b35' },
    { key: 'gpu_load',     label: 'GPU %',      color: '#ffb020' },
    { key: 'cpu_temp',     label: 'CPU Temp °C', color: '#e8364f' },
];

async function initChart() {
    const r = await fetch(`/api/timeline?window_sec=${currentWindowSec}`);
    const data = await r.json();

    const traces = METRIC_DEFS
        .filter(m => availableMetrics[m.key])
        .map(m => ({
            x: data.samples.map(s => new Date(s.timestamp * 1000)),
            y: data.samples.map(s => s[m.key]),
            name: m.label,
            type: 'scatter',
            mode: 'lines',
            line: { color: m.color, width: 2 },
            connectgaps: false,
        }));

    const layout = baseLayout();
    Plotly.newPlot('chart', traces, layout, {
        responsive: true,
        displayModeBar: false,
    });
    chart = document.getElementById('chart');
    await refreshAnomalies();  // adds anomaly bands
}

function baseLayout() {
    return {
        margin: { l: 50, r: 20, t: 10, b: 40 },
        paper_bgcolor: '#ffffff',
        plot_bgcolor: '#ffffff',
        font: { family: '-apple-system, Segoe UI, sans-serif', size: 12, color: '#5a6478' },
        xaxis: {
            type: 'date',
            gridcolor: '#eef0f4',
            linecolor: '#e3e6eb',
            tickfont: { size: 11 },
        },
        yaxis: {
            gridcolor: '#eef0f4',
            linecolor: '#e3e6eb',
            tickfont: { size: 11 },
            zeroline: false,
            rangemode: 'tozero',
        },
        legend: { orientation: 'h', y: 1.08, x: 0, font: { size: 11 } },
        hovermode: 'x unified',
        shapes: [],
    };
}

async function refreshChart() {
    if (!chart) return;
    try {
        const r = await fetch(`/api/timeline?window_sec=${currentWindowSec}`);
        const data = await r.json();
        const visible = METRIC_DEFS.filter(m => availableMetrics[m.key]);

        // Update all trace data
        const update = {
            x: visible.map(m => data.samples.map(s => new Date(s.timestamp * 1000))),
            y: visible.map(m => data.samples.map(s => s[m.key])),
        };
        Plotly.restyle('chart', update, visible.map((_, i) => i));

        // CRITICAL: re-apply the rolling X-axis range every refresh
        const now = Date.now();
        const windowStart = now - currentWindowSec * 1000;
        Plotly.relayout('chart', {
            'xaxis.range': [new Date(windowStart), new Date(now)],
            'xaxis.autorange': false,
            shapes: buildAnomalyShapes(),
        });
    } catch (e) {
        console.error('refreshChart failed', e);
    }
}
function setupWindowControls() {
    document.querySelectorAll('.btn-pill').forEach(btn => {
        btn.addEventListener('click', async () => {
            document.querySelectorAll('.btn-pill').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentWindowSec = parseInt(btn.dataset.window, 10);
            await refreshChart();
            await refreshAnomalies();
        });
    });
}

// ---------- Anomalies ----------
async function refreshAnomalies() {
    try {
        const r = await fetch('/api/anomalies');
        const data = await r.json();
        allAnomalies = data.anomalies;
        renderAnomalyList();
        renderAnomalyBands();
    } catch (e) { console.error(e); }
}

function renderAnomalyList() {
    const list = document.getElementById('anomaly-list');
    if (allAnomalies.length === 0) {
        list.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">◎</div>
                <p>No anomalies detected.</p>
                <p class="empty-sub">System is operating within normal parameters.</p>
            </div>`;
        return;
    }

    // newest first
    const sorted = [...allAnomalies].sort((a, b) => b.started_at - a.started_at);
    list.innerHTML = sorted.map(a => anomalyCardHTML(a)).join('');

    list.querySelectorAll('.anomaly-card').forEach(card => {
        card.addEventListener('click', () => {
            const id = card.dataset.id;
            const a = allAnomalies.find(x => x.id === id);
            if (a) {
                zoomToAnomaly(a);
                openSuspectModal(a);
            }
        });
    });
}

function anomalyCardHTML(a) {
    const isActive = a.ended_at == null;
    const time = formatTime(a.started_at);
    const peak = a.peak_value.toFixed(1);
    const baseline = a.baseline.toFixed(1);
    const suspectsHtml = a.suspects.slice(0, 3).map(s => `
        <div class="anomaly-suspect-row">
            <span class="name">${escapeHtml(s.name)}</span>
            <span class="delta">+${s.delta.toFixed(1)}${s.flag ? `<span class="flag">${s.flag}</span>` : ''}</span>
        </div>
    `).join('');

    return `
        <div class="anomaly-card ${isActive ? 'active' : ''}" data-id="${a.id}">
            <div class="anomaly-card-header">
                <span class="anomaly-metric">${escapeHtml(a.label)}</span>
                <span class="anomaly-time">${time}</span>
            </div>
            <div class="anomaly-stats">
                Peak <strong>${peak}${a.unit}</strong> · baseline ${baseline}${a.unit}
            </div>
            <div class="anomaly-suspects">${suspectsHtml || '<em>No suspects identified</em>'}</div>
        </div>`;
}

function renderAnomalyBands() {
    if (!chart) return;
    // Build shape rectangles for each anomaly's time window
    const shapes = allAnomalies.map(a => ({
        type: 'rect',
        xref: 'x',
        yref: 'paper',
        x0: new Date(a.started_at * 1000),
        x1: new Date((a.ended_at || Date.now() / 1000) * 1000),
        y0: 0, y1: 1,
        fillcolor: 'rgba(232, 54, 79, 0.10)',
        line: { width: 0 },
        layer: 'below',
    }));
    Plotly.relayout('chart', { shapes });
}

function zoomToAnomaly(a) {
    const start = (a.started_at - 30) * 1000;
    const end = ((a.ended_at || Date.now() / 1000) + 30) * 1000;
    Plotly.relayout('chart', {
        'xaxis.range': [new Date(start), new Date(end)],
    });
}

// ---------- WebSocket ----------
function connectWebSocket() {
    const ws = new WebSocket(WS_URL);

    ws.onopen = () => setWsStatus('live', 'Connected');
    ws.onclose = () => {
        setWsStatus('error', 'Disconnected');
        setTimeout(connectWebSocket, 2000);  // simple reconnect
    };
    ws.onerror = () => setWsStatus('error', 'Error');

    ws.onmessage = (ev) => {
        try {
            const msg = JSON.parse(ev.data);
            if (msg.event === 'started') {
                handleAnomalyStarted(msg.anomaly);
            } else if (msg.event === 'ended') {
                handleAnomalyEnded(msg.anomaly);
            }
        } catch (e) { console.error('ws parse', e); }
    };

    // Keep-alive ping
    setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 25000);
}

function setWsStatus(cls, text) {
    const dot = document.getElementById('ws-status-dot');
    dot.className = 'status-dot ' + (cls || '');
    document.getElementById('ws-status-text').textContent = text;
}

function handleAnomalyStarted(anomaly) {
    // Add to local list, re-render
    const existing = allAnomalies.findIndex(a => a.id === anomaly.id);
    if (existing >= 0) {
        allAnomalies[existing] = anomaly;
    } else {
        allAnomalies.push(anomaly);
    }
    renderAnomalyList();
    renderAnomalyBands();

    // Pulse the new card
    setTimeout(() => {
        const card = document.querySelector(`.anomaly-card[data-id="${anomaly.id}"]`);
        if (card) card.classList.add('new');
    }, 50);

    showToast(anomaly);
}

function handleAnomalyEnded(anomaly) {
    const existing = allAnomalies.findIndex(a => a.id === anomaly.id);
    if (existing >= 0) {
        allAnomalies[existing] = anomaly;
    }
    renderAnomalyList();
    renderAnomalyBands();
}

function showToast(anomaly) {
    const peak = anomaly.peak_value.toFixed(1);
    const top = anomaly.suspects[0];
    const detail = top
        ? `Peak ${peak}${anomaly.unit} · ${escapeHtml(top.name)} (+${top.delta.toFixed(1)})`
        : `Peak ${peak}${anomaly.unit}`;

    const div = document.createElement('div');
    div.className = 'toast';
    div.innerHTML = `
        <div class="toast-title">⚠ ${escapeHtml(anomaly.label)}</div>
        <div class="toast-detail">${detail}</div>
    `;
    document.getElementById('toast-container').appendChild(div);
    setTimeout(() => {
        div.style.transition = 'opacity 0.3s, transform 0.3s';
        div.style.opacity = '0';
        div.style.transform = 'translateX(20px)';
        setTimeout(() => div.remove(), 300);
    }, 5500);
}

// ---------- Suspect modal ----------
function openSuspectModal(a) {
    const body = document.getElementById('modal-body');
    const suspectsHtml = a.suspects.length === 0
        ? '<p style="color:var(--text-muted)">No suspects identified for this anomaly.</p>'
        : a.suspects.map(s => `
            <div class="suspect-row">
                <div>
                    <span class="suspect-name">${escapeHtml(s.name)}</span>
                    <span class="suspect-pid">PID ${s.pid}</span>
                    ${s.flag ? `<span class="anomaly-suspect-row"><span class="flag">${s.flag}</span></span>` : ''}
                </div>
                <span class="suspect-delta">+${s.delta.toFixed(1)}</span>
            </div>
        `).join('');

    body.innerHTML = `
        <div class="modal-title">${escapeHtml(a.label)}</div>
        <div class="modal-subtitle">
            Started ${formatTime(a.started_at)}${a.ended_at ? ` · ended ${formatTime(a.ended_at)}` : ' · still active'}
        </div>
        <div class="modal-stats">
            <div class="modal-stat">
                <div class="modal-stat-label">Peak</div>
                <div class="modal-stat-value">${a.peak_value.toFixed(1)}${a.unit}</div>
            </div>
            <div class="modal-stat">
                <div class="modal-stat-label">Baseline</div>
                <div class="modal-stat-value">${a.baseline.toFixed(1)}${a.unit}</div>
            </div>
            <div class="modal-stat">
                <div class="modal-stat-label">Threshold</div>
                <div class="modal-stat-value">${a.threshold.toFixed(1)}${a.unit}</div>
            </div>
        </div>
        <div class="modal-section-title">Suspect Processes</div>
        ${suspectsHtml}
    `;
    document.getElementById('suspect-modal').classList.remove('hidden');
}

function setupModalClose() {
    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.querySelector('.modal-backdrop').addEventListener('click', closeModal);
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') closeModal();
    });
}
function closeModal() {
    document.getElementById('suspect-modal').classList.add('hidden');
}

// ---------- Top processes table ----------
async function refreshProcesses() {
    try {
        const r = await fetch('/api/processes/now');
        const data = await r.json();
        const tbody = document.getElementById('processes-tbody');
        if (!data.processes || data.processes.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="loading">Waiting for data…</td></tr>';
            return;
        }
        tbody.innerHTML = data.processes.slice(0, 10).map(p => `
            <tr>
                <td class="proc-name">${escapeHtml(p.name)}</td>
                <td>${p.pid}</td>
                <td>${escapeHtml((p.user || '').split('\\').pop())}</td>
                <td class="num">
                    <span class="proc-cpu-bar"><div style="width:${Math.min(100, p.cpu * 10)}%"></div></span>
                    ${p.cpu.toFixed(1)}
                </td>
                <td class="num">${p.mem_mb.toFixed(0)}</td>
            </tr>
        `).join('');
        if (data.timestamp) {
            document.getElementById('processes-ts').textContent = `as of ${formatTime(data.timestamp)}`;
        }
    } catch (e) { /* silent */ }
}

// ---------- Utils ----------
function formatDuration(sec) {
    sec = Math.floor(sec || 0);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function formatTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString('en-US', { hour12: false });
}

function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}