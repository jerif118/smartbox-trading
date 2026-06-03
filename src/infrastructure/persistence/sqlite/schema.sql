-- SmartBox Trading v2 — Schema SQLite
-- Versionado vía PRAGMA user_version. Migraciones aplicadas en db.py:migrate()

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ── Runs: una fila por corrida del bot ─────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,                    -- running | success | failed | skipped
  error TEXT,
  config_snapshot TEXT,                    -- JSON de las settings relevantes
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

-- ── Decisions: una fila por decisión del crew ─────────────────────
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,                    -- LONG | SHORT | NO_OPERAR
  risk TEXT,                               -- COMPLETO | MEDIO
  confidence INTEGER,
  reasons TEXT,                            -- JSON array
  team_consensus TEXT,
  crew_raw_output TEXT,                    -- JSON completo
  key_levels TEXT,                         -- JSON
  signal TEXT,                             -- JSON
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_decisions_run_id ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

-- ── Trades: una fila por orden enviada (primary + runner = 2 filas) ─
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  decision_id INTEGER,
  ts_open TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,                      -- BUY | SELL
  volume REAL NOT NULL,
  entry_price REAL NOT NULL,
  stop_loss REAL,
  take_profit REAL,
  is_runner INTEGER NOT NULL DEFAULT 0,    -- 0 = primary, 1 = runner
  broker_order_id TEXT,
  status TEXT NOT NULL,                    -- PENDING | OPEN | CLOSED_TP | CLOSED_SL | CLOSED_MANUAL | EXPIRED | REJECTED
  ts_close TEXT,
  exit_price REAL,
  pnl REAL,
  r_multiple REAL,                         -- (exit-entry)/(entry-SL) firmado
  reason TEXT,                             -- razón al cerrar
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES runs(id),
  FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_trades_run_id ON trades(run_id);
CREATE INDEX IF NOT EXISTS idx_trades_decision_id ON trades(decision_id);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ts_open ON trades(ts_open);

-- ── Agent events: una fila por interacción de un agente ───────────
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  agent TEXT NOT NULL,                     -- decision_maker | trader | risk_analyst | mtfa | position_manager
  event_type TEXT NOT NULL,                -- THOUGHT | TOOL_CALL | TOOL_RESULT | MESSAGE | DECISION | SYSTEM
  payload TEXT,                            -- JSON
  duration_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_events_run_id ON agent_events(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent ON agent_events(agent);
CREATE INDEX IF NOT EXISTS idx_agent_events_type ON agent_events(event_type);
CREATE INDEX IF NOT EXISTS idx_agent_events_ts ON agent_events(ts);

-- ── Equity snapshots: P&L acumulado en el tiempo ──────────────────
CREATE TABLE IF NOT EXISTS equity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  balance REAL NOT NULL,
  equity REAL NOT NULL,
  daily_pnl REAL,
  total_pnl REAL,
  open_positions INTEGER,
  source TEXT,                             -- broker | computed
  run_id TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);
