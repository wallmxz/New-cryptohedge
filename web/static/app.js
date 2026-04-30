function dashboard() {
    return {
        activeTab: 'painel',
        showSettings: false,
        settingsTab: 'trading',

        state: {
            pool_value_usd: 0, pool_tokens: {},
            hedge_position: null, hedge_unrealized_pnl: 0, hedge_realized_pnl: 0,
            funding_total: 0, best_bid: 0, best_ask: 0, my_order: null,
            safe_mode: false, hedge_ratio: 0.95,
            total_maker_fills: 0, total_taker_fills: 0,
            total_maker_volume: 0, total_taker_volume: 0,
            total_fees_paid: 0, total_fees_earned: 0,
            connected_exchange: false, connected_chain: false,
            last_update: 0,
            range_lower: 0, range_upper: 0, liquidity_l: 0,
            dydx_collateral: 0, margin_ratio: 999, out_of_range: false,
            current_grid: [],
            current_operation_id: null,
            operation_state: "none",
            operation_pnl_breakdown: {},
            last_iter_timings: {},
            wallet_eth_balance: 0,
            weth_balance: 0,
            bootstrap_progress: '',
            bootstrap_swap_tx_hash: null,
            bootstrap_deposit_tx_hash: null,
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
            threshold_aggressive: 0.01,
            slippage_bps: 30,
        },

        logs: [],
        lastUpdate: '—',
        _opStartedAt: null,
        history: [],

        showStartModal: false,
        startBudget: 300.0,
        startBudgetMax: 0.0,

        showPairPicker: false,
        pairsData: { usd_pairs: [], cross_pairs: [], selected_vault_id: null, last_refresh_ts: 0 },
        pairSearch: '',
        pairSort: 'apy',
        pairRefreshing: false,
        pairsLastRefresh: 0,

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
            const b = this.state.operation_pnl_breakdown || {};
            const pool = (b.lp_fees_earned || 0) + (b.beefy_perf_fee || 0) + (b.il_natural || 0);
            const hedge = b.hedge_pnl || 0;
            const net = b.net_pnl || 0;
            return { pool, hedge, net };
        },

        get op() {
            const b = this.state.operation_pnl_breakdown || {};
            const netPnl = b.net_pnl || 0;
            const breakdown = [
                { label: "LP fees recebidas", value: b.lp_fees_earned || 0 },
                { label: "Beefy perf fee", value: b.beefy_perf_fee || 0 },
                { label: "IL natural", value: b.il_natural || 0 },
                { label: "Hedge PnL", value: b.hedge_pnl || 0 },
                { label: "Funding", value: b.funding || 0 },
                { label: "Perp fees", value: b.perp_fees_paid || 0 },
                { label: "Bootstrap slippage", value: b.bootstrap_slippage || 0 },
            ];
            return {
                elapsed: this._formatElapsed(),
                breakdown: breakdown,
                netPnl: netPnl,
            };
        },

        get healthSteps() {
            const t = this.state.last_iter_timings || {};
            const order = [
                ["chain_read", "Read chain"],
                ["margin_check", "Margin check"],
                ["grid_compute", "Compute grid"],
                ["grid_diff_apply", "Place/cancel"],
                ["pnl_breakdown", "PnL breakdown"],
                ["total", "Total"],
            ];
            const out = [];
            for (const [name, label] of order) {
                if (name in t) out.push({ name, label, ms: t[name] });
            }
            return out;
        },

        get filteredUsdPairs() {
            return this._filterAndSort(this.pairsData.usd_pairs);
        },

        get filteredCrossPairs() {
            return this._filterAndSort(this.pairsData.cross_pairs);
        },

        get selectedPairLabel() {
            const sel = this.pairsData.selected_vault_id;
            if (!sel) return null;
            const all = [...this.pairsData.usd_pairs, ...this.pairsData.cross_pairs];
            const p = all.find(x => x.vault_id === sel);
            if (!p) return sel.slice(0, 10) + '...';
            return p.pair + ' (' + p.manager + ')';
        },

        _formatElapsed() {
            if (!this._opStartedAt) return "";
            const sec = Math.max(0, (Date.now() / 1000) - this._opStartedAt);
            const h = Math.floor(sec / 3600);
            const m = Math.floor((sec % 3600) / 60);
            return h + "h " + m + "min";
        },

        async startOperation() {
            try {
                const resp = await fetch("/operations/start", { method: "POST" });
                if (!resp.ok) {
                    const err = await resp.json();
                    alert("Erro ao iniciar: " + (err.error || resp.status));
                }
            } catch (e) {
                alert("Erro: " + e);
            }
        },

        async openStartModal() {
            try {
                const resp = await fetch("/wallet");
                if (resp.ok) {
                    const data = await resp.json();
                    this.startBudgetMax = data.usdc_balance || 0;
                    if (this.startBudgetMax > 0) {
                        this.startBudget = Math.floor(this.startBudgetMax);
                    }
                }
            } catch (e) {}
            this.showStartModal = true;
        },

        async confirmStart() {
            try {
                const resp = await fetch("/operations/start", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({usdc_budget: this.startBudget}),
                });
                if (!resp.ok) {
                    const err = await resp.json();
                    alert("Erro ao iniciar: " + (err.error || resp.status));
                    return;
                }
                this.showStartModal = false;
            } catch (e) {
                alert("Erro: " + e);
            }
        },

        async stopOperation() {
            try {
                const resp = await fetch("/operations/stop", { method: "POST" });
                if (!resp.ok) {
                    const err = await resp.json();
                    alert("Erro ao encerrar: " + (err.error || resp.status));
                }
            } catch (e) {
                alert("Erro: " + e);
            }
        },

        async loadHistory() {
            try {
                const resp = await fetch("/operations?limit=50");
                if (resp.ok) this.history = await resp.json();
            } catch (e) {
                console.error("Failed to load history:", e);
            }
        },

        async cashOut() {
            if (!confirm("Converter WETH residual em USDC? (slippage 0.3%)")) return;
            try {
                const resp = await fetch("/operations/cashout", {method: "POST"});
                const data = await resp.json();
                if (resp.ok) {
                    alert("Swap enviado! Tx: " + (data.tx_hash || "(no WETH to swap)"));
                } else {
                    alert("Erro: " + (data.error || resp.status));
                }
            } catch (e) {
                alert("Erro: " + e);
            }
        },

        async refreshWallet() {
            try {
                const resp = await fetch("/wallet");
                if (resp.ok) {
                    const data = await resp.json();
                    this.state.weth_balance = data.weth_balance || 0;
                    this.state.wallet_eth_balance = data.eth_balance || 0;
                }
            } catch (e) {}
        },

        _filterAndSort(list) {
            let out = list || [];
            if (this.pairSearch) {
                const q = this.pairSearch.toLowerCase();
                out = out.filter(p => (p.pair || '').toLowerCase().includes(q));
            }
            const sort = this.pairSort;
            out = [...out].sort((a, b) => {
                if (sort === 'apy') return (b.apy_30d || 0) - (a.apy_30d || 0);
                if (sort === 'tvl') return (b.tvl_usd || 0) - (a.tvl_usd || 0);
                if (sort === 'pair') return (a.pair || '').localeCompare(b.pair || '');
                return 0;
            });
            return out;
        },

        formatTvl(v) {
            if (!v) return '—';
            if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
            if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
            if (v >= 1e3) return '$' + (v / 1e3).toFixed(0) + 'K';
            return '$' + v.toFixed(0);
        },

        formatApy(v) {
            if (v == null) return 'N/A';
            return (v * 100).toFixed(2) + '%';
        },

        apyColorClass(v) {
            if (v == null) return 'text-slate-400';
            if (v >= 0.6) return 'apy-high';
            if (v >= 0.3) return 'apy-medium';
            return 'apy-low';
        },

        formatRelativeTime(ts) {
            if (!ts) return 'nunca';
            const sec = Math.floor(Date.now() / 1000) - ts;
            if (sec < 60) return sec + 's atrás';
            if (sec < 3600) return Math.floor(sec / 60) + 'min atrás';
            if (sec < 86400) return Math.floor(sec / 3600) + 'h atrás';
            return Math.floor(sec / 86400) + 'd atrás';
        },

        async openPairPicker() {
            this.showPairPicker = true;
            await this.loadPairs();
        },

        async loadPairs() {
            try {
                const resp = await fetch('/pairs');
                if (resp.ok) {
                    const data = await resp.json();
                    this.pairsData = data;
                    this.pairsLastRefresh = data.last_refresh_ts || 0;
                }
            } catch (e) {}
        },

        async refreshPairs() {
            this.pairRefreshing = true;
            try {
                const resp = await fetch('/pairs/refresh', { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok) {
                    alert('Erro ao atualizar: ' + (data.error || resp.status));
                } else {
                    await this.loadPairs();
                }
            } catch (e) {
                alert('Erro: ' + e);
            }
            this.pairRefreshing = false;
        },

        async selectPair(p) {
            if (!p.selectable) {
                alert('Não selecionável: ' + (p.reason || ''));
                return;
            }
            if (p.vault_id === this.pairsData.selected_vault_id) {
                return;
            }
            if (!confirm('Selecionar par ' + p.pair + ' (' + p.manager + ')?')) return;
            try {
                const resp = await fetch('/pairs/select', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ vault_id: p.vault_id }),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    alert('Erro: ' + (data.error || resp.status));
                    return;
                }
                this.pairsData.selected_vault_id = p.vault_id;
                alert('Par selecionado! Aplica na próxima operação.');
            } catch (e) {
                alert('Erro: ' + e);
            }
        },

        init() {
            fetch('/config')
                .then(r => r.json())
                .then(data => Object.assign(this.config, data))
                .catch(() => {});

            fetch('/operations/current')
                .then(r => r.status === 204 ? null : r.json())
                .then(data => {
                    if (data) this._opStartedAt = data.started_at;
                })
                .catch(() => {});

            this.loadPairs();

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

            this.refreshWallet();
            setInterval(() => this.refreshWallet(), 30000);
        }
    };
}
