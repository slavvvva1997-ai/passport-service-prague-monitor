# Онлайн-мониторинг электронной очереди ДП «Документ» в Праге

## 1. Что делает проект

Проект проверяет публичную страницу электронной очереди ДП «Документ» в Праге:

https://prague.pasport.org.ua/solutions/e-queue

Скрипт запускается как Railway Cron Job или Render Cron Job каждые 5 минут, получает HTML страницы, извлекает чистый текст, определяет статус и отправляет Telegram-уведомление только при изменении статуса или заметном изменении текста страницы.

Статусы:

- `unavailable`
- `possibly_available`
- `blocked`
- `error`
- `unknown`

Состояние хранится в PostgreSQL/SQLite через `DATABASE_URL` или локально в `state.json`.

## 2. Что проект НЕ делает

- не записывает автоматически;
- не обходит SMS, Viber, Дію, капчу или другие защитные механизмы;
- не подбирает номера телефонов;
- не взламывает сайт;
- не гарантирует наличие талонов.

Проект только проверяет публичную страницу и отправляет уведомление.

## 3. Как создать Telegram-бота через BotFather

1. Открой Telegram.
2. Найди `@BotFather`.
3. Отправь команду `/newbot`.
4. Введи имя бота.
5. Введи username бота, который заканчивается на `bot`.
6. BotFather выдаст токен. Сохрани его как `TELEGRAM_BOT_TOKEN`.

Не добавляй токен в код и не публикуй его в GitHub.

## 4. Как получить TELEGRAM_CHAT_ID

1. Напиши любое сообщение своему новому боту.
2. Открой в браузере:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

3. Найди поле `chat.id`.
4. Сохрани это значение как `TELEGRAM_CHAT_ID`.

Если бот добавлен в группу, сначала напиши сообщение в группу, затем снова открой `getUpdates`.

## 5. Как залить проект в GitHub

```bash
git init
git add .
git commit -m "feat: add passport service monitor"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

Если этот каталог лежит внутри другого репозитория, можно либо вынести папку `passport-service-prague-monitor` в отдельный репозиторий, либо при деплое указать root directory этой папки.

## 6. Как запустить на Railway

1. Создай новый Railway project.
2. Подключи GitHub repository.
3. Если это monorepo, укажи root directory `passport-service-prague-monitor`.
4. Railway прочитает `railway.json`.
5. Проверь, что Start Command: `python monitor.py`.
6. Проверь Cron Schedule: `*/5 * * * *`.
7. Добавь environment variables:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
URL=https://prague.pasport.org.ua/solutions/e-queue
NOTIFY_COOLDOWN_SECONDS=1800
REQUEST_TIMEOUT_SECONDS=20
USER_AGENT=Mozilla/5.0 (compatible; PassportServicePragueMonitor/1.0; +https://prague.pasport.org.ua/solutions/e-queue)
DATABASE_URL=...
```

8. Для постоянного хранения добавь PostgreSQL в Railway и используй его `DATABASE_URL`.
9. Открой logs и проверь строки с `parsed_status`, `http_status`, `text_hash`.
10. Для проверки Telegram временно измени Start Command на `python monitor.py --test-telegram`, запусти job вручную, затем верни `python monitor.py`.

Railway cron использует UTC и не запускает jobs чаще одного раза в 5 минут.

## 7. Как запустить на Render

Вариант через Dashboard:

1. Создай Cron Job.
2. Подключи GitHub repository.
3. Если это monorepo, укажи root directory `passport-service-prague-monitor`.
4. Build Command:

```bash
pip install -r requirements.txt
```

5. Command:

```bash
python monitor.py
```

6. Schedule:

```text
*/5 * * * *
```

7. Добавь environment variables из `.env.example`.
8. Для постоянного хранения подключи PostgreSQL и добавь `DATABASE_URL`.

Вариант через Blueprint: используй `render.yaml` из этого проекта.

Render Cron Jobs не имеют постоянного диска, поэтому для надежного хранения состояния лучше использовать `DATABASE_URL`.

## 8. Что делать, если сайт возвращает 403

- Проверь страницу вручную в браузере.
- Уменьши частоту cron job, например до `*/10 * * * *` или `*/15 * * * *`.
- Не пытайся обходить защиту, SMS, Viber, Дію или капчу.
- Используй уведомление `blocked` как сигнал, что мониторинг не может проверить страницу.

## 9. Как проверить, что всё работает

Локально:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python monitor.py --dry-run
python monitor.py --test-telegram
python monitor.py
```

На macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python monitor.py --dry-run
python monitor.py --test-telegram
python monitor.py
```

В логах должна появиться JSON-строка с полями:

- `timestamp`
- `url`
- `http_status`
- `parsed_status`
- `text_hash`
- `notification_sent`
- `error`

`--dry-run` не отправляет Telegram и не сохраняет состояние.

## 10. Как отключить проект

Railway:

1. Открой project.
2. Открой service с cron job.
3. Удали Cron Schedule или останови service.

Render:

1. Открой Cron Job.
2. Нажми Suspend или Delete.

Также можно удалить `TELEGRAM_BOT_TOKEN`, чтобы уведомления перестали отправляться.

## Переменные окружения

Обязательные:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Опциональные:

- `URL`
- `NOTIFY_COOLDOWN_SECONDS`
- `REQUEST_TIMEOUT_SECONDS`
- `USER_AGENT`
- `DATABASE_URL`
- `STATE_FILE`

Если `DATABASE_URL` не задан, скрипт хранит состояние в `state.json`. На некоторых хостингах локальный файл может сбрасываться между деплоями или запусками cron job, поэтому для Railway/Render лучше использовать PostgreSQL.
