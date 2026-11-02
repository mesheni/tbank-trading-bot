"""Read-only TUI-дашборд демона: данные только из файлов ``reports/``, без сети и записи."""
from pathlib import Path

# Один источник истины для служебного каталога (лог/вывод демона): tui.app и
# tui.controls берут его отсюда, а не собирают ``ROOT / "var"`` каждый сам.
_VAR_DIR = Path(__file__).resolve().parents[1] / "var"
