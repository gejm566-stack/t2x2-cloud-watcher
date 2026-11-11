# Автономная система 24/7 (ПК можно выключать)

Эта папка запускает полностью автономную цепочку **Twitch -> Telegram -> Kaggle GPU (Tesla T4) -> YouTube** без включённого домашнего компьютера.

Код внутри `cloud_24_7` **не содержит паролей и токенов** — все ключи хранятся в зашифрованных секретах (GitHub Secrets или `.env` на сервере).

---

## Какие 3 секрета понадобятся

1. `TELEGRAM_BOT_TOKEN` — токен твоего бота (из локального `d:\YT-DLP\Пайтон\bot_config.json`, поле `"bot_token"`).
2. `TELEGRAM_CHAT_ID` — твой Telegram ID (из `bot_config.json`, поле `"authorized_chat_id"`).
3. `KAGGLE_API_TOKEN` — твой токен Kaggle `KGAT_...` (из файла `%USERPROFILE%\.kaggle\access_token`).

---

## Вариант А: GitHub Actions (проще всего, без аренды серверов)

1. Создай новый репозиторий на GitHub (например `t2x2-cloud-watcher`).
   - Если сделать репозиторий **Public**, GitHub Actions работает **безлимитно и бесплатно** даже при проверке каждые **5 минут** (`*/5 * * * *`), а токены в `Secrets` всё равно скрыты и зашифрованы.
   - Если репозиторий **Private**, оставь `*/20 * * * *` или `*/25 * * * *`, чтобы укладываться в бесплатные 2000 минут/месяц.
2. Загрузи содержимое папки **`cloud_24_7`** в корень этого репозитория (включая скрытую папку `.github/workflows/twitch_watcher.yml`).
3. В репозитории открой **Settings -> Secrets and variables -> Actions -> New repository secret** и добавь 3 секрета:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `KAGGLE_API_TOKEN`
4. Перейди во вкладку **Actions -> 24/7 Twitch -> Telegram -> Kaggle Watcher -> Run workflow**.
   - Бот отправит приветствие в Telegram и запомнит последний VOD.
   - Далее компьютер можно **выключать полностью**: при каждом новом стриме бот напишет `"🔴 СТРИМ НАЧАЛСЯ!"`, дождётся окончания и сохранения записи (`RECORDED`), сам запустит **Kaggle Tesla T4** в режиме `both` (Длинные 1080p60 без рекламы + Shorts 9:16 без субтитров + ИИ-обложки 1080p) и зальёт на YouTube по расписанию.

---

## Вариант Б: Google Cloud `e2-micro` Always Free (мгновенные кнопки в Telegram 24/7)

Если хочешь, чтобы команды `/status`, `/check`, `/run`, `/auto` и кнопки срабатывали **мгновенно (за 0.5 сек)**, а не ждали следующего тика крона GitHub:

1. В Google Cloud Console создай бесплатную VM **Compute Engine**:
   - **Machine type:** `e2-micro`
   - **Region:** `us-central1`, `us-west1` или `us-east1` (входит в Google Always Free!)
   - **Boot disk:** Standard persistent disk (до 30 GB), **Ubuntu 22.04 / 24.04 LTS**
2. Скопируй папку `cloud_24_7` на сервер (через `git clone` или SSH Upload) и выполни:
   ```bash
   cd cloud_24_7
   chmod +x setup_gcp_e2micro.sh
   ./setup_gcp_e2micro.sh
   ```
3. Введи 3 токена при запросе скрипта — сервис `t2x2-bot.service` запустится в фоне и будет автоматически перезапускаться 24/7.

---

## Команды управления в Telegram (`@t2x2_my_clips_bot`)

- `/status` — текущий статус Twitch, последнего VOD и Kaggle GPU
- `/check` — проверить канал и показать карточку последнего VOD
- `/run` — прямо сейчас запустить нарезку последнего VOD на Kaggle
- `/auto` — включить/выключить автозапуск Kaggle сразу после окончания стрима
- `/kaggle` — проверить, работает ли сейчас облачный GPU-сервер
- Или просто пришли в чат ссылку `https://www.twitch.tv/videos/...`
