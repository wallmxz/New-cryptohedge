function dashboard() {
    return {
        state: {
            pool_value_usd: 0, pool_deposited_usd: 0, pool_tokens: {},
            hedge_position: null, hedge_unrealized_pnl: 0, hedge_realized_pnl: 0,
            funding_total: 0, best_bid: 0, best_ask: 0, my_order: null,
            safe_mode: false, hedge_ratio: 0.95, max_exposure_pct: 0.05,
            repost_depth: 3, total_maker_fills: 0, total_taker_fills: 0,
            total_maker_volume: 0, total_taker_volume: 0,
            total_fees_paid: 0, total_fees_earned: 0,
            connected_exchange: false, connected_chain: false,
        },
        lastUpdate: '-',
        bookAsks: [],
        bookBids: [],

        get exposurePct() {
            if (this.state.pool_value_usd <= 0) return 0;
            const target = this.state.pool_value_usd * 0.5 * this.state.hedge_ratio;
            const current = this.state.hedge_position ? this.state.hedge_position.size : 0;
            return Math.abs(target - current) / this.state.pool_value_usd;
        },

        get pnl() {
            const pool = this.state.pool_value_usd - this.state.pool_deposited_usd;
            const hedge = this.state.hedge_realized_pnl + this.state.hedge_unrealized_pnl;
            const net = pool + hedge + this.state.funding_total - this.state.total_fees_paid;
            return { pool, hedge, net };
        },

        init() {
            const es = new EventSource('/sse/state');
            es.addEventListener('state-update', (e) => {
                const data = JSON.parse(e.data);
                Object.assign(this.state, data);
                this.lastUpdate = new Date().toLocaleTimeString();
                if (window.updateChart) window.updateChart(data);
            });
            if (typeof initialSnapshots !== 'undefined' && window.initChart) window.initChart(initialSnapshots);
        }
    };
}
