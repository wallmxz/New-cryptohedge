let chart = null;
let chartData = [[], [], [], []];

function renderEmptyChart(container) {
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#94a3b8;font-size:0.85rem;">Sem historico ainda</div>';
}

function initChart(snapshots) {
    const container = document.getElementById('chart-container');
    if (!container) return;

    container.innerHTML = '';
    chartData = [[], [], [], []];

    for (const s of snapshots) {
        chartData[0].push(s.timestamp);
        chartData[1].push(s.pool_pnl || 0);
        chartData[2].push(-(s.hedge_pnl || 0));
        chartData[3].push(s.net_pnl || 0);
    }

    if (chartData[0].length === 0) {
        chart = null;
        renderEmptyChart(container);
        return;
    }

    const w = container.clientWidth - 16;
    const h = 250;

    const opts = {
        width: w,
        height: h,
        cursor: { show: true },
        scales: { x: { time: true }, y: { auto: true } },
        axes: [
            { stroke: '#cbd5e1', grid: { stroke: '#f1f5f9', width: 1 }, font: '11px Inter', ticks: { stroke: '#e2e8f0' } },
            { stroke: '#cbd5e1', grid: { stroke: '#f1f5f9', width: 1 }, font: '11px Inter', ticks: { stroke: '#e2e8f0' },
              values: (u, vals) => vals.map(v => '$' + v.toFixed(2)) },
        ],
        series: [
            {},
            { label: 'PnL da Pool', stroke: '#6366f1', width: 2, fill: 'rgba(99,102,241,0.06)' },
            { label: 'PnL do Hedge x-1', stroke: '#ef4444', width: 2, fill: 'rgba(239,68,68,0.06)' },
            { label: 'PnL Liquido', stroke: '#10b981', width: 2, fill: 'rgba(16,185,129,0.06)' },
        ],
    };

    chart = new uPlot(opts, chartData, container);
    window.addEventListener('resize', () => {
        if (chart && container.clientWidth > 0) chart.setSize({ width: container.clientWidth - 16, height: h });
    });
}

function updateChart(state) {
    if (!chart) return;
    if (!state.last_update) return;

    const now = state.last_update;
    const poolPnl = state.pool_value_usd - state.pool_deposited_usd;
    const hedgePnl = -(state.hedge_realized_pnl + state.hedge_unrealized_pnl + state.funding_total);
    const netPnl = poolPnl + state.hedge_realized_pnl + state.hedge_unrealized_pnl + state.funding_total - state.total_fees_paid;

    chartData[0].push(now);
    chartData[1].push(poolPnl);
    chartData[2].push(hedgePnl);
    chartData[3].push(netPnl);

    if (chartData[0].length > 5000) {
        for (let i = 0; i < 4; i++) chartData[i].shift();
    }
    chart.setData(chartData);
}

window.initChart = initChart;
window.updateChart = updateChart;
