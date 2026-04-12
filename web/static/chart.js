let chart = null;
let chartData = [[], [], [], []];

function initChart(snapshots) {
    const container = document.getElementById('chart-container');
    if (!container) return;

    chartData = [[], [], [], []];
    for (const s of snapshots) {
        chartData[0].push(s.timestamp);
        chartData[1].push(s.pool_pnl || 0);
        chartData[2].push(-(s.hedge_pnl || 0));
        chartData[3].push(s.net_pnl || 0);
    }

    const opts = {
        width: container.clientWidth,
        height: 280,
        scales: { x: { time: true }, y: {} },
        axes: [
            { stroke: '#555', grid: { stroke: '#1a1a2e' } },
            { stroke: '#555', grid: { stroke: '#1a1a2e' } },
        ],
        series: [
            {},
            { label: 'Pool PnL', stroke: '#5b86e5', width: 2 },
            { label: 'Hedge PnL x-1', stroke: '#ff4757', width: 2 },
            { label: 'Net PnL', stroke: '#00d4aa', width: 2 },
        ],
    };

    chart = new uPlot(opts, chartData, container);
    window.addEventListener('resize', () => chart.setSize({ width: container.clientWidth, height: 280 }));
}

function updateChart(state) {
    if (!chart) return;
    const now = state.last_update || Date.now() / 1000;
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
