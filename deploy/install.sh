#!/usr/bin/env bash
# Установка бота на Linux-сервере (Ubuntu/Debian; root или отдельный пользователь).
# Идемпотентен: повторный запуск обновляет зависимости и юнит, не трогая
# .env / data / reports / models_artifacts.
# Использование: deploy/install.sh [каталог-проекта]   (по умолчанию — каталог выше скрипта)
# Имя systemd-юнита можно переопределить: SYSTEMD_UNIT=my-bot deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${1:-$APP_DIR}"
UNIT_NAME="${SYSTEMD_UNIT:-tbank-bot}"
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}.service"

echo "== Установка tbank-trading-bot в ${APP_DIR} (юнит: ${UNIT_NAME})"
cd "${APP_DIR}"

# 1. Системные зависимости (OpenMP нужен LightGBM; не трогаем на не-Debian/не-root)
if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y --no-install-recommends python3-venv python3-pip libgomp1
else
    echo "[i] пропуск apt-зависимостей (не root или не Debian). При ошибке libgomp.so.1:"
    echo "    sudo apt install -y libgomp1 python3-venv"
fi

# 2. Виртуальное окружение (пересоздавать не нужно: если существует — переиспользуем)
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
chmod +x tbank.sh
mkdir -p var

# 3. Конфигурация: .env обязателен, токен вносится вручную
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "[!] .env создан из шаблона — впишите T_INVEST_TOKEN (nano ${APP_DIR}/.env) и запустите install.sh снова"
    exit 1
fi
chmod 600 .env

# 4. systemd-юнит
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
    sed "s|__APP_DIR__|${APP_DIR}|g" deploy/tbank-bot.service > "${UNIT_FILE}"
    systemctl daemon-reload
    systemctl enable "${UNIT_NAME}"
    echo "== Юнит установлен: systemctl status ${UNIT_NAME} --no-pager"
else
    echo "[i] systemctl недоступен — запускайте бота вручную: ./tbank.sh run (или через tmux)"
fi

echo
echo "== Следующие шаги:"
echo "  1) токен в .env (если ещё нет):            nano ${APP_DIR}/.env"
echo "  2) первый запуск (данные+обучение+тест):   ${APP_DIR}/tbank.sh setup"
echo "  3) проверка почтовых уведомлений:          ${APP_DIR}/tbank.sh send-test-email"
echo "  4) запуск сервиса:                         systemctl start ${UNIT_NAME} && journalctl -u ${UNIT_NAME} -f"
echo "  5) cron (переобучение/watchdog/дайджест):  crontab -e и вставьте deploy/tbank-bot.cron"
