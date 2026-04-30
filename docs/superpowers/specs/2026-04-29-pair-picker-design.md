# Pair Picker — Design Spec

## Objetivo

Permitir que o usuário escolha qual par WETH/USDC, ARB/USDC, etc operar diretamente no UI do app, ao invés de editar `.env` + reiniciar. Lista de pares vem da Beefy API (live discovery), filtrada por compatibilidade com perp na dYdX, e separada visualmente em "USD-Pairs" (suportadas) e "Cross-Pairs" (Phase 3.x — display-only).

**Após implementação:**
- Settings → tab "Par" mostra cards estilo Beefy: token logos, APY 30d, TVL, range visualizer, manager badge
- Usuário clica "Select", bot persiste em DB
- Próxima operação roda com o par selecionado (sem reiniciar servidor)
- Cross-pairs visíveis mas grayed-out com aviso

**Fora de escopo:**
- Multi-pair simultâneo (mantém Phase 1.1: single concurrent operation)
- Cross-pair OPERACIONAL (Phase 3.x — requer hedge dual-leg)
- Multi-chain (mantém Arbitrum-only)
- Discovery em outras APIs (só Beefy)
- Hedge de dois ativos voláteis (token1 deve ser stable)

## Decisões de design

### 1. Single concurrent operation (preservado)

Usuário roda 1 operação por vez. "Trocar par" = encerrar atual + escolher novo + iniciar novo. Bot recusa Start enquanto operação anterior está ativa (já recusa hoje). Pair switch é decoupled do lifecycle: você pode mudar a config no Settings a qualquer momento, mas só vale a partir do próximo Start.

### 2. Discovery via Beefy API + dYdX indexer

Beefy mantém em `https://api.beefy.finance/cows` (CLM data) + `/apy/breakdown` (yields) + `/tvl` (TVLs). dYdX expõe `https://indexer.dydx.trade/v4/perpetualMarkets` com lista de perps disponíveis.

Fluxo:
1. Bot fetcha Beefy + dYdX no startup, salva no cache local (DB)
2. Filtra Beefy CLMs Arbitrum cujo token0 tem perp correspondente na dYdX
3. Classifica resultado: USD-Pair (token1 ∈ stables) vs Cross-Pair (token1 não-stable)
4. UI mostra os dois grupos
5. Botão Refresh força re-fetch

### 3. Filtering: USD-Pair vs Cross-Pair

```python
STABLECOINS_ARBITRUM = {
    "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC native
    "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT
    "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",  # USDC.e (legacy bridged)
    "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",  # DAI
}

DYDX_TOKEN_TO_PERP = {
    "WETH": "ETH-USD", "WBTC": "BTC-USD", "ARB": "ARB-USD",
    "GMX": "GMX-USD", "LINK": "LINK-USD", "SOL": "SOL-USD",
    "AVAX": "AVAX-USD", "MATIC": "MATIC-USD", "OP": "OP-USD",
    # extensível conforme dYdX adicionar perps; mapping wrapped→symbol
}

def classify(beefy_clm, dydx_markets):
    token0_perp = DYDX_TOKEN_TO_PERP.get(beefy_clm.token0_symbol)
    if not token0_perp or token0_perp not in dydx_markets:
        return None  # descarta (sem perp)
    is_usd_pair = beefy_clm.token1_address in STABLECOINS_ARBITRUM
    return ("usd_pair" if is_usd_pair else "cross_pair", token0_perp)
```

Cross-pairs entram na lista pra display mas com `selectable=False`, motivo "Phase 3.x — hedge dual-leg requerido".

### 4. Hot-reload (no restart)

Pair config persistida em `config` table (DB). `engine/lifecycle.py` é instanciado per-operation no `start_operation` em vez de singleton no startup do app. Lifecycle factory lê pair atual do DB, cria UniswapExecutor + BeefyExecutor com endereços corretos do par escolhido.

Alternativa rejeitada: Restart-required (mais simples mas adiciona fricção a cada switch).

### 5. UI: cards estilo Beefy

Substitui a Settings → "Trading" tab por uma "Par" tab com:
- Pair atual destacado no topo ("Selected: ETH-USDC Bell, $5.21M TVL, 28.4% APY")
- Cards listados, cada um com:
  - Token logos (hotlinkados de https://api.beefy.finance/token/...)
  - Pair name
  - Manager badge (Bell / Wide / Narrow / Tight / Adaptive)
  - DEX badge (Uniswap V3 com fee tier)
  - Range visualizer (mini-bar com lower / current / upper)
  - TVL formatado ($5.21M, $850K)
  - APY 30d com cor escalada (verde claro <30%, médio 30-60%, escuro+🔥 >60%)
  - Botão "Select"
- Search bar (filtra por nome)
- Sort dropdown (APY desc default; TVL desc; Range tight first)
- Refresh button + "last refresh: X min ago"
- Cross-pairs separados em seção própria, grayed-out, com aviso

Token logos hotlinked, fallback emoji se 404. Cache local de logos NÃO no MVP (feature 3.x).

### 6. Refactor pré-requisito

Phase 2.0 hardcoded `WETH_TOKEN_ADDRESS` e `USDC_TOKEN_ADDRESS` no `.env`. Pra suportar pares variáveis, precisa renomear para genérico:

```env
TOKEN0_ADDRESS=...    # token volátil (WETH, ARB, WBTC, ...)
TOKEN1_ADDRESS=...    # stable (USDC, USDT, USDC.e, DAI)
TOKEN0_DECIMALS=18
TOKEN1_DECIMALS=6
```

Estes ficam **read-only** no `.env` como fallback default; o pair picker sobrescreve em runtime via DB. Quando usuário seleciona um par via UI:
- Bot lê metadata da Beefy CLM (token0 addr, token1 addr, decimals, fee tier)
- Persiste no DB junto com `selected_vault_id`
- Próximo `start_operation` lê do DB

### 7. Validation

Quando usuário seleciona um vault:
1. Vault deve existir no cache Beefy (descarta se não)
2. Token1 deve estar em `STABLECOINS_ARBITRUM`
3. Token0 symbol deve mapear pra `DYDX_TOKEN_TO_PERP` E o perp deve estar na lista dYdX cached
4. Token0 e Token1 decimals devem ser válidos (1-30)
5. Pool address (CLM_POOL_ADDRESS extraído do Beefy data) deve ser checksum address válido

Se alguma falhar: rejeita seleção com mensagem específica. Não persiste.

## Arquitetura

### Módulos novos

```
chains/
  beefy_api.py            # fetcher + cache pra api.beefy.finance
  dydx_markets.py         # fetcher + cache pra indexer.dydx.trade

engine/
  pair_resolver.py        # join Beefy + dYdX → classified pair list
  pair_factory.py         # build UniswapExecutor + BeefyExecutor + readers
                          # pra um vault_id específico

config/
  stables.py              # STABLECOINS_ARBITRUM, DYDX_TOKEN_TO_PERP
```

### Módulos modificados

- `db.py` — tabelas novas: `beefy_pairs_cache`, `dydx_markets_cache`; helper `get_selected_vault_id()` / `set_selected_vault_id()`
- `config.py` — rename `weth_token_address`/`usdc_token_address` → `token0_address`/`token1_address`; add `token0_decimals`/`token1_decimals`
- `engine/lifecycle.py` — `bootstrap()` lê selected_vault_id do DB; instancia executors via `pair_factory`
- `engine/__init__.py` — `start_operation` chama `pair_factory.build_lifecycle()` em vez de usar singleton
- `app.py` — lifespan não cria mais `lifecycle` upfront; cria placeholder; lifecycle real é construído per-operation
- `web/routes.py` — endpoints novos: `GET /pairs`, `POST /pairs/select`, `POST /pairs/refresh`, `GET /pairs/refresh-status`
- `web/templates/partials/pair_picker.html` — novo (substitui parte da settings.html Trading tab)
- `web/templates/partials/settings.html` — Trading tab vira "Par" + Trading minimal (só hedge_ratio, threshold)
- `web/static/app.js` — pair fetch, search, sort, select; rendering de cards Beefy-style
- `web/static/app.css` — styling de cards Beefy-style (range bar, APY colors, manager badge)
- `tests/test_beefy_api.py`, `tests/test_dydx_markets.py`, `tests/test_pair_resolver.py`, `tests/test_pair_factory.py` — novos
- `.env.example` — atualiza vars (TOKEN0/1_ADDRESS substituem WETH/USDC)

### Schema DB

```sql
-- Beefy CLMs cache (atualizado em refresh)
CREATE TABLE IF NOT EXISTS beefy_pairs_cache (
    vault_id TEXT PRIMARY KEY,
    chain TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    token0_address TEXT NOT NULL,
    token0_symbol TEXT NOT NULL,
    token0_decimals INTEGER NOT NULL,
    token1_address TEXT NOT NULL,
    token1_symbol TEXT NOT NULL,
    token1_decimals INTEGER NOT NULL,
    pool_fee INTEGER NOT NULL,
    manager TEXT,
    tick_lower INTEGER,
    tick_upper INTEGER,
    tvl_usd REAL,
    apy_30d REAL,
    is_usd_pair INTEGER NOT NULL,
    dydx_perp TEXT,
    fetched_at REAL NOT NULL
);

-- dYdX markets cache (refresh menos frequente)
CREATE TABLE IF NOT EXISTS dydx_markets_cache (
    ticker TEXT PRIMARY KEY,
    status TEXT,
    fetched_at REAL NOT NULL
);

-- Selected pair (key/value no config table existente)
INSERT OR REPLACE INTO config (key, value) VALUES ('selected_vault_id', '0x...');
```

### REST API

#### `GET /pairs`

Returns:
```json
{
  "usd_pairs": [
    {
      "vault_id": "0x...",
      "pair": "ETH-USDC",
      "token0_symbol": "WETH",
      "token1_symbol": "USDC",
      "manager": "Bell",
      "dex": "Uniswap V3",
      "pool_fee_pct": 0.05,
      "tvl_usd": 5210000,
      "apy_30d": 0.2842,
      "range_lower_price": 2400,
      "range_upper_price": 3600,
      "current_price": 3000,
      "token0_logo_url": "https://api.beefy.finance/token/arbitrum/WETH",
      "token1_logo_url": "https://api.beefy.finance/token/arbitrum/USDC",
      "selectable": true,
      "dydx_perp": "ETH-USD"
    }
  ],
  "cross_pairs": [
    {
      "vault_id": "0x...",
      "pair": "WETH-ARB",
      "selectable": false,
      "reason": "Phase 3.x — cross-pair requires dual-leg hedge"
    }
  ],
  "selected_vault_id": "0x...",
  "last_refresh_ts": 1730000000
}
```

#### `POST /pairs/select`

Body: `{"vault_id": "0x..."}`

Validates + persists. Returns 200 com pair info, 400 com erro se inválido.

#### `POST /pairs/refresh`

Re-fetcha Beefy + dYdX APIs, atualiza caches. Returns 200 com novo `last_refresh_ts`. Timeout 30s.

#### `GET /pairs/refresh-status`

Status assincrono se refresh está em andamento (pra UI mostrar spinner).

### Lifecycle factory

```python
# engine/pair_factory.py
async def build_lifecycle(
    *, settings, hub, db, exchange,
    selected_vault_id: str,
    w3: AsyncWeb3, account: LocalAccount,
) -> OperationLifecycle:
    """Build OperationLifecycle for a specific Beefy CLM."""
    pair = await db.get_pair_from_cache(selected_vault_id)
    if pair is None:
        raise ValueError(f"Vault {selected_vault_id} not in cache; refresh first")
    if not pair["is_usd_pair"]:
        raise ValueError(f"Vault {selected_vault_id} is cross-pair; not selectable")

    pool_reader = UniswapV3PoolReader(
        w3=w3, pool_address=pair["pool_address"],
        decimals0=pair["token0_decimals"], decimals1=pair["token1_decimals"],
    )
    beefy_reader = BeefyClmReader(
        w3=w3, strategy_address=selected_vault_id,
        wallet_address=settings.wallet_address,
        decimals0=pair["token0_decimals"], decimals1=pair["token1_decimals"],
    )
    uniswap_exec = UniswapExecutor(
        w3=w3, account=account,
        router_address=settings.uniswap_v3_router_address,
    )
    beefy_exec = BeefyExecutor(
        w3=w3, account=account, strategy_address=selected_vault_id,
    )

    # Patch settings com dados do par escolhido
    pair_settings = dataclasses.replace(
        settings,
        token0_address=pair["token0_address"],
        token1_address=pair["token1_address"],
        token0_decimals=pair["token0_decimals"],
        token1_decimals=pair["token1_decimals"],
        clm_vault_address=selected_vault_id,
        clm_pool_address=pair["pool_address"],
        uniswap_v3_pool_fee=pair["pool_fee"],
        dydx_symbol=pair["dydx_perp"],
    )

    return OperationLifecycle(
        settings=pair_settings, hub=hub, db=db,
        exchange=exchange, uniswap=uniswap_exec, beefy=beefy_exec,
        pool_reader=pool_reader, beefy_reader=beefy_reader,
        decimals0=pair["token0_decimals"],
        decimals1=pair["token1_decimals"],
    )
```

`engine.GridMakerEngine.start_operation` chama `pair_factory.build_lifecycle()` antes de delegar.

## Failure modes e mitigações

### F1 — Beefy API down ao iniciar bot

- **Estado:** cache pode estar stale ou vazio
- **Mitigação:** se cache existe, usa stale (UI mostra "last refresh: 2h ago" como aviso). Se cache vazio (primeira execução), UI mostra "Beefy API unavailable; try Refresh later". Não bloqueia uvicorn de subir.

### F2 — dYdX indexer down

- **Estado:** mesmo do F1; pares mostrados podem incluir alguns sem perp realmente disponível
- **Mitigação:** stale cache OK; bot tenta no próximo refresh

### F3 — Token logo URL retorna 404

- **Estado:** logo aparece quebrado
- **Mitigação:** `<img onerror="this.replaceWith('💎')">` fallback emoji genérico. Não bloqueia funcionalidade.

### F4 — Usuário seleciona vault que sumiu da Beefy (vault foi pausado/depreciado)

- **Estado:** `start_operation` falha porque pair_factory não acha no cache
- **Mitigação:** start retorna erro claro "Vault no longer available; pick another pair". Pre-emptivamente, UI marca pair como "stale" se `fetched_at` é mais antigo que algum threshold.

### F5 — TVL ou APY zerados (vault novo, sem dados ainda)

- **Estado:** display mostra "$0 / N/A"
- **Mitigação:** não filtra vaults novos; mostra com placeholder. Usuário decide.

### F6 — User troca pair com operação ativa

- **Estado:** pair no DB mudou mas operação atual usa pair antigo
- **Mitigação:** start_operation rejeita ("Already active") como já faz hoje. Pair já persistido é usado no próximo Start, não retroativamente. UI pode adicionar aviso laranja "Pair changed; will apply on next operation".

## Wallet & decimals constraint

Phase 2.0 assumiu decimals0=18, decimals1=6 (WETH/USDC). Pair picker amplia: cada par tem decimals próprios, lidos da Beefy data.

Ainda assim, pra MVP, se algum par tiver decimals exóticos (ex: token de 6 decimais como token0), o `compute_optimal_split` está numericamente correto mas o display formatting precisa cuidar. Pra simplicidade do MVP: filtrar pra `token0_decimals == 18` E `token1_decimals == 6` (USDC). Outros decimals podem ser feature 3.x.

(**Nota:** maioria dos volátil-USD pairs caem nesse caso. Exceções como WBTC-USDC têm WBTC com 8 decimais — vai precisar suportar isso eventualmente. Por ora, descartar e flag pra futuro.)

## UI changes

### Settings → "Par" tab (nova, substitui parte da Trading)

Card layout per spec acima:
- Header: pair atual destacado
- Dois grupos: USD-Pairs (selectable) e Cross-Pairs (disabled + reason)
- Search + Sort + Refresh
- Cada card: logos, pair, manager, dex, range visualizer, TVL, APY badge

### Settings → "Trading" tab (mantém o restante)

Hedge ratio, max open orders, threshold aggressive, slippage display. Sem mais campo de pair.

### Operation card

Sem mudança — já mostra range_lower/upper baseado em `state.range_lower`/`range_upper` que vem da Beefy reader. Funciona com qualquer pair.

## Testing strategy

### Unit tests

- `tests/test_beefy_api.py` — fetch + cache, mock httpx
- `tests/test_dydx_markets.py` — fetch + filter active markets, mock httpx
- `tests/test_pair_resolver.py` — classify USD/cross, validation logic
- `tests/test_pair_factory.py` — lifecycle construction com pair data válido + inválido

### Integration tests

- `tests/test_pair_picker_integration.py` — `GET /pairs` retorna estrutura correta com mock data; `POST /pairs/select` valida + persiste; `POST /pairs/refresh` atualiza cache

### Backwards compat

- Existing tests (test_engine_grid, test_lifecycle, etc) continuam passando: lifecycle factory tem fallback que usa `.env` settings se nenhum pair selecionado (preserva path Phase 2.0)

## Tasks estimadas

```
T0  - Refactor: WETH/USDC_TOKEN_ADDRESS → TOKEN0/1_ADDRESS (rename mecanico)
       + TOKEN0/1_DECIMALS settings; engine/lifecycle.py + tests
T1  - config/stables.py: STABLECOINS_ARBITRUM + DYDX_TOKEN_TO_PERP
T2  - chains/dydx_markets.py: fetch indexer perpetualMarkets + DB cache
T3  - chains/beefy_api.py: fetch /cows + /apy/breakdown + /tvl + DB cache
T4  - engine/pair_resolver.py: classify USD/cross + filter dYdX-compat
T5  - DB schema: beefy_pairs_cache + dydx_markets_cache + helpers
T6  - engine/pair_factory.py: build_lifecycle per vault_id
T7  - engine/__init__.py: GridMakerEngine.start_operation usa pair_factory
T8  - REST API: GET /pairs, POST /pairs/select, POST /pairs/refresh
T9  - app.py: lazy lifecycle (factory pattern em vez de singleton)
T10 - UI: web/templates/partials/pair_picker.html (cards Beefy-style)
T11 - UI: app.css styling (range bar, APY colors, badges)
T12 - UI: app.js search + sort + select handlers
T13 - Tests: test_beefy_api, test_dydx_markets, test_pair_resolver,
       test_pair_factory, test_pair_picker_integration
T14 - Tag fase-pair-picker-completa + CLAUDE.md update
```

15 tasks. ~400 LoC novos + 100 LoC modificados.

## Riscos

| Risco | Mitigação |
|---|---|
| Beefy API schema muda | Wrapper module com schema versioning; testa contra fixture salvo |
| Token logo URL pattern muda | Fallback emoji + monitora 404 rate |
| Vault deprecated mid-operation | Operação ativa não afetada (lê do op_row); next start mostra erro |
| Decimals exóticos quebram math | Filter pra (18, 6) no MVP; extensão futura |
| dYdX indexer rate-limit | Cache 1h+ entre refreshes; manual refresh button só via user click |
| Cross-pair displayed mas não selectable confunde user | Aviso explícito + opacity reduzida + tooltip ao hover |
| Pair stale (Beefy não atualizado) faz user pegar yield desatualizado | "last refresh" timestamp visível + recommend refresh se >1h |

## Critérios de aceitação

1. Settings → "Par" tab mostra cards Beefy-style com USD-Pairs selecionáveis e Cross-Pairs grayed-out
2. `GET /pairs` retorna lista categorizada com TVL + APY + range + tokens
3. `POST /pairs/select` valida + persiste; rejeita cross-pair, vault inexistente, decimals inválidos
4. `POST /pairs/refresh` re-fetcha Beefy + dYdX em <30s; retorna 200 com timestamp novo
5. Próximo `start_operation` após select usa o novo pair sem reiniciar servidor
6. Cache stale graceful: bot sobe mesmo com Beefy API down; UI mostra warning
7. Backwards compat: tests Phase 2.0 (engine/lifecycle/integration) passam
8. Tests novos: ≥4 unit (cada modulo novo) + ≥3 integration
9. UI mobile-friendly (cards stack em telas <768px)

## Arquivos novos

- `chains/beefy_api.py`
- `chains/dydx_markets.py`
- `engine/pair_resolver.py`
- `engine/pair_factory.py`
- `config/stables.py`
- `web/templates/partials/pair_picker.html`
- `tests/test_beefy_api.py`
- `tests/test_dydx_markets.py`
- `tests/test_pair_resolver.py`
- `tests/test_pair_factory.py`
- `tests/test_pair_picker_integration.py`

## Arquivos modificados

- `config.py` — rename token addresses, add decimals
- `db.py` — schema migration + helpers
- `engine/__init__.py` — start_operation usa pair_factory
- `engine/lifecycle.py` — settings injetadas via factory
- `app.py` — lazy lifecycle
- `web/routes.py` — endpoints novos
- `web/templates/partials/settings.html` — Trading vira só Trading minimal
- `web/templates/dashboard.html` — include pair_picker.html
- `web/static/app.js` — pair handlers
- `web/static/app.css` — styling Beefy-like
- `.env.example` — vars novas
- `CLAUDE.md` — note phase pair-picker

## Convenções (mantidas)

- TDD: failing test → impl → green → commit
- Commit format: `feat(task-N):`, `fix(task-N):`, etc
- Branch: `feature/pair-picker`
- Tag: `fase-pair-picker-completa`
