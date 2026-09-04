# Запуск и деплой торгового бота

Инструкция охватывает два сценария: **локальный запуск на Windows** и **работу на удалённом
Linux-сервере (VPS)** 24/7. Основной путь на сервере — работа под root (один пользователь,
никаких игр с правами); изоляция под отдельным пользователем — опция в §3.9.
Полное описание архитектуры и параметров — в [README.md](README.md).

---

## 1. Что понадобится

| Что | Зачем | Где взять |
|---|---|---|
| Токен T-Invest API | доступ к sandbox/счёту | Приложение Т-Инвестиции → профиль → Настройки → токен T-Invest API |
| Python 3.11–3.14 | рантайм | python.org / `apt install python3-venv` |
| ~500 МБ диска | venv + SQLite + артефакты | — |
| Сертификаты НУЦ Минцифры | только при доступе из РФ-сети | [gu-st.ru](https://gu-st.ru/content/lending/russian_trusted_root_ca.zip) |

Серверу хватит 1 vCPU / 2 ГБ RAM (2 ГБ — если включать нейросетевой NLP, см. §3.7).
Весь трафик — исходящий HTTPS к `invest-public-api.tbank.ru`.

---

## 2. Локальный запуск (Windows)

```bat
cd tbank-trading-bot
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
:: впишите T_INVEST_TOKEN в .env
```

Положите сертификаты НУЦ (`Russian_Trusted_Root_CA.cer`, `Russian_Trusted_Sub_CA.cer`,
`Russian_Trusted_Sub_CA_2024.cer`) в корень проекта или в папку `certs/` — клиент подхватит их сам.
Для зарубежной сети они не нужны.

Последовательность первого запуска:

```bat
.venv\Scripts\python cli.py smoke              :: проверка токена, счёта, свечей, новостей
.venv\Scripts\python cli.py download --days 720 :: история свечей → data/market.sqlite (~5 мин на 16 тикеров)
.venv\Scripts\python cli.py news                :: новостная лента → БД
.venv\Scripts\python cli.py train               :: сравнение моделей, артефакты (~30 мин на 16 тикеров)
.venv\Scripts\python cli.py backtest            :: честный walk-forward бэктест → reports/
.venv\Scripts\python cli.py run                 :: торговый цикл в sandbox (Ctrl+C — остановить)
.venv\Scripts\python cli.py report              :: капитал, позиции, сделки, кривая капитала
```

Полезные флаги: `run --iterations 5` (пробный прогон), `download --days 720`.
Плановое переобучение: `download` → `train` раз в 1–2 недели (модели видят только прошлые данные).

---

## 3. Запуск на удалённом сервере (Linux VPS) — под root

> **Важно про venv:** виртуальное окружение хранит абсолютные пути. Если проект переезжает в
> другой каталог — venv пересоздаётся (`rm -rf .venv && python3 -m venv .venv` + установка
> зависимостей). Скопированный из другого места venv не работает (`bad interpreter`).

### 3.1 Подготовка

```bash
# Ubuntu/Debian — сразу ставим OpenMP-рантайм для LightGBM (на минимальных образах его нет)
sudo apt update && sudo apt install -y python3-venv python3-pip git libgomp1
# CentOS/RHEL/AlmaLinux: sudo yum install -y libgomp
```

### 3.2 Копирование проекта

Вариант А — git (рекомендуется): запушьте репозиторий **без** `.env` и сертификатов
(см. `.gitignore`), на сервере:

```bash
git clone <ваш-репозиторий> /root/tbank-trading-bot && cd /root/tbank-trading-bot
```

Вариант Б — прямое копирование с машины разработки:

```bash
# локально (Git Bash / scp), исключая тяжёлое и секреты
scp -r tbank-trading-bot root@SERVER_IP:/root/
```

Дальше все команды выполняются в `/root/tbank-trading-bot` под root.

### 3.3 Установка и конфигурация

```bash
cd /root/tbank-trading-bot
rm -rf .venv                      # если venv приехал с другой машины/пути — пересоздать
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

chmod 600 .env                    # если уже скопирован; иначе создайте из шаблона:
cp .env.example .env              # и впишите T_INVEST_TOKEN (nano .env)
```

Сертификаты (если сервер в РФ и TLS перехватывается провайдером):

```bash
mkdir -p certs
# скопируйте .cer/.pem файлы НУЦ в certs/ — бот соберёт из них бандл автоматически.
# Альтернатива на уровне системы:
sudo cp Russian_Trusted_Root_CA.cer /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
sudo update-ca-certificates
```

Проверка связи:

```bash
.venv/bin/python cli.py smoke
```

### 3.4 Данные и обучение

```bash
.venv/bin/python cli.py download --days 720
.venv/bin/python cli.py news
.venv/bin/python cli.py train     # ~30 мин на 16 тикеров
.venv/bin/python cli.py backtest
```

> Часовой пояс сервера не важен: расписание MOEX считается в MSK (UTC+3) внутри программы.

### 3.5 Постоянная работа через systemd

Создайте юнит `nano /etc/systemd/system/tbank-bot.service`:

```ini
[Unit]
Description=T-Bank sandbox trading bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/root/tbank-trading-bot
ExecStart=/root/tbank-trading-bot/.venv/bin/python cli.py run
Restart=always
RestartSec=30
# лог в journald; для файла вместо этого:
# StandardOutput=append:/root/tbank-trading-bot/var/bot.log
# StandardError=append:/root/tbank-trading-bot/var/bot.log

[Install]
WantedBy=multi-user.target
```

Строка `User=` не нужна — сервис работает от root (владельца файлов).

```bash
mkdir -p /root/tbank-trading-bot/var
systemctl daemon-reload
systemctl enable --now tbank-bot
systemctl status tbank-bot --no-pager   # состояние
journalctl -u tbank-bot -f              # живой лог (q — выйти)
```

`Restart=always` переживёт перезагрузку сервера и сетевые сбои: вне торговой сессии бот сам спит,
ошибки итерации логируются и не останавливают цикл. Остановка: `systemctl stop tbank-bot`.

**Быстрая альтернатива без systemd** — tmux:

```bash
apt install -y tmux
tmux new -s bot '.venv/bin/python cli.py run 2>&1 | tee -a var/bot.log'
# Ctrl+B, затем D — отключиться; tmux attach -t bot — вернуться
```

### 3.6 Плановое переобучение (cron)

Раз в неделю ночью: обновить историю → переобучить → перезапустить бота (он кэширует артефакты
в памяти до рестарта). Cron создаётся в расписании **root** (`crontab -e`), без sudo:

```
0 6 * * 6 cd /root/tbank-trading-bot && .venv/bin/python cli.py download --days 720 >> var/retrain.log 2>&1 && .venv/bin/python cli.py train >> var/retrain.log 2>&1 && systemctl restart tbank-bot >> var/retrain.log 2>&1
```

(суббота 06:00 — если сервер живёт в MSK; иначе переведите на ночь по Москве).

### 3.7 Опционально: нейросетевой NLP (transformers + torch)

По умолчанию тональность считается офлайн-лексиконом, кластеризация повестки отключена.
Для включения трансформеров ставьте их в venv проекта (НЕ в систему — Ubuntu защищает
системный Python, ошибка `externally-managed-environment`):

```bash
cd /root/tbank-trading-bot

# CPU-сборка torch (~200 МБ); без --index-url Linux-пакет потянет NVIDIA CUDA на ~2-3 ГБ
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install transformers sentence-transformers
.venv/bin/python -c "import torch, transformers; print('OK')"
systemctl restart tbank-bot
```

Требования и ограничения:

* Модели скачиваются с huggingface.co при первом старте (~0.5–1 ГБ в `/root/.cache/huggingface`).
* **Память — главный ограничитель.** Базовая ruBERT-тональность ~700 МБ + эмбеддер ~450 МБ.
  Если RAM меньше суммарной потребности, ядро убьёт процесс при старте (`oom-kill` в
  `journalctl`), а systemd зациклит перезапуски. Первым делом: `systemctl stop tbank-bot`.

Профили под объём RAM (переключатели — в `.env`):

| RAM | `.env` | Что получится |
|---|---|---|
| ≤ 1 ГБ | `NLP_SENTIMENT=lexicon` + `NLP_EMBEDDER=0` | офлайн-режим: лексиконная тональность, без кластеризации (~0 доп. МБ) |
| 1–2 ГБ | `SENTIMENT_MODEL=cointegrated/rubert-tiny-sentiment-balanced` + `NLP_EMBEDDER=0` | трансформер-тональность (~30 МБ модель), эмбеддер выключен |
| 2+ ГБ | ничего не менять | полная схема: ruBERT-тональность + e5-эмбеддер |

После изменения `.env` — `systemctl restart tbank-bot`. Если OOM повторяется даже в лёгком
профиле — добавьте swap (сглаживает пики, ценой скорости):

```bash
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Совсем убрать нейронку из окружения: `.venv/bin/pip uninstall -y torch transformers sentence-transformers` —
код сам вернётся к лексикону.

### 3.8 Мониторинг

| Что | Команда / файл |
|---|---|
| Капитал, P&L, позиции, сделки | `/root/tbank-trading-bot/.venv/bin/python cli.py report` |
| Кривая капитала | `reports/equity_live.csv` (растёт после каждой итерации) |
| Журнал сделок с причинами | `reports/journal.csv` |
| Живой лог | `journalctl -u tbank-bot -f` |
| Состояние сервиса | `systemctl status tbank-bot --no-pager` |

Удобно забирать `reports/*.csv` на локальную машину по scp и смотреть в Excel.

### 3.9 Опционально: изоляция под отдельным пользователем (не root)

Если захотите отделить бота от root-инфраструктуры:

```bash
useradd -m -s /bin/bash botuser
mv /root/tbank-trading-bot /home/botuser/
chown -R botuser:botuser /home/botuser/tbank-trading-bot
su - botuser -c 'cd ~/tbank-trading-bot && rm -rf .venv && python3 -m venv .venv \
  && .venv/bin/pip install -r requirements.txt'
```

В systemd-юните добавить `User=botuser` и поправить пути на `/home/botuser/...`.
Cron — в расписании botuser (`su - botuser -c 'crontab -e'`); рестарт сервиса из cron — по
правилу sudoers:

```
echo "botuser ALL=(root) NOPASSWD: /usr/bin/systemctl restart tbank-bot" > /etc/sudoers.d/tbank-bot
chmod 440 /etc/sudoers.d/tbank-bot
```

Дисциплина: обслуживающие команды (`train`, `download`) — только от botuser, иначе файлы
получат владельца root и сервис упадёт на записи (лечение — `chown -R botuser:botuser`).
Токен в `.env` остаётся секретом независимо от пользователя.

---

### 3.10 Обновление кода на сервере

Если серверная копия склонирована через git:

```bash
cd /root/tbank-trading-bot
git pull
# если менялся requirements.txt:
.venv/bin/pip install -r requirements.txt
systemctl restart tbank-bot
```

Если копировали по scp (нет .git) — переключитесь на репозиторий один раз:

```bash
cd /root/tbank-trading-bot
git init -b main
git remote add origin https://github.com/ВАШ-АККАУНТ/tbank-trading-bot.git
git fetch origin
git reset --hard origin/main
git branch --set-upstream-to=origin/main main
systemctl restart tbank-bot
```

`reset --hard` меняет только файлы из репозитория: `.env`, `data/`, `reports/`,
`models_artifacts/`, `certs/` (в `.gitignore`) не затрагиваются. Ручные правки кода на сервере
при этом теряются — править стоит локально и пушить. Цикл: локально `commit` + `push` →
на сервере `git pull` → `systemctl restart tbank-bot`. Отставание видно в `git status -sb`
(`[behind N]`).

## 4. Типичные проблемы на сервере

| Симптом | Причина и решение |
|---|---|
| `oom-kill` в journalctl, сервис циклически перезапускается | Не хватает RAM под трансформеры: `systemctl stop tbank-bot`, затем профиль по RAM из §3.7 (`NLP_SENTIMENT=lexicon`, `NLP_EMBEDDER=0`, облегчённая модель) |
| `OSError: libgomp.so.1: cannot open shared object file` при train | Нет OpenMP-рантайма: `apt install -y libgomp1` (или `yum install libgomp`) |
| `.venv/bin/pip: bad interpreter: ... permission denied` / venv «не тот» | Venv скопирован из другого каталога — он хранит абсолютные пути. Пересоздать: `rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| `PermissionError` у сервиса на файлы проекта | Файлы принадлежат не тому пользователю (созданы под другим юзером). Вернуть владельца: `chown -R root:root /root/tbank-trading-bot` (или пользователю из `User=` юнита) |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Сервер в РФ с перехватом TLS: положите НУЦ-сертификаты в `certs/` проекта (см. §3.3) |
| `T_INVEST_TOKEN не задан` | Не заполнен `.env` в рабочей директории юнита/cron (`WorkingDirectory`!) |
| Ошибки вида `database is locked` | Одновременная запись в SQLite: не запускайте `download` параллельно с работающим ботом — по расписанию cron сначала `systemctl stop`, после `train` — `start` |
| Обучение падает на новом листинге | Тикер с историей < 500 свечей пропускается с предупреждением — это норма |
| `externally-managed-environment` при pip | Ставили в системный Python. Используйте venv: `.venv/bin/pip install ...` (CPU-torch: `--index-url https://download.pytorch.org/whl/cpu`) |
| `systemctl status` «завис» и не реагирует | Это открылся пейджер: `q` — выйти. Если нажали Ctrl+Z и увидели `[1]+ Stopped` — верните `fg` и закройте `q`, либо `kill %1` |

## 5. Безопасность и важные оговорки

* **Главный секрет — токен**: `.env` с правами `600`, никогда не в git (`.gitignore`), не
  пересылать вместе с проектом. При компрометации — перевыпустить токен в приложении.
* В режиме root весь бот работает с полными правами машины: не запускайте посторонний код в его
  окружении и не открывайте лишних портов (боту исходящий HTTPS, входящие подключения не нужны).
* `MODE=real` переключает бота на **реальный счёт**. Не включайте, пока стратегия не показала
  устойчивый положительный результат за месяцы sandbox-работы — и помните, что прошлые результаты
  не гарантируют будущих.
* Бэкапьте `data/market.sqlite` и `reports/` (история выгрузок и журналы) — например, в cron:
  `tar czf /root/backup_$(date +\%F).tgz -C /root/tbank-trading-bot data reports`.

## 6. Краткий чек-лист деплоя (root)

1. VPS (лучше в РФ — ниже пинг к API; НУЦ-сертификаты в `certs/`).
2. `apt install python3-venv python3-pip libgomp1`.
3. Проект в `/root/tbank-trading-bot`, venv пересоздан на месте, `.env` с токеном, `chmod 600`.
4. `cli.py smoke` → `download` → `news` → `train` → `backtest`.
5. systemd-юнит (root, `/root/...` пути, без `User=`) → `daemon-reload` → `enable --now` → `journalctl -f`.
6. Cron root: еженедельные `download` + `train` + `systemctl restart tbank-bot`.
7. Раз в день: `cli.py report` (или смотреть `journal.csv` / `equity_live.csv`).
