#!/bin/bash
# SmartBox Trading v2 — Setup automático
# Reconstruye el venv limpio y registra el paquete.
# Funciona con venv puro o con conda (evita el conflicto site.py).

set -e
cd "$(dirname "$0")"

echo "═══════════════════════════════════════════════════════"
echo "  SmartBox Trading v2 — Setup"
echo "═══════════════════════════════════════════════════════"
echo ""

# 1. Detectar Python 3.12
if ! command -v python3.12 &> /dev/null; then
    if command -v python3 &> /dev/null; then
        PYTHON=$(command -v python3)
        echo "⚠  python3.12 no encontrado, usando $PYTHON"
    else
        echo "✗ Python 3 no encontrado. Instala Python 3.12 primero."
        exit 1
    fi
else
    PYTHON=$(command -v python3.12)
fi

echo "• Python: $($PYTHON --version)"
echo ""

# 2. Recrear venv limpio
if [ -d ".venv" ]; then
    echo "• Eliminando .venv existente (puede estar roto)..."
    rm -rf .venv
fi

echo "• Creando .venv nuevo con $PYTHON..."
$PYTHON -m venv .venv
echo "  ✓ .venv creado"
echo ""

# 3. Actualizar pip
echo "• Actualizando pip..."
.venv/bin/python -m pip install --upgrade pip wheel setuptools 2>&1 | tail -2
echo ""

# 4. Instalar paquete en editable mode
echo "• Instalando dependencias + paquete editable..."
.venv/bin/python -m pip install -e ".[dev]" 2>&1 | tail -5
echo ""

# 5. Verificar
echo "• Verificando instalación..."
if .venv/bin/python -c "import sys; sys.path.insert(0, 'src'); import interfaces, application, infrastructure, pipeline" 2>/dev/null; then
    echo "  ✓ Imports OK (vía PYTHONPATH=src)"
else
    echo "  ⚠  Algunos imports fallaron (no crítico si ./run.sh funciona)"
fi

# 6. Hacer scripts ejecutables
chmod +x run.sh start_ui.sh
echo "  ✓ Scripts ejecutables (run.sh, start_ui.sh)"
echo ""

# 7. Inicializar DB
echo "• Inicializando base de datos SQLite..."
mkdir -p data logs
.venv/bin/python -c "from infrastructure.persistence.sqlite import db; db.init_db(); print('  ✓ DB inicializada en', db.get_settings().db_path)" 2>&1
echo ""

echo "═══════════════════════════════════════════════════════"
echo "  ✓ Setup completo"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "Próximos pasos:"
echo ""
echo "  1. Configura tu API key en .env (si aún no lo hiciste):"
echo "     $EDITOR .env"
echo ""
echo "  2. Verifica que todo esté OK:"
echo "     ./run.sh doctor"
echo ""
echo "  3. Corre el bot (dry-run, no envía órdenes reales):"
echo "     ./run.sh run --dry-run"
echo ""
echo "  4. Abre el dashboard:"
echo "     ./start_ui.sh"
echo "     → http://localhost:8501"
echo ""
