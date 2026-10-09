# ShuzaGram OTP Bot

Telegram-бот для выдачи виртуальных номеров и доставки OTP-кодов от [ShuzaGram](https://github.com/Shuzycky-design/ShuzaGram) — MTProto-сервера Telegram.

Работает **на том же сервере**, что и ShuzaGram. Принимает коды подтверждения через webhook (`127.0.0.1:9091`) и пересылает их пользователю в Telegram.

---

## ✨ Возможности

### Для пользователей
- 📱 Выдача случайного свободного номера в выбранной зоне (`+7`, `+1`, `+49`, `+380`, ...)
- 🔢 Лимит **2 номера** на пользователя (настраивается)
- 🔑 Получение OTP-кодов прямо в Telegram
- 📋 Просмотр своих номеров — `/my`

### Для администраторов
- ♾️ Без лимита номеров
- 🎯 `/num <номер>` — добавить **любой** номер и закрепить за собой (зона определится автоматически, включая скрытые `+888`)
- 🤝 `/take <номер>` — закрепить существующий номер
- ➕ `/add`, `/addzone`, `/gen` — массовое добавление и генерация
- 🗑 `/del`, `/free`, `/unbind`, `/delzone` — управление
- 📊 `/stats`, `/zones`, `/list`, `/users`, `/user`, `/reset`
- 🎛 Inline-меню — `/admin`

### Скрытые зоны
Зоны из `HIDDEN_ZONES` (по умолчанию `+888`) **не показываются в меню** и **недоступны через кнопки**. Только админ может забрать из них номер командой `/num`.

---

## 🚀 Быстрый старт

### 1. Клонировать и установить

```bash
git clone https://github.com/borbehr-dev/shuzagram_bot.git
cd shuzagram_bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Настроить `.env`

```bash
cp .env.example .env
nano .env
```

Заполни как минимум:
- `BOT_TOKEN` — от [@BotFather](https://t.me/BotFather)
- `ADMIN_IDS` — свои Telegram ID (узнать можно у [@userinfobot](https://t.me/userinfobot))
- `DATABASE_URL` — DSN PostgreSQL от ShuzaGram
- `OTP_WEBHOOK_SECRET` — тот же, что `TELESRV_OTP_WEBHOOK_SECRET` в ShuzaGram

### 3. Создать таблицы в БД

Бот создаст их автоматически при первом запуске. Если структура БД уже была — примени миграции:

```bash
docker exec -i gramsrv-main-postgres-1 psql -U telesrv -d telesrv_main << 'EOF'
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS username VARCHAR;
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS first_seen TIMESTAMP DEFAULT NOW();
ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'main';
ALTER TABLE bot_phone_numbers ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
ALTER TABLE bot_phone_numbers ADD COLUMN IF NOT EXISTS bound_at TIMESTAMP;
EOF
```

### 4. Запустить

```bash
python bot.py
```

В логе должно появиться:
```
INFO DB initialized
INFO Webhook: http://0.0.0.0:9091/v1/otp/deliveries
INFO Bot polling started
INFO Run polling for bot @YourBot id=... - 'YourBot'
```

---

## ⚙️ Настройка ShuzaGram

В `~/ShuzaGram/deploy/docker/.env` поменяй:

```env
# Отключаем dev-режим с фиксированным кодом 12345
TELESRV_PHONE_CODE_DELIVERY_PROVIDER=webhook
TELESRV_ALLOW_INSECURE_DEVELOPMENT_AUTH=false

# Куда слать OTP-коды — в бота
TELESRV_OTP_WEBHOOK_URL=http://127.0.0.1:9091/v1/otp/deliveries
TELESRV_OTP_WEBHOOK_TIMEOUT=5s
```

`TELESRV_OTP_WEBHOOK_SECRET` уже есть — его же надо вписать в `.env` бота.

Перезапустить:

```bash
cd ~/ShuzaGram/deploy/docker
docker compose down && docker compose up -d
```

---

## 🔧 Автозапуск через systemd

```bash
nano /etc/systemd/system/shuzagram-bot.service
```

```ini
[Unit]
Description=ShuzaGram OTP Bot
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/shuzagram_bot
Environment="PATH=/root/shuzagram_bot/venv/bin"
EnvironmentFile=/root/shuzagram_bot/.env
ExecStart=/root/shuzagram_bot/venv/bin/python /root/shuzagram_bot/bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
chmod 600 /root/shuzagram_bot/.env

systemctl daemon-reload
systemctl enable --now shuzagram-bot

systemctl status shuzagram-bot
journalctl -u shuzagram-bot -f
```

---

## 📖 Команды

### Пользовательские
| Команда | Описание |
|---|---|
| `/start` | Меню выбора зоны |
| `/my` | Мои номера |
| `/help` | Справка |

### Админские
| Команда | Описание |
|---|---|
| `/admin` | Inline-меню |
| `/adminhelp` | Справка по всем командам |
| `/num <номер>` | Добавить + закрепить одной командой |
| `/take <номер>` | Привязать существующий номер к себе |
| `/add <зона> <номер...>` | Добавить вручную |
| `/addzone <номер>` | Автоопределение зоны |
| `/addzone <зона> <номер>` | Явно указать зону |
| `/gen <зона> <кол-во>` | Сгенерировать случайные |
| `/del <номер>` | Удалить |
| `/free <номер>` | Освободить (без сброса счётчика) |
| `/unbind <номер>` | Отвязать + уменьшить счётчик |
| `/zones` | Список зон |
| `/delzone <зона>` | Удалить всю зону (с подтверждением) |
| `/list [зона]` | Список номеров |
| `/users` | Список юзеров |
| `/user <id>` | Инфо о юзере |
| `/reset <id>` | Сбросить счётчик юзера |
| `/stats` | Статистика |

---

## 🗂 Структура БД

### `bot_phone_numbers`
| Колонка | Тип | Описание |
|---|---|---|
| `id` | int | PK |
| `number` | varchar | Уникальный номер в формате `+...` |
| `zone` | varchar | Зона (`+7`, `+380`, ...) |
| `is_busy` | bool | Занят ли номер |
| `user_id` | bigint | Telegram ID владельца (если занят) |
| `created_at` | timestamp | Когда добавлен |
| `bound_at` | timestamp | Когда привязан к юзеру |

### `bot_users`
| Колонка | Тип | Описание |
|---|---|---|
| `user_id` | bigint | PK, Telegram ID |
| `username` | varchar | @username |
| `phone_count` | int | Сколько номеров взял |
| `first_seen` | timestamp | Первый заход |
| `source` | varchar | Источник (`main` / `bgram`) |

---

## ⚙️ Конфигурация в `bot.py`

```python
USER_LIMIT = 2              # Лимит номеров для обычных юзеров
AUTO_GEN_ON_DEMAND = True   # Автогенерация при пустом пуле
AUTO_GEN_COUNT = 1          # Сколько генерировать за раз

DEFAULT_ZONES = ["+7", "+1", "+49", "+380"]  # Всегда в меню
HIDDEN_ZONES = {"+888"}                       # Скрытые (только через /num)
```

Справочник стран — в `COUNTRY_CODES`. Хочешь добавить зону — добавь туда, и она появится в меню автоматически (если есть свободные номера).

---

## 🛡️ Безопасность

- **Токен бота** храни только в `.env` (он в `.gitignore`).
- **Права на `.env`**: `chmod 600 .env`.
- **Порт `9091`** слушает только `127.0.0.1` — снаружи недоступен.
- **Подпись вебхука** (`OTP_WEBHOOK_SECRET`) — опциональна, но рекомендуется если сервер не изолирован.
- **Не коммить** `.env`, `venv/`, `*.pyc`, `__pycache__/`.

---

## 📦 Зависимости

- `aiogram==3.7.0` — Telegram Bot API фреймворк
- `aiohttp==3.9.5` — HTTP-сервер для приёма вебхуков
- `sqlalchemy==2.0.30` + `asyncpg==0.29.0` — асинхронная работа с PostgreSQL
- `python-dotenv==1.0.1` — загрузка `.env`

---

## 🐛 Известные особенности

- **`TelegramNetworkError: Request timeout`** — бывает при сетевых проблемах до `api.telegram.org`. Обычно само проходит. Если повторяется — проверь маршрут до Telegram.
- **`column X does not exist`** — таблицы уже были созданы старой версией. Примени миграции (см. выше).
- **Код приходит не сразу** — ShuzaGram отправляет webhook с задержкой до 5 секунд.

---

## 📄 Лицензия

MIT — делай что хочешь.

---

## 🙏 Благодарности

- [ShuzaGram](https://github.com/Shuzycky-design/ShuzaGram) — MTProto-сервер
- [aiogram](https://github.com/aiogram/aiogram) — фреймворк для ботов