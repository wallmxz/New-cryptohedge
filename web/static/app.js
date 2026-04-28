function dashboard() {
    return {
        activeTab: 'painel',
        showSettings: false,
        settingsTab: 'trading',

        state: {
            pool_value_usd: 0, pool_deposited_usd: 0, pool_tokens: {},
            hedge_position: null, hedge_unrealized_pnl: 0, hedge_realized_pnl: 0,
            funding_total: 0, best_bid: 0, best_ask: 0, my_order: null,
            safe_mode: false, hedge_ratio: 0.95, max_exposure_pct: 0.05,
            repost_depth: 3, total_maker_fills: 0, total_taker_fills: 0,
            total_maker_volume: 0, total_taker_volume: 0,
            total_fees_paid: 0, total_fees_earned: 0,
            connected_exchange: false, connected_chain: false,
            last_update: 0,
            range_lower: 0, range_upper: 0, liquidity_l: 0,
            dydx_collateral: 0, margin_ratio: 999, out_of_range: false,
            current_grid: [],
        },

        config: {
            arbitrum_rpc_url: '',
            arbitrum_rpc_fallback: '',
            clm_vault_address: '',
            clm_pool_address: '',
            wallet_address: '',
            active_exchange: 'hyperliquid',
            symbol: 'ARB',
            alert_webhook_url: '',
            pool_token0_symbol: 'ARB',
            pool_token1_symbol: 'USDC',
            max_open_orders: 200,
            threshold_aggressive: 0.05,
            threshold_recovery: 0.02,
        },

        logs: [],
        lastUpdate: '—',

        get hasBook() {
            return this.state.best_bid > 0 && this.state.best_ask > 0;
        },

        get spreadTicks() {
            if (!this.hasBook) return 0;
            return (this.state.best_ask - this.state.best_bid) * 10000;
        },

        get myBid() {
            return this.state.my_order && this.state.my_order.side === 'buy' ? this.state.my_order : null;
        },

        get myAsk() {
            return this.state.my_order && this.state.my_order.side === 'sell' ? this.state.my_order : null;
        },

        get tokenBase() {
            return this.state.pool_tokens[this.config.pool_token0_symbol] || 0;
        },

        get exposurePct() {
            if (this.tokenBase <= 0) return 0;
            const target = this.tokenBase * this.state.hedge_ratio;
            const current = this.state.hedge_position ? this.state.hedge_position.size : 0;
            return Math.abs(target - current) / this.tokenBase;
        },

        get pnl() {
            const pool = this.state.pool_value_usd - this.state.pool_deposited_usd;
            const hedge = this.state.hedge_realized_pnl + this.state.hedge_unrealized_pnl;
            const net = pool + hedge + this.state.funding_total - this.state.total_fees_paid;
            return { pool, hedge, net };
        },

        init() {
            fetch('/config')
                .then(r => r.json())
                .then(data => Object.assign(this.config, data))
                .catch(() => {});

            const es = new EventSource('/sse/state');
            es.addEventListener('state-update', (e) => {
                const data = JSON.parse(e.data);
                for (const key of Object.keys(this.state)) {
                    if (key in data) this.state[key] = data[key];
                }
                this.lastUpdate = new Date(this.state.last_update * 1000).toLocaleTimeString();
                if (window.updateChart) window.updateChart(data);
            });

            const esLogs = new EventSource('/sse/logs');
            esLogs.addEventListener('new-log', (e) => {
                const entry = JSON.parse(e.data);
                this.logs.unshift(entry);
                if (this.logs.length > 100) this.logs.pop();
            });

            if (typeof initialLogs !== 'undefined') this.logs = initialLogs.slice(0, 100);

            if (typeof initialSnapshots !== 'undefined' && window.initChart) {
                window.initChart(initialSnapshots);
            }
        }
    };
}
