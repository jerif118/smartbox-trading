# Código legacy (v1)

Módulos de la versión original del bot, fuera de `src/` para que el código
nuevo no pueda importarlos por accidente. El pipeline v2 ya no depende de
nada de aquí (la única función usada, `box_window_unix`, vive ahora en
`src/domain/market_time.py`).

Borrar esta carpeta tras 1-2 semanas de runs estables del pipeline v2.
