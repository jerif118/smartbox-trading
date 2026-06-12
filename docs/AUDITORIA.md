# Auditoría técnica — SmartBox Trading v2

**Fecha:** 2026-06-12
**Alcance:** código fuente completo (`src/`), tests, configuración, empaquetado y manejo de secretos.
**Estado al momento de auditar:** 141 tests en verde; bot operando en modo DRY_RUN + DEMO.

---

## Resumen ejecutivo

El proyecto tiene una base sólida: arquitectura en capas bien separada (domain puro, application, infrastructure, pipeline), contratos Pydantic entre stages, persistencia SQLite con WAL, retry con backoff en adapters y 22 tests de regresión que protegen las reglas de la estrategia. Sin embargo, la auditoría encontró **4 hallazgos críticos** centrados en la ruta de ejecución de órdenes (falta de idempotencia y de atomicidad) y en el manejo de secretos, además de hallazgos medios en robustez del orquestador, tipado de contratos y código legacy.

| Severidad | Cantidad |
|---|---|
| Crítica | 4 |
| Alta/Media | 12 |
| Menor | 8 |

---

## Hallazgos críticos

### C1 — Órdenes duplicables por falta de idempotencia (W8)
**Archivo:** `src/pipeline/stages/s6_execute.py`
Si `broker.place_order()` tiene éxito pero el update posterior de la DB falla, el trade queda en `PENDING` sin `broker_order_id`. Un re-run del mismo día vuelve a enviar la orden: **orden real duplicada con dinero real**. No existe ninguna clave de deduplicación.
**Remediación:** `client_order_id` determinista por (fecha, símbolo, lado, tipo) + índice único parcial en SQLite + verificación antes de enviar.

### C2 — Escrituras en DB antes de validar y antes del broker (W9/W10)
**Archivo:** `src/pipeline/stages/s6_execute.py`
`insert_decision` se ejecuta antes de las validaciones (box, plan, R:R, budget) y `insert_trade` antes de `place_order`. Si algo falla a mitad, quedan decisiones fantasma y trades `PENDING` que el broker nunca recibió.
**Remediación:** reordenar el stage: validar todo → persistir decisión → deduplicar → insertar trade → enviar orden → actualizar estado, con reconciliación de `PENDING` huérfanos al inicio de cada run.

### C3 — Runs zombi en estado "running" (W4)
**Archivo:** `src/pipeline/orchestrator.py`
Si el proceso crashea (o recibe SIGKILL) a mitad de run, el registro en `runs` queda en `running` para siempre. No hay reparación al arrancar ni `finally` que cierre el run.
**Remediación:** `fail_stale_runs()` al inicio + bloque `finally` que marca el run como `failed/aborted` si sigue abierto.

### C4 — Manejo de secretos
- `.env` local contiene credenciales reales de Capital.com, SimpleFX y OpenAI. **Nunca estuvieron en el historial de git** (verificado con `git log --all --full-history -- .env`), pero por higiene **se recomienda rotarlas**: la contraseña de Capital.com, la API key de Capital.com, el client secret de SimpleFX y la API key de OpenAI.
- `src/tools_bot/test.py` era un script de debug que imprimía tokens de sesión a stdout (eliminado en esta auditoría).
- `src/infrastructure/broker/simplefx/adapter.py` loguea `resp.text` crudo en errores HTTP — puede contener información sensible del servidor.
- `settings.py` usa `"dummy"` como API key por defecto para proveedores OpenAI-compatible: si se configura `base_url` sin key, se envía "dummy" a un servidor potencialmente hostil.

---

## Hallazgos altos y medios

### Orquestador y pipeline
1. **`except Exception` genéricos** en `orchestrator.py` (líneas 224, 237, 327, 346), `s5_analyze.py:159` y `s7_manage.py:101` — ocultan el tipo de error y dificultan diagnóstico. En `s5_analyze` los errores de parseo del JSON del crew se silencian por completo.
2. **Sin timeout por stage** — si el crew de agentes (stage 5) se cuelga, el run entero se bloquea indefinidamente. El cron del día siguiente encontraría el proceso vivo.
3. **`DailyOrderBudget` no sobrevive re-runs** (`orchestrator.py:299`) — el cap de `MAX_ORDERS_PER_DAY` se cuenta solo en memoria del run actual; un segundo run el mismo día arranca con presupuesto completo. Además se instancia después del crew (W6).
4. **Loop de ejecución sin aislamiento** (`orchestrator.py:300-316`) — un error en una decisión aborta las siguientes.
5. **`equity_snapshots` con placeholders** (`orchestrator.py:321-322`) — se persiste `balance=0.0, equity=0.0`, lo que hace inútil la gráfica de equity del panel.
6. **Contratos con tipos débiles** (`src/pipeline/contracts/__init__.py`) — `df_candles: Any`, `AnalyzeInput.symbols: list[dict]`, `ManageInput.open_trades: list[dict]`, etc. Errores de estructura solo aparecen en runtime.
7. **Sin métricas por stage** — no se persiste duración ni resultado por stage; imposible saber qué parte del pipeline es lenta o falla más.

### Infraestructura y utilidades
8. **Retry sin cap de delay** (`src/utils/retry.py:31`) — el backoff exponencial crece sin límite.
9. **Scrapers silenciosos** (`src/infrastructure/data_sources/scrapers.py:40,115,159`) — `except Exception` que devuelve lista vacía sin loguear la causa; un fallo del calendario macro pasa desapercibido (y la regla 20 de la estrategia depende de él).
10. **Sin migraciones de schema** — `schema.sql` menciona `migrate()` pero `db.py` no lo implementa; cualquier cambio de schema requiere borrar la DB.

### Código legacy
11. **~1.761 líneas legacy empaquetadas** — `src/broker_api`, `src/preprocess`, `src/strategy_ai`, `src/tools_bot` van dentro del wheel (`pyproject.toml`) aunque el código nuevo solo usa una función: `tools_bot.time_now.box_window_unix` (importada desde `orchestrator.py` y `s2_preprocess.py`).
12. **Sin tests** de orchestrator end-to-end, CLI, scrapers ni Streamlit. Cobertura estimada ~40% (domain muy bien cubierto; interfaces y legacy sin cobertura).

---

## Hallazgos menores

- `.env.exmple` con typo (renombrado a `.env.example` en esta auditoría).
- `.gitignore` cubría `*.db` pero no `*.db-wal`/`*.db-shm` (corregido).
- `pyproject.toml` con `authors = "Your Name"` placeholder.
- Health checks de Ollama/LM Studio silenciosos (`provider.py:92-105`).
- Timeouts HTTP hardcodeados (15s/10s/20s) en lugar de configurables.
- Warning de deprecación de `pandas_ta` con pandas ≥3.0 (romperá con pandas 4.0).
- Type hints incompletos en `application/agents/`.
- Sin tests de propagación de NaN/inf en `volume_profile.py`.

---

## Lo que está bien hecho (preservar)

- **Capa de dominio pura** sin I/O, con dataclasses frozen y type hints completos.
- **22 tests de regresión** que codifican las reglas originales de la estrategia.
- **Queries parametrizadas** en todos los repos SQLite — sin inyección SQL.
- **Context manager de DB** con commit/rollback correcto.
- **Retry decorator** con backoff en los adapters de broker.
- **Multi-provider LLM** vía wrapper flexible.
- **Logging rotativo** bien configurado; los adapters no loguean credenciales en el flujo normal.
- **Settings tipados con Pydantic** y validators de formato.

---

## Plan de remediación

Las remediaciones de C1–C4 y de los hallazgos altos/medios se implementan en este mismo ciclo de trabajo (ver historial de commits que sigue a esta auditoría): migraciones de schema, idempotencia de órdenes, orquestador con timeouts y métricas por stage, contratos tipados, migración del código legacy y endurecimiento de secretos.

**Acciones que quedan en manos del usuario:**
1. Rotar credenciales: contraseña + API key de Capital.com, client secret de SimpleFX, API key de OpenAI.
2. Tras 1–2 semanas de runs estables, borrar definitivamente la carpeta `legacy/`.
3. Activar el cron diario si se quiere acumular historial de validación en demo.
