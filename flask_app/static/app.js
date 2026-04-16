/* ═══════════════════════════════════════════
   Marketing Analytics Dashboard — App Logic
   ═══════════════════════════════════════════ */

const API = '';
let currentTable = null;
let allData = [];
let filteredData = [];
let currentPage = 1;
const PAGE_SIZE = 50;
let charts = {};

/* ═══════ Init ═══════ */
document.addEventListener('DOMContentLoaded', () => {
    initSession();
    checkHealth();
    loadTables();
    loadHistory();
    initScrollReveal();
});

function initSession() {
    fetch('/api/auth/check')
        .then(r => r.json())
        .then(data => {
            if (data.authenticated) {
                const nameEl = document.getElementById('user-display-name');
                const avatarEl = document.getElementById('user-avatar-initial');
                const displayName = data.username || data.email || 'User';
                if (nameEl) nameEl.textContent = displayName;
                if (avatarEl) avatarEl.textContent = displayName[0].toUpperCase();
            }
        })
        .catch(() => {});
}

function handleLogout() {
    window.location.href = '/api/auth/logout';
}

/* ═══════ Scroll-Reveal (Acctual-inspired: scroll-tied, not time-looped) ═══════ */
function initScrollReveal() {
    // Targets: each of these will fade+translateY into view on scroll
    const revealSelectors = [
        '.section-header',
        '.kpi-grid',
        '.etl-comparison',
        '.perf-summary',
        '.chart-card',
        '.table-card',
        '.ai-insights-card',
        '.history-card',
        '.top-campaigns-card',
    ];

    const elements = document.querySelectorAll(revealSelectors.join(', '));

    elements.forEach(el => {
        // Apply hidden state immediately (no flash, no CLS)
        el.classList.add('sr-hidden');
        // KPI grid gets stagger treatment on its children
        if (el.classList.contains('kpi-grid')) {
            el.classList.add('sr-stagger');
        }
    });

    const revealObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const el = entry.target;
                // Swap class to trigger CSS transition
                el.classList.remove('sr-hidden');
                el.classList.add('sr-visible');
                revealObserver.unobserve(el); // Only animate once
            }
        });
    }, {
        threshold: 0.08,          // Trigger when 8% visible (consistent with Acctual)
        rootMargin: '0px 0px -40px 0px'  // Slightly below viewport bottom = feels anchored
    });

    elements.forEach(el => revealObserver.observe(el));
}

/* ═══════ Health Check ═══════ */
async function checkHealth() {
    const badge = document.getElementById('db-status');
    try {
        const res = await fetch(`${API}/api/health`);
        const data = await res.json();
        if (data.status === 'connected') {
            badge.className = 'status-badge status-connected';
            badge.querySelector('.status-text').textContent = 'DB Connected';
        } else {
            badge.className = 'status-badge status-disconnected';
            badge.querySelector('.status-text').textContent = 'DB Offline';
        }
    } catch {
        badge.className = 'status-badge status-disconnected';
        badge.querySelector('.status-text').textContent = 'DB Offline';
    }
}

/* ═══════ Load Tables ═══════ */
async function loadTables(skipAutoSelect = false) {
    try {
        const res = await fetch(`${API}/api/tables`);
        const data = await res.json();
        const select = document.getElementById('table-select');

        // Clear existing options except the placeholder
        select.innerHTML = '<option value="">Select a dataset...</option>';

        if (data.tables && data.tables.length > 0) {
            data.tables.forEach(t => {
                const opt = document.createElement('option');
                opt.value = t.name;
                opt.textContent = `${t.name} (${t.rows.toLocaleString()} rows)`;
                select.appendChild(opt);
            });

            // Only auto-select the first table on initial page load
            if (!skipAutoSelect) {
                select.value = data.tables[0].name;
                loadDashboard(data.tables[0].name);
            }

            document.getElementById('empty-state').style.display = 'none';
        } else {
            showEmptyState();
        }
    } catch (err) {
        console.error('Failed to load tables:', err);
        showEmptyState();
    }
}

function showEmptyState() {
    document.getElementById('empty-state').style.display = 'block';
    document.querySelector('.kpi-grid').style.display = 'none';
    document.querySelector('.charts-grid').style.display = 'none';
    document.querySelector('.table-card').style.display = 'none';
}

function showDashboard() {
    document.getElementById('empty-state').style.display = 'none';
    document.querySelector('.kpi-grid').style.display = 'grid';
    const chartsEl = document.querySelector('.charts-grid-expanded') || document.querySelector('.charts-grid');
    if (chartsEl) chartsEl.style.display = 'grid';
    document.querySelector('.table-card').style.display = 'block';
    document.getElementById('ai-insights-card').style.display = 'block';
    document.getElementById('history-card').style.display = 'block';
    document.getElementById('perf-summary').style.display = 'grid';
    document.getElementById('top-campaigns-card').style.display = 'block';
    // Advanced sections will be toggled by loadAdvancedStats based on data availability
}

/* ═══════ Load Dashboard ═══════ */
let dashboardLoading = false;
async function loadDashboard(tableName) {
    if (!tableName || dashboardLoading) return;
    dashboardLoading = true;
    currentTable = tableName;
    document.getElementById('download-btn').style.display = 'inline-flex';
    
    // Show loading state for UI
    document.querySelectorAll('.kpi-card').forEach(c => c.classList.add('kpi-skeleton'));
    document.getElementById('table-body').innerHTML = '<tr><td colspan="10" style="text-align: center; padding: 40px; color: var(--accent); font-weight: 600; animation: pulse 2s infinite;">Loading Data...</td></tr>';
    const headerTitle = document.querySelector('.section-header h2') || document.createElement('h2');
    
    showDashboard();

    try {
        // Load table data concurrently with the first batch of charts to make it feel faster
        await Promise.all([
            loadStats(tableName),
            loadTableData(tableName),
            loadCharts(tableName) // This manages its own batching internally
        ]);
        
        loadETLStats(tableName);
        loadSummary(tableName);
        loadAdvancedStats(tableName);
    } finally {
        dashboardLoading = false;
        document.querySelectorAll('.kpi-card').forEach(c => c.classList.remove('kpi-skeleton'));
    }
}

/* ═══════ ETL Before/After Stats ═══════ */
async function loadETLStats(tableName) {
    const card = document.getElementById('etl-comparison');
    try {
        const res = await fetch(`${API}/api/etl-stats/${tableName}`);
        if (!res.ok) {
            card.style.display = 'none';
            return;
        }
        const data = await res.json();
        const b = data.before;
        const a = data.after;

        // Show card
        card.style.display = 'block';

        // Source file
        document.getElementById('etl-source').textContent =
            `Source: ${data.source_file || 'Unknown'} • Processed: ${new Date(data.processed_at).toLocaleString()}`;

        // Before stats
        document.getElementById('before-rows').textContent = b.rows.toLocaleString();
        document.getElementById('before-cols').textContent = b.columns;
        document.getElementById('before-dupes').textContent = b.duplicates.toLocaleString();
        document.getElementById('before-nulls').textContent = b.total_nulls.toLocaleString();
        document.getElementById('before-memory').textContent = b.memory_mb.toFixed(2);
        document.getElementById('before-null-pct').textContent = b.null_percentage + '%';

        // After stats
        document.getElementById('after-rows').textContent = a.rows.toLocaleString();
        document.getElementById('after-cols').textContent = a.columns;
        document.getElementById('after-dupes').textContent = a.duplicates.toLocaleString();
        document.getElementById('after-nulls').textContent = a.total_nulls.toLocaleString();
        document.getElementById('after-memory').textContent = a.memory_mb.toFixed(2);
        document.getElementById('after-null-pct').textContent = a.null_percentage + '%';

        // Change summary badges
        const changes = data.changes;
        const badgeContainer = document.getElementById('etl-changes');
        badgeContainer.innerHTML = '';

        const badges = [];
        if (changes.rows_removed > 0) {
            badges.push({ text: `${changes.rows_removed} rows removed`, type: 'positive' });
        }
        if (changes.duplicates_removed > 0) {
            badges.push({ text: `${changes.duplicates_removed} duplicates removed`, type: 'positive' });
        }
        if (changes.nulls_filled > 0) {
            badges.push({ text: `${changes.nulls_filled} nulls filled`, type: 'positive' });
        }
        badges.push({
            text: `${changes.columns_before} > ${changes.columns_after} columns`,
            type: 'neutral'
        });
        const memorySaved = b.memory_mb - a.memory_mb;
        if (memorySaved > 0) {
            badges.push({ text: `${memorySaved.toFixed(2)} MB saved`, type: 'positive' });
        }

        badges.forEach(badge => {
            const el = document.createElement('span');
            el.className = `etl-change-badge ${badge.type}`;
            el.textContent = badge.text;
            badgeContainer.appendChild(el);
        });

    } catch (err) {
        card.style.display = 'none';
        console.log('No ETL stats available for this table');
    }
}

/* ═══════ KPI Stats ═══════ */
async function loadStats(tableName) {
    try {
        const res = await fetch(`${API}/api/stats/${tableName}`);
        const s = await res.json();

        animateValue('kpi-records', s.total_rows || 0, false);
        animateValue('kpi-revenue', s.total_revenue || 0, true);
        animateValue('kpi-clicks', s.total_clicks || 0, false);
        animateValue('kpi-ctr', s.avg_ctr || 0, false, '%');
        animateValue('kpi-impressions', s.total_impressions || 0, false);
        animateValue('kpi-conversions', s.total_conversions || 0, false);

        // Dynamically update KPI labels based on detected column roles
        if (s.kpi_labels) {
            const labelMap = {
                'kpi-revenue': 'Total ' + (s.kpi_labels.revenue || 'Revenue'),
                'kpi-clicks': 'Total ' + (s.kpi_labels.clicks || 'Clicks'),
                'kpi-impressions': s.kpi_labels.impressions || 'Impressions',
                'kpi-ctr': 'Avg ' + (s.kpi_labels.ctr || 'CTR'),
                'kpi-conversions': 'Total ' + (s.kpi_labels.conversions || 'Conversions'),
            };
            Object.entries(labelMap).forEach(([id, label]) => {
                const card = document.getElementById(id);
                if (card) {
                    const labelEl = card.closest('.kpi-card')?.querySelector('.kpi-label');
                    if (labelEl) labelEl.textContent = label;
                }
            });
        }

        // Remove skeleton class
        document.querySelectorAll('.kpi-card').forEach(c => c.classList.remove('kpi-skeleton'));
    } catch (err) {
        console.error('Failed to load stats:', err);
    }
}

/* ═══════ Animated Counter ═══════ */
function animateValue(elementId, target, isCurrency = false, suffix = '') {
    const el = document.getElementById(elementId);
    if (!el) return;

    el.classList.add('counting');
    const duration = 1200;
    const start = performance.now();
    const startVal = 0;

    function tick(now) {
        const elapsed = now - start;
        const progress = Math.min(elapsed / duration, 1);
        // Ease out cubic
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = startVal + (target - startVal) * eased;

        if (isCurrency) {
            el.textContent = '$' + formatNumber(Math.round(current));
        } else if (suffix === '%') {
            el.textContent = current.toFixed(2) + '%';
        } else {
            el.textContent = formatNumber(Math.round(current));
        }

        if (progress < 1) {
            requestAnimationFrame(tick);
        } else {
            el.classList.remove('counting');
        }
    }

    requestAnimationFrame(tick);
}

function formatNumber(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
    if (n >= 1_000) return n.toLocaleString();
    return n.toString();
}

/* ═══════ Charts ═══════ */
async function loadCharts(tableName) {
    // Batch 1: main visible charts
    await Promise.all([
        loadRevenueChart(tableName),
        loadGaugeChart(tableName),
    ]);
    // Batch 2: secondary charts
    await Promise.all([
        loadClicksChart(tableName),
        loadFunnelChart(tableName),
        loadWeekdayChart(tableName),
    ]);
    // Batch 3: remaining charts
    await Promise.all([
        loadCTRChart(tableName),
        loadTopDaysChart(tableName),
        loadMonthlyChart(tableName),
    ]);
}

const chartDefaults = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: {
            labels: {
                color: '#9CA3AF',
                font: { family: 'Inter', size: 11 },
                usePointStyle: true,
                padding: 16,
            }
        },
        tooltip: {
            backgroundColor: 'rgba(11, 11, 15, 0.95)',
            titleColor: '#F5F5F5',
            bodyColor: '#9CA3AF',
            borderColor: 'rgba(168, 85, 247, 0.2)',
            borderWidth: 1,
            cornerRadius: 10,
            padding: 12,
            titleFont: { family: 'Inter', weight: '600' },
            bodyFont: { family: 'Inter' },
        }
    },
    scales: {
        x: {
            grid: { color: 'rgba(255, 255, 255, 0.03)', drawBorder: false },
            ticks: { color: '#6B7280', font: { family: 'Inter', size: 10 } },
        },
        y: {
            grid: { color: 'rgba(255, 255, 255, 0.03)', drawBorder: false },
            ticks: { color: '#6B7280', font: { family: 'Inter', size: 10 } },
        }
    }
};

async function loadRevenueChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/revenue_trend`);
        const data = await res.json();

        if (charts.revenue) charts.revenue.destroy();

        const ctx = document.getElementById('chart-revenue').getContext('2d');

        // Create gradient
        const gradient = ctx.createLinearGradient(0, 0, 0, 360);
        gradient.addColorStop(0, 'rgba(168, 85, 247, 0.35)');
        gradient.addColorStop(1, 'rgba(168, 85, 247, 0.0)');

        charts.revenue = new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.labels || [],
                datasets: [{
                    label: 'Revenue ($)',
                    data: data.values || [],
                    borderColor: '#A855F7',
                    backgroundColor: gradient,
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4,
                    pointRadius: 0,
                    pointHoverRadius: 5,
                    pointHoverBackgroundColor: '#C084FC',
                    pointHoverBorderColor: '#fff',
                    pointHoverBorderWidth: 2,
                }]
            },
            options: {
                ...chartDefaults,
                interaction: {
                    intersect: false,
                    mode: 'index',
                },
                scales: {
                    ...chartDefaults.scales,
                    x: {
                        ...chartDefaults.scales.x,
                        ticks: {
                            ...chartDefaults.scales.x.ticks,
                            maxTicksLimit: 12,
                            maxRotation: 0,
                        }
                    },
                    y: {
                        ...chartDefaults.scales.y,
                        ticks: {
                            ...chartDefaults.scales.y.ticks,
                            callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0) + 'k' : v),
                        }
                    }
                }
            }
        });
    } catch (err) {
        console.error('Revenue chart error:', err);
    }
}

async function loadClicksChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/clicks_impressions`);
        const data = await res.json();

        if (charts.clicks) charts.clicks.destroy();

        const ctx = document.getElementById('chart-clicks').getContext('2d');

        charts.clicks = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: (data.labels || []).map((_, i) => i % 50 === 0 ? i : ''),
                datasets: [
                    {
                        label: 'Impressions',
                        data: data.impressions || [],
                        backgroundColor: 'rgba(167, 139, 250, 0.25)',
                        borderColor: 'rgba(167, 139, 250, 0.6)',
                        borderWidth: 1,
                        borderRadius: 2,
                    },
                    {
                        label: 'Clicks',
                        data: data.clicks || [],
                        backgroundColor: 'rgba(99, 102, 241, 0.7)',
                        borderColor: '#6366f1',
                        borderWidth: 1,
                        borderRadius: 2,
                    },
                ]
            },
            options: {
                ...chartDefaults,
                scales: {
                    ...chartDefaults.scales,
                    x: {
                        ...chartDefaults.scales.x,
                        stacked: false,
                        ticks: { ...chartDefaults.scales.x.ticks, maxTicksLimit: 10 },
                    }
                }
            }
        });
    } catch (err) {
        console.error('Clicks chart error:', err);
    }
}

async function loadCTRChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/ctr_distribution`);
        const data = await res.json();

        if (charts.ctr) charts.ctr.destroy();

        const ctx = document.getElementById('chart-ctr').getContext('2d');

        // Create gradient for bars
        const gradient = ctx.createLinearGradient(0, 0, 0, 300);
        gradient.addColorStop(0, 'rgba(244, 114, 182, 0.8)');
        gradient.addColorStop(1, 'rgba(244, 114, 182, 0.2)');

        charts.ctr = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.labels || [],
                datasets: [{
                    label: 'CTR Distribution (%)',
                    data: data.values || [],
                    backgroundColor: gradient,
                    borderColor: '#f472b6',
                    borderWidth: 1,
                    borderRadius: 4,
                }]
            },
            options: {
                ...chartDefaults,
                plugins: {
                    ...chartDefaults.plugins,
                    legend: { display: false },
                },
                scales: {
                    ...chartDefaults.scales,
                    x: {
                        ...chartDefaults.scales.x,
                        ticks: {
                            ...chartDefaults.scales.x.ticks,
                            maxTicksLimit: 10,
                            maxRotation: 45,
                        }
                    }
                }
            }
        });
    } catch (err) {
        console.error('CTR chart error:', err);
    }
}

/* ═══════ Conversion Rate Gauge ═══════ */
async function loadGaugeChart(tableName) {
    try {
        const res = await fetch(`${API}/api/stats/${tableName}`);
        const data = await res.json();

        const rate = data.avg_conversion_rate || data.avg_ctr || 0;
        const remaining = Math.max(100 - rate, 0);

        if (charts.gauge) charts.gauge.destroy();

        const ctx = document.getElementById('chart-gauge').getContext('2d');
        charts.gauge = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: ['Conversion Rate', 'Remaining'],
                datasets: [{
                    data: [rate, remaining],
                    backgroundColor: ['#A855F7', 'rgba(255, 255, 255, 0.04)'],
                    borderWidth: 0,
                    borderRadius: 6,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                rotation: -90,
                circumference: 180,
                cutout: '78%',
                plugins: {
                    legend: { display: false },
                    tooltip: { ...chartDefaults.plugins.tooltip }
                }
            }
        });

        document.getElementById('gauge-value').textContent = rate.toFixed(1) + '%';
    } catch (err) {
        console.error('Gauge chart error:', err);
    }
}

/* ═══════ Campaign Funnel ═══════ */
async function loadFunnelChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/conversion_funnel`);
        const data = await res.json();

        if (charts.funnel) charts.funnel.destroy();

        const ctx = document.getElementById('chart-funnel').getContext('2d');
        charts.funnel = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: ['Impressions', 'Clicks', 'Conversions'],
                datasets: [{
                    label: 'Volume',
                    data: [data.impressions || 0, data.clicks || 0, data.conversions || 0],
                    backgroundColor: [
                        'rgba(56, 189, 248, 0.7)',
                        'rgba(168, 85, 247, 0.7)',
                        'rgba(34, 197, 94, 0.7)',
                    ],
                    borderColor: ['#38bdf8', '#A855F7', '#22C55E'],
                    borderWidth: 1,
                    borderRadius: 6,
                    barThickness: 40,
                }]
            },
            options: {
                ...chartDefaults,
                indexAxis: 'y',
                plugins: {
                    ...chartDefaults.plugins,
                    legend: { display: false },
                },
                scales: {
                    x: {
                        ...chartDefaults.scales.x,
                        ticks: {
                            ...chartDefaults.scales.x.ticks,
                            callback: v => v >= 1e6 ? (v/1e6).toFixed(1) + 'M' : v >= 1e3 ? (v/1e3).toFixed(0) + 'K' : v,
                        }
                    },
                    y: {
                        ...chartDefaults.scales.y,
                        grid: { display: false },
                    }
                }
            }
        });
    } catch (err) {
        console.error('Funnel chart error:', err);
    }
}

/* ═══════ Revenue by Weekday (Polar) ═══════ */
async function loadWeekdayChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/revenue_by_weekday`);
        const data = await res.json();

        if (charts.weekday) charts.weekday.destroy();

        const ctx = document.getElementById('chart-weekday').getContext('2d');
        const colors = [
            'rgba(168, 85, 247, 0.7)',
            'rgba(56, 189, 248, 0.7)',
            'rgba(34, 197, 94, 0.7)',
            'rgba(251, 191, 36, 0.7)',
            'rgba(248, 113, 113, 0.7)',
            'rgba(192, 132, 252, 0.7)',
            'rgba(244, 114, 182, 0.7)',
        ];

        charts.weekday = new Chart(ctx, {
            type: 'polarArea',
            data: {
                labels: (data.labels || []).map(d => d.slice(0, 3)),
                datasets: [{
                    data: data.values || [],
                    backgroundColor: colors,
                    borderWidth: 0,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'right',
                        labels: { ...chartDefaults.plugins.legend.labels, padding: 8, font: { size: 10, family: 'Inter' } }
                    },
                    tooltip: chartDefaults.plugins.tooltip,
                },
                scales: {
                    r: {
                        grid: { color: 'rgba(255, 255, 255, 0.04)' },
                        ticks: { display: false },
                    }
                }
            }
        });
    } catch (err) {
        console.error('Weekday chart error:', err);
    }
}

/* ═══════ Top Revenue Days (Horizontal Bar) ═══════ */
async function loadTopDaysChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/top_days`);
        const data = await res.json();

        if (charts.topdays) charts.topdays.destroy();

        const ctx = document.getElementById('chart-topdays').getContext('2d');

        const gradient = ctx.createLinearGradient(0, 0, 400, 0);
        gradient.addColorStop(0, 'rgba(168, 85, 247, 0.3)');
        gradient.addColorStop(1, 'rgba(168, 85, 247, 0.8)');

        charts.topdays = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.labels || [],
                datasets: [{
                    label: 'Revenue ($)',
                    data: data.values || [],
                    backgroundColor: gradient,
                    borderColor: '#A855F7',
                    borderWidth: 1,
                    borderRadius: 4,
                    barThickness: 18,
                }]
            },
            options: {
                ...chartDefaults,
                indexAxis: 'y',
                plugins: {
                    ...chartDefaults.plugins,
                    legend: { display: false },
                },
                scales: {
                    x: {
                        ...chartDefaults.scales.x,
                        ticks: {
                            ...chartDefaults.scales.x.ticks,
                            callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0) + 'k' : v),
                        }
                    },
                    y: {
                        ...chartDefaults.scales.y,
                        grid: { display: false },
                        ticks: { ...chartDefaults.scales.y.ticks, font: { size: 9, family: 'Inter' } }
                    }
                }
            }
        });
    } catch (err) {
        console.error('Top days chart error:', err);
    }
}

/* ═══════ Monthly Revenue (Grouped Bar) ═══════ */
async function loadMonthlyChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/monthly_revenue`);
        const data = await res.json();

        if (charts.monthly) charts.monthly.destroy();

        const ctx = document.getElementById('chart-monthly').getContext('2d');

        const datasets = [{
            label: 'Revenue',
            data: data.revenue || [],
            backgroundColor: 'rgba(168, 85, 247, 0.6)',
            borderColor: '#A855F7',
            borderWidth: 1,
            borderRadius: 4,
        }];

        if (data.clicks && data.clicks.length > 0) {
            datasets.push({
                label: 'Clicks',
                data: data.clicks,
                backgroundColor: 'rgba(56, 189, 248, 0.5)',
                borderColor: '#38bdf8',
                borderWidth: 1,
                borderRadius: 4,
            });
        }

        charts.monthly = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.labels || [],
                datasets: datasets,
            },
            options: {
                ...chartDefaults,
                scales: {
                    ...chartDefaults.scales,
                    x: {
                        ...chartDefaults.scales.x,
                        ticks: { ...chartDefaults.scales.x.ticks, maxRotation: 45, font: { size: 9, family: 'Inter' } }
                    },
                    y: {
                        ...chartDefaults.scales.y,
                        ticks: {
                            ...chartDefaults.scales.y.ticks,
                            callback: v => v >= 1e6 ? (v/1e6).toFixed(1) + 'M' : v >= 1e3 ? (v/1e3).toFixed(0) + 'K' : v,
                        }
                    }
                }
            }
        });
    } catch (err) {
        console.error('Monthly chart error:', err);
    }
}

/* ═══════ Performance Summary & Top Campaigns ═══════ */
async function loadSummary(tableName) {
    try {
        const res = await fetch(`${API}/api/stats/${tableName}/summary`);
        const data = await res.json();

        // Performance cards
        document.getElementById('perf-rpc').textContent = '$' + (data.revenue_per_click || 0).toFixed(2);
        document.getElementById('perf-adr').textContent = '$' + formatNumber(Math.round(data.avg_daily_revenue || 0));
        document.getElementById('perf-ctr').textContent = (data.click_through_rate || 0).toFixed(2) + '%';

        // Top campaigns table
        if (data.top_days && data.top_days.length > 0) {
            const body = document.getElementById('top-campaigns-body');
            const colors = ['#fbbf24', '#A855F7', '#22C55E', '#38bdf8', '#f472b6'];
            const rankClasses = ['gold', 'silver', 'bronze', 'normal', 'normal'];
            body.innerHTML = data.top_days.map((d, i) => `
                <div class="campaign-row">
                    <div class="campaign-rank ${rankClasses[i] || 'normal'}">${i + 1}</div>
                    <div class="campaign-info">
                        <div class="campaign-date">${d.date}</div>
                        <div class="campaign-bar-container">
                            <div class="campaign-bar" style="width: ${d.pct}%; background: ${colors[i] || colors[4]};"></div>
                        </div>
                    </div>
                    <span class="campaign-revenue">$${formatNumber(Math.round(d.revenue))}</span>
                </div>
            `).join('');
        }
    } catch (err) {
        console.error('Summary load error:', err);
    }
}

/* ═══════ Data Table ═══════ */
async function loadTableData(tableName) {
    try {
        const res = await fetch(`${API}/api/data/${tableName}`);
        const data = await res.json();

        allData = data.data || [];
        filteredData = [...allData];
        currentPage = 1;

        // Build table headers
        const thead = document.getElementById('table-head');
        thead.innerHTML = '<tr>' + (data.columns || []).map(c =>
            `<th onclick="sortTable('${c}')">${c} ↕</th>`
        ).join('') + '</tr>';

        document.getElementById('table-row-count').textContent = `${allData.length.toLocaleString()} rows`;
        renderTablePage();
    } catch (err) {
        console.error('Table data error:', err);
    }
}

function renderTablePage() {
    const startIdx = (currentPage - 1) * PAGE_SIZE;
    const pageData = filteredData.slice(startIdx, startIdx + PAGE_SIZE);
    const totalPages = Math.ceil(filteredData.length / PAGE_SIZE) || 1;

    const tbody = document.getElementById('table-body');
    tbody.innerHTML = pageData.map(row =>
        '<tr>' + Object.values(row).map(v => {
            if (typeof v === 'number') {
                return `<td>${v % 1 === 0 ? v.toLocaleString() : v.toFixed(4)}</td>`;
            }
            return `<td>${v ?? ''}</td>`;
        }).join('') + '</tr>'
    ).join('');

    document.getElementById('page-info').textContent = `Page ${currentPage} of ${totalPages}`;
    document.getElementById('prev-page').disabled = currentPage <= 1;
    document.getElementById('next-page').disabled = currentPage >= totalPages;
}

function changePage(delta) {
    const totalPages = Math.ceil(filteredData.length / PAGE_SIZE);
    const newPage = currentPage + delta;
    if (newPage >= 1 && newPage <= totalPages) {
        currentPage = newPage;
        renderTablePage();
    }
}

function filterTable(query) {
    const q = query.toLowerCase();
    if (!q) {
        filteredData = [...allData];
    } else {
        filteredData = allData.filter(row =>
            Object.values(row).some(v => String(v).toLowerCase().includes(q))
        );
    }
    currentPage = 1;
    document.getElementById('table-row-count').textContent = `${filteredData.length.toLocaleString()} rows`;
    renderTablePage();
}

let sortColumn = null;
let sortAsc = true;

function sortTable(column) {
    if (sortColumn === column) {
        sortAsc = !sortAsc;
    } else {
        sortColumn = column;
        sortAsc = true;
    }

    filteredData.sort((a, b) => {
        let va = a[column], vb = b[column];
        if (typeof va === 'number' && typeof vb === 'number') {
            return sortAsc ? va - vb : vb - va;
        }
        va = String(va || ''); vb = String(vb || '');
        return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    });

    currentPage = 1;
    renderTablePage();
}

/* ═══════ Upload ═══════ */
function openUploadModal() {
    document.getElementById('upload-modal').classList.add('active');
    resetUploadUI();
}

function closeUploadModal() {
    document.getElementById('upload-modal').classList.remove('active');
}

function resetUploadUI() {
    document.getElementById('drop-zone').style.display = 'block';
    document.getElementById('fill-strategy-section').style.display = 'block';
    document.getElementById('upload-progress').style.display = 'none';
    document.getElementById('upload-result').style.display = 'none';
    document.getElementById('upload-result').className = 'upload-result';
    document.getElementById('upload-result').innerHTML = '';
    // Reset fill strategy to default (zero)
    const zeroRadio = document.querySelector('input[name="fill_strategy"][value="zero"]');
    if (zeroRadio) zeroRadio.checked = true;
    document.getElementById('strategy-zero-label').classList.add('selected');
    document.getElementById('strategy-mean-label').classList.remove('selected');
}

function selectStrategy(radio) {
    document.querySelectorAll('.strategy-option').forEach(el => el.classList.remove('selected'));
    radio.closest('.strategy-option').classList.add('selected');
}

function handleDragOver(e) {
    e.preventDefault();
    e.currentTarget.classList.add('drag-over');
}

function handleDragLeave(e) {
    e.currentTarget.classList.remove('drag-over');
}

function handleDrop(e) {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    const validExts = ['.csv', '.xlsx', '.xls'];
    if (file && validExts.some(ext => file.name.toLowerCase().endsWith(ext))) {
        uploadFile(file);
    } else {
        showToast('Please upload a CSV or Excel (.xlsx) file', 'error');
    }
}

function handleFileSelect(e) {
    const file = e.target.files[0];
    if (file) uploadFile(file);
}

async function uploadFile(file) {
    const dropZone = document.getElementById('drop-zone');
    const progressDiv = document.getElementById('upload-progress');
    const resultDiv = document.getElementById('upload-result');

    dropZone.style.display = 'none';
    progressDiv.style.display = 'block';

    document.getElementById('upload-filename').textContent = file.name;
    document.getElementById('upload-filesize').textContent = formatFileSize(file.size);
    document.getElementById('upload-status').textContent = 'Uploading file...';

    const progressFill = document.getElementById('progress-fill');
    progressFill.style.width = '0%';
    progressFill.style.background = '';

    try {
        const formData = new FormData();
        formData.append('file', file);

        const strategyRadio = document.querySelector('input[name="fill_strategy"]:checked');
        formData.append('fill_strategy', strategyRadio ? strategyRadio.value : 'zero');

        document.getElementById('fill-strategy-section').style.display = 'none';

        // Step 1: Upload file and get job_id
        const res = await fetch(`${API}/api/upload`, {
            method: 'POST',
            body: formData,
        });

        const uploadData = await res.json();

        if (uploadData.error) {
            throw new Error(uploadData.error);
        }

        const jobId = uploadData.job_id;
        document.getElementById('upload-status').textContent = 'Processing ETL pipeline...';
        progressFill.style.width = '5%';

        // Step 2: Poll job status until complete
        const data = await pollJobStatus(jobId, progressFill);

        if (data.status === 'complete' && data.result) {
            const result = data.result;
            document.getElementById('upload-status').textContent = 'Complete!';
            resultDiv.style.display = 'block';
            resultDiv.className = 'upload-result success';

            // Show validation warnings if present
            const validation = result.etl_report?.validation;
            let warningHtml = '';
            if (validation && validation.warnings && validation.warnings.length > 0) {
                warningHtml = '<br><small style="color: var(--warning, #fbbf24);">' +
                    validation.warnings.map(w => `&#9888; ${w}`).join('<br>') +
                    '</small>';
            }

            resultDiv.innerHTML = `
                <strong>\u2705 ${result.message}</strong><br>
                <small>Table: <code>${result.table_name}</code> \u2022 Columns: ${result.columns.join(', ')}</small>
                ${warningHtml}
            `;

            showToast(`Loaded ${result.rows_loaded.toLocaleString()} rows into "${result.table_name}"`, 'success');

            setTimeout(async () => {
                await loadTables(true);
                const select = document.getElementById('table-select');
                select.value = result.table_name;
                loadDashboard(result.table_name);
                loadHistory();
            }, 500);
        } else {
            throw new Error(data.error || data.message || 'ETL processing failed');
        }

    } catch (err) {
        progressFill.style.width = '100%';
        progressFill.style.background = 'linear-gradient(90deg, #f87171, #dc2626)';

        document.getElementById('upload-status').textContent = 'Failed';
        resultDiv.style.display = 'block';
        resultDiv.className = 'upload-result error';
        resultDiv.innerHTML = `<strong>\u274c Error:</strong> ${err.message}`;

        showToast('Upload failed: ' + err.message, 'error');
    }
}

function pollJobStatus(jobId, progressFill) {
    return new Promise((resolve, reject) => {
        const poll = setInterval(async () => {
            try {
                const res = await fetch(`${API}/api/job/${jobId}`);
                const data = await res.json();

                if (data.progress !== undefined) {
                    progressFill.style.width = data.progress + '%';
                }

                if (data.message) {
                    document.getElementById('upload-status').textContent = data.message;
                }

                if (data.status === 'complete') {
                    clearInterval(poll);
                    progressFill.style.width = '100%';
                    resolve(data);
                } else if (data.status === 'failed') {
                    clearInterval(poll);
                    reject(new Error(data.error || 'ETL processing failed'));
                }
            } catch (err) {
                clearInterval(poll);
                reject(err);
            }
        }, 600);
    });
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

/* ═══════ Download ═══════ */
function downloadCSV() {
    if (!currentTable) return;
    window.open(`${API}/api/download/${currentTable}`, '_blank');
    showToast(`Downloading ${currentTable}.csv`, 'info');
}

/* ═══════ Refresh ═══════ */
async function refreshAll() {
    showToast('Refreshing...', 'info');
    await checkHealth();
    await loadTables();
    if (currentTable) {
        await loadDashboard(currentTable);
    }
}

/* ═══════ Toast Notifications ═══════ */
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const icons = { success: '✅', error: '❌', info: 'ℹ️' };
    toast.innerHTML = `<span>${icons[type] || 'ℹ️'}</span><span>${message}</span>`;

    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'toastOut 0.4s ease-in forwards';
        setTimeout(() => toast.remove(), 400);
    }, 4000);
}

/* ═══════ AI Insights ═══════ */
async function generateInsights() {
    if (!currentTable) {
        showToast('Select a dataset first', 'error');
        return;
    }

    const btn = document.getElementById('ai-generate-btn');
    const emptyState = document.getElementById('ai-empty-state');
    const loading = document.getElementById('ai-loading');
    const report = document.getElementById('ai-report');

    // Show loading
    btn.disabled = true;
    btn.innerHTML = `<div class="ai-spinner" style="width:16px;height:16px;margin:0;border-width:2px;"></div> Analyzing...`;
    emptyState.style.display = 'none';
    report.style.display = 'none';
    loading.style.display = 'block';

    try {
        const res = await fetch(`${API}/api/ai-insights/${currentTable}`);
        const data = await res.json();

        loading.style.display = 'none';

        if (data.error) {
            throw new Error(data.error);
        }

        // Render markdown report
        report.innerHTML = marked.parse(data.report);
        report.style.display = 'block';

        showToast(`AI report generated (${data.data_points.toLocaleString()} data points analyzed)`, 'success');

    } catch (err) {
        loading.style.display = 'none';
        emptyState.style.display = 'block';
        showToast('AI report failed: ' + err.message, 'error');
        console.error('AI Insights error:', err);
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Generate Report`;
    }
}

/* ═══════ Upload History ═══════ */
async function loadHistory() {
    try {
        const res = await fetch(`${API}/api/my-history`);
        const data = await res.json();
        const body = document.getElementById('history-body');
        const card = document.getElementById('history-card');

        if (!data.uploads || data.uploads.length === 0) {
            body.innerHTML = '<p class="history-empty">No uploads yet. Upload a CSV to see your history.</p>';
            card.style.display = 'block';
            return;
        }

        card.style.display = 'block';
        body.innerHTML = data.uploads.map(u => {
            const date = new Date(u.uploaded_at).toLocaleString();
            return `
                <div class="history-item" onclick="loadDashboard('${u.table_name}')">
                    <div class="history-dot"></div>
                    <div class="history-info">
                        <div class="history-filename">${u.filename}</div>
                        <div class="history-meta">${date} | ${u.rows_loaded.toLocaleString()} rows, ${u.columns} cols</div>
                    </div>
                    <span class="history-badge">${u.table_name}</span>
                </div>
            `;
        }).join('');

    } catch (err) {
        console.log('History load error:', err);
    }
}

/* ═══════ Advanced Analytics KPIs ═══════ */
async function loadAdvancedStats(tableName) {
    try {
        const res = await fetch(`${API}/api/stats/${tableName}/advanced`);
        const data = await res.json();

        const section = document.getElementById('adv-kpi-section');
        const chartsSection = document.getElementById('adv-charts-section');

        if (!data.has_advanced) {
            section.style.display = 'none';
            chartsSection.style.display = 'none';
            return;
        }

        section.style.display = 'block';
        chartsSection.style.display = 'block';

        // Populate KPI values
        const roasEl = document.getElementById('kpi-roas');
        const cpaEl = document.getElementById('kpi-cpa');
        const engEl = document.getElementById('kpi-engagement');
        const marginEl = document.getElementById('kpi-margin');
        const anomalyEl = document.getElementById('kpi-anomalies');

        if (roasEl) roasEl.textContent = data.avg_roas != null ? data.avg_roas.toFixed(2) + 'x' : '—';
        if (cpaEl) cpaEl.textContent = data.avg_cpa != null ? '$' + data.avg_cpa.toFixed(2) : '—';
        if (engEl) engEl.textContent = data.avg_engagement_score != null ? data.avg_engagement_score.toFixed(1) : '—';
        if (marginEl) {
            const m = data.avg_profit_margin;
            marginEl.textContent = m != null ? m.toFixed(1) + '%' : '—';
            if (m != null && m < 0) marginEl.style.color = '#f87171';
            else marginEl.style.color = '';
        }

        const totalAnomalies = (data.bot_traffic_count || 0) + (data.ctr_anomaly_count || 0) + (data.outlier_count || 0);
        if (anomalyEl) {
            anomalyEl.textContent = totalAnomalies;
            const card = document.getElementById('kpi-anomaly-card');
            if (card) {
                if (totalAnomalies > 0) card.classList.add('kpi-alert');
                else card.classList.remove('kpi-alert');
            }
        }

        // Load advanced charts
        loadAdvancedCharts(tableName);

    } catch (err) {
        console.log('Advanced stats not available:', err);
        document.getElementById('adv-kpi-section').style.display = 'none';
        document.getElementById('adv-charts-section').style.display = 'none';
    }
}

async function loadAdvancedCharts(tableName) {
    await Promise.all([
        loadROASChart(tableName),
        loadChannelChart(tableName),
    ]);
    await Promise.all([
        loadEngagementChart(tableName),
        loadEfficiencyChart(tableName),
        loadProfitChart(tableName),
    ]);
    loadAnomalyTable(tableName);
}

/* ═══════ ROAS Trend Chart ═══════ */
async function loadROASChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/roas_trend`);
        const data = await res.json();
        if (!data.roas || data.roas.length === 0) return;

        if (charts.roas) charts.roas.destroy();
        const ctx = document.getElementById('chart-roas').getContext('2d');

        const gradient = ctx.createLinearGradient(0, 0, 0, 360);
        gradient.addColorStop(0, 'rgba(34, 197, 94, 0.3)');
        gradient.addColorStop(1, 'rgba(34, 197, 94, 0.0)');

        const datasets = [{
            label: 'ROAS',
            data: data.roas,
            borderColor: '#22C55E',
            backgroundColor: gradient,
            borderWidth: 2.5,
            fill: true,
            tension: 0.4,
            pointRadius: 3,
            pointBackgroundColor: '#22C55E',
        }];

        if (data.roas_ma3 && data.roas_ma3.length > 0) {
            datasets.push({
                label: '3-Day MA',
                data: data.roas_ma3,
                borderColor: '#fbbf24',
                borderWidth: 2,
                borderDash: [6, 3],
                fill: false,
                tension: 0.4,
                pointRadius: 0,
            });
        }

        charts.roas = new Chart(ctx, {
            type: 'line',
            data: { labels: data.labels, datasets },
            options: {
                ...chartDefaults,
                scales: {
                    ...chartDefaults.scales,
                    x: { ...chartDefaults.scales.x, ticks: { ...chartDefaults.scales.x.ticks, maxTicksLimit: 12, maxRotation: 0 } },
                    y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => v.toFixed(1) + 'x' } },
                }
            }
        });
    } catch (err) { console.error('ROAS chart error:', err); }
}

/* ═══════ Channel Revenue (Doughnut) ═══════ */
async function loadChannelChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/channel_revenue`);
        const data = await res.json();
        if (!data.values || data.values.length === 0) return;

        if (charts.channel) charts.channel.destroy();
        const ctx = document.getElementById('chart-channel').getContext('2d');

        const colors = ['#A855F7', '#f472b6', '#22C55E', '#fbbf24', '#38bdf8', '#94a3b8'];

        charts.channel = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: data.labels,
                datasets: [{
                    data: data.values,
                    backgroundColor: colors.slice(0, data.labels.length),
                    borderWidth: 0,
                    borderRadius: 4,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '65%',
                plugins: {
                    legend: {
                        position: 'right',
                        labels: { ...chartDefaults.plugins.legend.labels, padding: 12 }
                    },
                    tooltip: chartDefaults.plugins.tooltip,
                }
            }
        });
    } catch (err) { console.error('Channel chart error:', err); }
}

/* ═══════ Engagement Score Distribution ═══════ */
async function loadEngagementChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/engagement_distribution`);
        const data = await res.json();
        if (!data.values || data.values.length === 0) return;

        if (charts.engagement) charts.engagement.destroy();
        const ctx = document.getElementById('chart-engagement').getContext('2d');

        const gradient = ctx.createLinearGradient(0, 0, 0, 300);
        gradient.addColorStop(0, 'rgba(168, 85, 247, 0.8)');
        gradient.addColorStop(1, 'rgba(168, 85, 247, 0.15)');

        charts.engagement = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.labels,
                datasets: [{
                    label: 'Engagement Score',
                    data: data.values,
                    backgroundColor: gradient,
                    borderColor: '#A855F7',
                    borderWidth: 1,
                    borderRadius: 6,
                }]
            },
            options: {
                ...chartDefaults,
                plugins: { ...chartDefaults.plugins, legend: { display: false } },
            }
        });
    } catch (err) { console.error('Engagement chart error:', err); }
}

/* ═══════ Spend Efficiency Tiers ═══════ */
async function loadEfficiencyChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/spend_efficiency`);
        const data = await res.json();
        if (!data.values || data.values.every(v => v === 0)) return;

        if (charts.efficiency) charts.efficiency.destroy();
        const ctx = document.getElementById('chart-efficiency').getContext('2d');

        const tierColors = ['#f87171', '#fbbf24', '#818cf8', '#22C55E'];

        charts.efficiency = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.labels,
                datasets: [{
                    label: 'Campaigns',
                    data: data.values,
                    backgroundColor: tierColors,
                    borderColor: tierColors,
                    borderWidth: 1,
                    borderRadius: 6,
                    barThickness: 36,
                }]
            },
            options: {
                ...chartDefaults,
                plugins: { ...chartDefaults.plugins, legend: { display: false } },
                scales: {
                    ...chartDefaults.scales,
                    y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, stepSize: 1 } },
                }
            }
        });
    } catch (err) { console.error('Efficiency chart error:', err); }
}

/* ═══════ Profit Margin Timeline ═══════ */
async function loadProfitChart(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/profit_margin_trend`);
        const data = await res.json();
        if (!data.values || data.values.length === 0) return;

        if (charts.profit) charts.profit.destroy();
        const ctx = document.getElementById('chart-profit').getContext('2d');

        // Color bars: green if positive, red if negative
        const barColors = data.values.map(v => v >= 0 ? 'rgba(34, 197, 94, 0.7)' : 'rgba(248, 113, 113, 0.7)');
        const borderColors = data.values.map(v => v >= 0 ? '#22C55E' : '#f87171');

        charts.profit = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: data.labels,
                datasets: [{
                    label: 'Profit Margin (%)',
                    data: data.values,
                    backgroundColor: barColors,
                    borderColor: borderColors,
                    borderWidth: 1,
                    borderRadius: 4,
                }]
            },
            options: {
                ...chartDefaults,
                plugins: { ...chartDefaults.plugins, legend: { display: false } },
                scales: {
                    ...chartDefaults.scales,
                    x: { ...chartDefaults.scales.x, ticks: { ...chartDefaults.scales.x.ticks, maxTicksLimit: 10, maxRotation: 0 } },
                    y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => v + '%' } },
                }
            }
        });
    } catch (err) { console.error('Profit chart error:', err); }
}

/* ═══════ Anomaly Detection Table ═══════ */
async function loadAnomalyTable(tableName) {
    try {
        const res = await fetch(`${API}/api/chart/${tableName}/anomalies`);
        const data = await res.json();

        const card = document.getElementById('anomaly-card');
        const badge = document.getElementById('anomaly-badge');
        const body = document.getElementById('anomaly-body');

        if (data.total_flagged === 0) {
            card.style.display = 'block';
            badge.textContent = '0 flagged';
            body.innerHTML = '<p class="anomaly-clean">✅ No anomalies detected — all rows are clean!</p>';
            return;
        }

        card.style.display = 'block';
        badge.textContent = data.total_flagged + ' flagged';
        badge.classList.add('chart-badge-red');

        // Build table
        const cols = data.columns || Object.keys(data.flagged_rows[0] || {});
        let html = '<div class="anomaly-table-wrap"><table class="anomaly-table">';
        html += '<thead><tr>' + cols.map(c => `<th>${c}</th>`).join('') + '</tr></thead>';
        html += '<tbody>';
        data.flagged_rows.forEach(row => {
            html += '<tr>';
            cols.forEach(c => {
                let val = row[c];
                let cls = '';
                if (c.startsWith('is_') && val === true) cls = ' class="anomaly-flag"';
                if (typeof val === 'number') val = Number.isInteger(val) ? val.toLocaleString() : val.toFixed(4);
                if (val === true) val = '⚠️ Yes';
                if (val === false) val = '—';
                html += `<td${cls}>${val ?? ''}</td>`;
            });
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        body.innerHTML = html;

    } catch (err) {
        console.log('Anomaly data not available:', err);
    }
}
