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
    setInterval(refreshInsights, 5000);
    refreshInsights();
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
    document.querySelectorAll('.btn-pill[data-window]').forEach(btn => {
        btn.addEventListener('click', async () => {
            document.querySelectorAll('.btn-pill[data-window]').forEach(b => b.classList.remove('active'));
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

function buildAnomalyShapes() {
    return allAnomalies.map(a => ({
        type: 'rect',
        xref: 'x',
        yref: 'paper',
        x0: new Date(a.started_at * 1000),
        x1: new Date((a.ended_at || Date.now() / 1000) * 1000),
        y0: 0, y1: 1,
        fillcolor: 'rgba(232, 54, 79, 0.08)',
        line: {
            color: 'rgba(232, 54, 79, 0.5)',
            width: 1,
        },
        layer: 'below',
        opacity: 1,
    }));
}

function renderAnomalyBands() {
    if (!chart) return;
    Plotly.relayout('chart', { shapes: buildAnomalyShapes() });
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
    refreshInsights;
}

function handleAnomalyEnded(anomaly) {
    const existing = allAnomalies.findIndex(a => a.id === anomaly.id);
    if (existing >= 0) {
        allAnomalies[existing] = anomaly;
    }
    renderAnomalyList();
    renderAnomalyBands();
    refreshInsights;
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
            <div class="suspect-row" data-pid="${s.pid}" data-name="${escapeHtml(s.name)}">
                <div class="suspect-info">
                    <span class="suspect-name">${escapeHtml(s.name)}</span>
                    <span class="suspect-pid">PID ${s.pid}</span>
                    ${s.flag ? `<span class="suspect-flag">${escapeHtml(s.flag)}</span>` : ''}
                </div>
                <div class="suspect-actions">
                    <span class="suspect-delta">+${s.delta.toFixed(1)}</span>
                    <button class="btn-kill" data-pid="${s.pid}" data-name="${escapeHtml(s.name)}">
                        Terminate
                    </button>
                </div>
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

    // Wire up kill buttons
    body.querySelectorAll('.btn-kill').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const pid = parseInt(btn.dataset.pid, 10);
            const name = btn.dataset.name;
            await handleKill(pid, name, btn);
        });
    });

    document.getElementById('suspect-modal').classList.remove('hidden');
}

async function handleKill(pid, name, btn) {
    const confirmed = confirm(`Terminate ${name} (PID ${pid})?\n\nThis will stop the process immediately.`);
    if (!confirmed) return;

    btn.disabled = true;
    btn.textContent = 'Killing…';

    try {
        const r = await fetch(`/api/processes/${pid}/kill`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({force: false}),
        });
        const data = await r.json();

        if (!r.ok) {
            alert(`Failed: ${data.detail || 'unknown error'}`);
            btn.disabled = false;
            btn.textContent = 'Terminate';
            return;
        }

        if (data.still_running) {
            // Soft terminate didn't work, offer force
            const force = confirm(`${name} did not exit gracefully. Force kill?`);
            if (force) {
                const r2 = await fetch(`/api/processes/${pid}/kill`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({force: true}),
                });
                const data2 = await r2.json();
                if (!r2.ok) {
                    alert(`Force kill failed: ${data2.detail}`);
                    btn.disabled = false;
                    btn.textContent = 'Terminate';
                    return;
                }
            } else {
                btn.disabled = false;
                btn.textContent = 'Terminate';
                return;
            }
        }

        // Success — visually confirm
        btn.textContent = '✓ Killed';
        btn.classList.add('btn-killed');
        const row = btn.closest('.suspect-row');
        if (row) row.classList.add('suspect-killed');

        showSuccessToast(`Terminated ${name}`);
    } catch (e) {
        alert(`Network error: ${e.message}`);
        btn.disabled = false;
        btn.textContent = 'Terminate';
    }
}

function showSuccessToast(message) {
    const div = document.createElement('div');
    div.className = 'toast toast-success';
    div.innerHTML = `<div class="toast-title">${escapeHtml(message)}</div>`;
    document.getElementById('toast-container').appendChild(div);
    setTimeout(() => {
        div.style.transition = 'opacity 0.3s, transform 0.3s';
        div.style.opacity = '0';
        div.style.transform = 'translateX(20px)';
        setTimeout(() => div.remove(), 300);
    }, 3000);
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

// ---------- System Info modal ----------
let systemInfoCache = null;
let hardwareInventoryCache = null;

document.getElementById('btn-system-info').addEventListener('click', openSystemInfoModal);
document.getElementById('system-info-close').addEventListener('click', closeSystemInfoModal);
document.querySelector('#system-info-modal .modal-backdrop').addEventListener('click', closeSystemInfoModal);

async function openSystemInfoModal() {
    const body = document.getElementById('system-info-body');
    body.innerHTML = '<div class="loading">Loading system info…</div>';
    document.getElementById('system-info-modal').classList.remove('hidden');

    if (!systemInfoCache || !hardwareInventoryCache) {
        try {
            const [si, hi] = await Promise.all([
                fetch('/api/system_info').then(r => r.json()),
                fetch('/api/hardware_inventory').then(r => r.json()),
            ]);
            systemInfoCache = si;
            hardwareInventoryCache = hi;
        } catch (e) {
            body.innerHTML = '<p>Failed to load system info.</p>';
            return;
        }
    }
    body.innerHTML = renderSystemInfo(systemInfoCache, hardwareInventoryCache);
}

function closeSystemInfoModal() {
    document.getElementById('system-info-modal').classList.add('hidden');
}

function renderSystemInfo(si, hi) {
    const cards = [
        { label: 'Hostname', value: si.hostname },
        { label: 'OS', value: `${si.os.system} ${si.os.release}`, sub: si.os.version },
        { label: 'CPU', value: si.cpu.model, sub: `${si.cpu.physical_cores} cores · ${si.cpu.logical_cores} threads` },
        { label: 'Memory', value: `${si.memory.total_gb} GB total`, sub: `${si.memory.available_gb} GB available` },
    ];

    let html = '<div class="modal-title">System Information</div>';
    html += '<div class="modal-subtitle">Detailed hardware and OS inventory for this machine</div>';

    html += '<div class="system-info-grid">';
    for (const c of cards) {
        html += `<div class="system-info-card">
            <div class="system-info-card-label">${escapeHtml(c.label)}</div>
            <div class="system-info-card-value">${escapeHtml(c.value || '—')}</div>
            ${c.sub ? `<div class="system-info-card-sub">${escapeHtml(c.sub)}</div>` : ''}
        </div>`;
    }
    html += '</div>';

    // Each section is now a <details> element so it's collapsible
    if (hi.processors?.length) {
        html += sectionHTML('Processors', hi.processors.length, hi.processors.map(p => ({
            name: p.name,
            details: [
                p.physical_cores && `${p.physical_cores} cores`,
                p.logical_cores && `${p.logical_cores} threads`,
                p.architecture,
                p.max_clock_mhz && `${p.max_clock_mhz} MHz`,
                p.l3_cache_kb && `L3 ${(p.l3_cache_kb/1024).toFixed(1)} MB`,
                p.virtualization_enabled !== undefined && (p.virtualization_enabled ? 'VT-x/AMD-V' : 'Virt disabled'),
                p.socket && `Socket: ${p.socket}`,
            ],
        })), true);
    }

    if (hi.memory_modules?.length) {
        html += sectionHTML('Memory', hi.memory_modules.length, hi.memory_modules.map(m => ({
            name: m.slot ? `${m.slot} — ${m.manufacturer}` : m.manufacturer,
            details: [
                m.capacity_gb && `${m.capacity_gb} GB`,
                m.memory_type,
                m.configured_speed_mhz && `${m.configured_speed_mhz} MHz (rated ${m.speed_mhz})`,
                m.part_number,
                m.form_factor,
                m.voltage_v && `${m.voltage_v} V`,
            ],
        })));
    }

    if (hi.graphics?.length) {
        html += sectionHTML('Graphics', hi.graphics.length, hi.graphics.map(g => ({
            name: g.name,
            details: [
                g.memory_mb && `${g.memory_mb} MB VRAM`,
                g.current_resolution && `${g.current_resolution} @ ${g.current_refresh_hz}Hz`,
                g.driver_version && `Driver ${g.driver_version}`,
                g.driver_date,
                g.video_processor,
            ],
        })));
    }

    if (hi.monitors?.length) {
        html += sectionHTML('Monitors', hi.monitors.length, hi.monitors.map(m => ({
            name: `${m.manufacturer} ${m.user_friendly_name || m.product_code || ''}`.trim(),
            details: [
                m.product_code && `Model code: ${m.product_code}`,
                m.serial && `S/N: ${m.serial}`,
                m.year_of_manufacture && `Manufactured ${m.year_of_manufacture}` + (m.week_of_manufacture ? ` wk${m.week_of_manufacture}` : ''),
            ],
        })));
    }

    if (hi.storage) {
        const phys = hi.storage.physical || [];
        if (phys.length) {
            html += sectionHTML('Storage Drives', phys.length, phys.map(d => {
                const isFailing = d.smart_predicted_failure === true;
                return {
                    name: d.model + (isFailing ? ' ⚠ FAILING' : ''),
                    nameClass: isFailing ? 'hardware-warning' : '',
                    details: [
                        d.size_gb && `${d.size_gb} GB`,
                        d.media_type,
                        d.interface,
                        d.firmware_revision && `Firmware ${d.firmware_revision}`,
                        d.serial && `S/N: ${d.serial}`,
                        d.smart_status && `SMART: ${d.smart_status}`,
                    ],
                };
            }));
        }
        const parts = hi.storage.partitions || [];
        if (parts.length) {
            html += sectionHTML('Partitions', parts.length, parts.map(p => ({
                name: `${p.device} → ${p.mountpoint}`,
                details: [
                    `${p.used_gb} / ${p.total_gb} GB used (${p.percent_used}%)`,
                    p.fstype,
                ],
            })));
        }
    }

    if (hi.audio?.length) {
        html += sectionHTML('Audio Devices', hi.audio.length, hi.audio.map(a => ({
            name: a.name,
            details: [a.manufacturer, a.category, a.status].filter(Boolean),
        })));
    }

    if (hi.peripherals) {
        const allPeripherals = [
            ...(hi.peripherals.pointing || []).map(p => ({...p, kind: p.type})),
            ...(hi.peripherals.keyboards || []).map(p => ({...p, kind: 'Keyboard'})),
            ...(hi.peripherals.cameras || []).map(p => ({...p, kind: 'Camera'})),
            ...(hi.peripherals.other || []).map(p => ({...p, kind: p.category})),
        ];
        if (allPeripherals.length) {
            html += sectionHTML('Peripherals', allPeripherals.length, allPeripherals.map(p => ({
                name: p.name,
                details: [p.kind, p.manufacturer, p.buttons && `${p.buttons} buttons`].filter(Boolean),
            })));
        }
    }

    if (hi.battery) {
        const b = hi.battery;
        html += sectionHTML('Battery', 1, [{
            name: b.name,
            details: [
                b.manufacturer,
                b.chemistry,
                b.estimated_charge_pct != null && `Charge: ${b.estimated_charge_pct}%`,
                b.health_percent != null && `Health: ${b.health_percent}%${b.wear_percent > 20 ? ' ⚠' : ''}`,
                b.design_capacity_mwh && b.full_charge_capacity_mwh && `${b.full_charge_capacity_mwh} / ${b.design_capacity_mwh} mWh`,
                b.status,
            ].filter(Boolean),
        }]);
    }

    if (hi.network?.length) {
        const upAdapters = hi.network.filter(n => n.is_up);
        if (upAdapters.length) {
            html += sectionHTML('Network Adapters', upAdapters.length, upAdapters.map(n => ({
                name: `${n.name}${n.product ? ` (${n.product})` : ''}`,
                details: [
                    n.speed_mbps && `${n.speed_mbps} Mbps`,
                    n.manufacturer,
                    n.mac,
                    ...n.addresses.map(a => `${a.family === 'AF_INET' ? 'IPv4' : 'IPv6'}: ${a.address}`),
                ].filter(Boolean),
            })));
        }
    }

    if (hi.motherboard) {
        const m = hi.motherboard;
        html += sectionHTML('Motherboard / BIOS', 1, [{
            name: `${m.system_manufacturer || m.manufacturer} ${m.system_model || m.product}`.trim(),
            details: [
                m.bios_version && `BIOS ${m.bios_version}`,
                m.bios_release_date && `BIOS released ${m.bios_release_date}`,
                m.bios_manufacturer,
                m.serial && `Board S/N: ${m.serial}`,
            ].filter(Boolean),
        }]);
    }

    return html;
}

function sectionHTML(title, count, items, expanded = false) {
    if (!items.length) return '';
    let html = `<details class="hardware-section" ${expanded ? 'open' : ''}>
        <summary class="hardware-section-title">
            <span class="hardware-section-title-text">${escapeHtml(title)}</span>
            <span class="hardware-section-count">${count}</span>
        </summary>
        <div class="hardware-section-body">`;
    for (const item of items) {
        const details = (item.details || []).filter(d => d).map(d => escapeHtml(String(d))).join(' · ');
        html += `<div class="hardware-item">
            <div class="hardware-item-name ${item.nameClass || ''}">${escapeHtml(item.name || '—')}</div>
            ${details ? `<div class="hardware-item-detail">${details}</div>` : ''}
        </div>`;
    }
    html += '</div></details>';
    return html;
}

// ---------- Pattern engine / Insights ----------
async function refreshInsights() {
    try {
        const r = await fetch('/api/insights');
        const data = await r.json();
        renderInsights(data);
    } catch (e) { /* silent */ }
}

function renderInsights(data) {
    const panel = document.getElementById('insights-panel');
    if (!panel) return;
    if (data.total === 0) {
        panel.innerHTML = '';
        return;
    }

    let html = '<div class="insight-section-title">Insights</div>';

    html += `<div class="insight-row">
        <span class="label">Total anomalies</span>
        <span class="value">${data.total}${data.active_now ? ` · ${data.active_now} active` : ''}</span>
    </div>`;

    if (data.avg_duration_sec) {
        html += `<div class="insight-row">
            <span class="label">Avg duration</span>
            <span class="value">${formatDuration(data.avg_duration_sec)}</span>
        </div>`;
    }

    if (data.repeat_offenders?.length) {
        const top3 = data.repeat_offenders.slice(0, 3);
        const max = Math.max(...top3.map(o => o.anomaly_count));
        html += '<div class="insight-section"><div class="insight-section-title">Repeat offenders</div>';
        for (const o of top3) {
            const pct = (o.anomaly_count / max) * 100;
            html += `<div class="insight-bar-row">
                <span class="name">${escapeHtml(o.name)}</span>
                <span class="count">${o.anomaly_count}</span>
            </div>
            <div class="insight-bar-fill"><div style="width:${pct}%"></div></div>`;
        }
        html += '</div>';
    }

    if (data.metric_distribution?.length) {
        const topMetrics = data.metric_distribution.slice(0, 4);
        html += '<div class="insight-section"><div class="insight-section-title">By metric</div>';
        for (const m of topMetrics) {
            html += `<div class="insight-bar-row">
                <span class="name">${escapeHtml(m.label)}</span>
                <span class="count">${m.count}</span>
            </div>
            <div class="insight-bar-fill"><div style="width:${m.percent}%"></div></div>`;
        }
        html += '</div>';
    }

    panel.innerHTML = html;
}

// ---------- Diagnostic Export ----------
document.getElementById('btn-export').addEventListener('click', openExportModal);
document.getElementById('export-close').addEventListener('click', closeExportModal);
document.querySelector('#export-modal .modal-backdrop').addEventListener('click', closeExportModal);
document.getElementById('export-copy').addEventListener('click', copyExportToClipboard);
document.getElementById('export-download').addEventListener('click', () => {
    window.location.href = '/api/diagnostic.md/download';
});
document.getElementById('export-json').addEventListener('click', async () => {
    const r = await fetch('/api/diagnostic.json');
    const data = await r.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    a.download = `processlens-diagnostic-${ts}.json`;
    a.click();
    URL.revokeObjectURL(url);
});

let cachedExportMarkdown = '';

async function openExportModal() {
    const preview = document.getElementById('export-preview');
    preview.textContent = 'Generating report…';
    document.getElementById('export-modal').classList.remove('hidden');

    try {
        const r = await fetch('/api/diagnostic.md');
        const text = await r.text();
        cachedExportMarkdown = text;
        preview.textContent = text;
    } catch (e) {
        preview.textContent = 'Failed to generate report.';
    }
}

function closeExportModal() {
    document.getElementById('export-modal').classList.add('hidden');
}

async function copyExportToClipboard() {
    if (!cachedExportMarkdown) return;
    try {
        await navigator.clipboard.writeText(cachedExportMarkdown);
        const btn = document.getElementById('export-copy');
        const original = btn.textContent;
        btn.textContent = '✓ Copied!';
        btn.classList.add('btn-killed');
        setTimeout(() => {
            btn.textContent = original;
            btn.classList.remove('btn-killed');
        }, 1500);
    } catch (e) {
        alert('Copy failed. You can manually select and copy from the preview below.');
    }
}