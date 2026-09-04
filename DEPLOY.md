# Запуск и деплой торгового бота

Инструкция охватывает два сценария: **локальный запуск на Windows** и **работу на удалённом
Linux-сервере (VPS)** 24/7. Полное описание архитектуры и параметров — в [README.md](README.md).

---

## 1. Что понадобится

| Что | Зачем | Где взять |
|---|---|---|
| Токен T-Invest API | доступ к sandbox/счёту | Приложение Т-Инвестиции → профиль → Настройки → токен T-Invest API |
| Python 3.11–3.14 | рантайм | python.org / `apt install python3-venv` |
| ~500 МБ диска | venv + SQLite + артефакты | — |
| Сертификаты НУЦ Минцифры | только при доступе из РФ-сети | [gu-st.ru](https://gu-st.ru/content/lending/russian_trusted_root_ca.zip) |

Серверу хватит 1 vCPU / 1 ГБ RAM. Весь трафик — исходящий HTTPS к `invest-public-api.tbank.ru`.

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

## 3. Запуск на удалённом сервере (Linux VPS)

### 3.1 Подготовка

```bash
# Ubuntu/Debian
sudo apt update && sudo apt install -y python3-venv python3-pip git

# выделенный пользователь (не root)
sudo useradd -m -s /bin/bash botuser
sudo -iu botuser
```

### 3.2 Копирование проекта

Вариант А — git (рекомендуется): запушьте репозиторий **без** `.env` и сертификатов
(см. `.gitignore`), на сервере:

```bash
git clone <ваш-репозиторий> ~/tbank-trading-bot && cd ~/tbank-trading-bot
```

Вариант Б — прямое копирование с машины разработки:

```bash
# локально (Git Bash / scp), исключая тяжёлое и секреты
scp -r tbank-trading-bot botuser@SERVER_IP:~/
```

### 3.3 Установка и конфигурация

```bash
cd ~/tbank-trading-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

chmod 600 .env        # если уже скопирован; иначе создайте из шаблона:
cp .env.example .env  # и впишите T_INVEST_TOKEN (nano .env)
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
.venv/bin/python cli.py train
.venv/bin/python cli.py backtest
```

> Часовой пояс сервера не важен: расписание MOEX считается в MSK (UTC+3) внутри программы.

### 3.5 Постоянная работа через systemd

Создайте юнит `sudo nano /etc/systemd/system/tbank-bot.service`:

```ini
[Unit]
Description=T-Bank sandbox trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=botuser
WorkingDirectory=/home/botuser/tbank-trading-bot
ExecStart=/home/botuser/tbank-trading-bot/.venv/bin/python cli.py run
Restart=always
RestartSec=30
# лог в journald; для файла вместо этого:
# StandardOutput=append:/home/botuser/tbank-trading-bot/var/bot.log
# StandardError=append:/home/botuser/tbank-trading-bot/var/bot.log

[Install]
WantedBy=multi-user.target
```

```bash
mkdir -p ~/tbank-trading-bot/var
sudo systemctl daemon-reload
sudo systemctl enable --now tbank-bot
sudo systemctl status tbank-bot        # состояние
journalctl -u tbank-bot -f             # живой лог (Ctrl+C — выйти)
```

`Restart=always` переживёт перезагрузку сервера и сетевые сбои: вне торговой сессии бот сам спит,
ошибки итерации логируются и не останавливают цикл. Остановка: `sudo systemctl stop tbank-bot`.

**Быстрая альтернатива без systemd** — tmux:

```bash
sudo apt install -y tmux
tmux new -s bot '.venv/bin/python cli.py run 2>&1 | tee -a var/bot.log'
# Ctrl+B, затем D — отключиться; tmux attach -t bot — вернуться
```

### 3.6 Плановое переобучение (cron)

Раз в неделю ночью: обновить историю → переобучить → перезапустить бота (он кэширует артефакты
в памяти до рестарта).

```bash
crontab -e
# суббота 06:00 MSK (время сервера переводите сами; тут сервер в MSK):
0 6 * * 6 cd /home/botuser/tbank-trading-bot && .venv/bin/python cli.py download --days 720 >> var/retrain.log 2>&1 && .venv/bin/python cli.py train >> var/retrain.log 2>&1 && sudo systemctl restart tbank-bot >> var/retrain.log 2>&1
```

Чтобы `sudo systemctl` работал из cron без пароля: `sudo visudo -f /etc/sudoers.d/tbank-bot` →

```
botuser ALL=(root) NOPASSWD: /usr/bin/systemctl restart tbank-bot
```

### 3.7 Мониторинг

| Что | Команда / файл |
|---|---|
| Капитал, P&L, позиции, сделки | `.venv/bin/python cli.py report` (или локально на сервере) |
| Кривая капитала | `reports/equity_live.csv` (растёт после каждой итерации) |
| Журнал сделок с причинами | `reports/journal.csv` |
| Живой лог | `journalctl -u tbank-bot -f` |
| Состояние сервиса | `systemctl status tbank-bot` |

Удобно забирать `reports/*.csv` на локальную машину по scp и смотреть в Excel.

---

## 4. Безопасность и важные оговорки

* `.env` с токеном никогда не попадает в git (`.gitignore`) — на сервере права `600`.
* Токен давайте только с нужными правами; для песочницы — sandbox-токен, для реального счёта —
  отдельный токен с правом торговли.
* `MODE=real` переключает бота на **реальный счёт**. Не включайте, пока стратегия не показала
  устойчивый положительный результат за месяцы sandbox-работы — и помните, что прошлые результаты
  не гарантируют будущих.
* Сертификаты в папке проекта (`certs/`) не секретны; секрет — только токен.
* Бэкапьте `data/market.sqlite` и `reports/` (история выгрузок и журналы) — например, в cron
  `tar czf backup_$(date +\%F).tgz data reports`.

## 5. Краткий чек-лист деплоя

1. VPS (лучше в РФ — ниже пинг к API; НУЦ-сертификаты в `certs/`).
2. Python 3.11+, venv, `pip install -r requirements.txt`.
3. `.env` с токеном, `chmod 600`.
4. `cli.py smoke` → `download` → `news` → `train` → `backtest`.
5. systemd-юнит `tbank-bot` → `enable --now` → `journalctl -f`.
6. Cron: еженедельные `download` + `train` + `systemctl restart tbank-bot`.
7. Раз в день: `cli.py report` (или смотреть `journal.csv` / `equity_live.csv`).
