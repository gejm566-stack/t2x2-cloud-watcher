#!/usr/bin/env python3
"""
Интерактивный Telegram-менеджер (PyTelegramBotAPI) поверх единого движка cloud_watcher.py.
Работает 24/7 как на локальном ПК, так и на бесплатной VM Google Cloud e2-micro (systemd).
"""

import os
import re
import sys
import threading
import time
from pathlib import Path

try:
    import telebot
    from telebot import types
except ImportError:
    print("Библиотека pyTelegramBotAPI не установлена! Установите: pip install pyTelegramBotAPI")
    sys.exit(1)

import cloud_watcher as cw

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "bot_config.json"
QUEUE_FILE = BASE_DIR / "stream_queue.txt"

STATE_LOCK = threading.Lock()
USER_STATES = {}


def load_config() -> dict:
    return cw.load_state(CONFIG_FILE)


def save_config(cfg: dict) -> None:
    cw.save_state(cfg, CONFIG_FILE)


def is_authorized(chat_id: int) -> bool:
    settings = cw.load_secrets_and_settings(load_config())
    auth_id = settings["chat_id"]
    if not auth_id:
        return True
    return int(chat_id) == int(auth_id)


def add_task_to_queue(vod_url: str, mode: str = "both", exclude: str = None) -> None:
    entry = f"{vod_url} | mode={mode}"
    if exclude:
        entry += f" | exclude={exclude}"
    existing = []
    if QUEUE_FILE.exists():
        existing = [l.strip() for l in QUEUE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    existing.append(entry)
    QUEUE_FILE.write_text("\n".join(existing) + "\n", encoding="utf-8")


def _launch_from_bot(vod_url: str, mode: str = "both", exclude: str = None) -> tuple[bool, str]:
    with STATE_LOCK:
        state = load_config()
        settings = cw.load_secrets_and_settings(state)

    ok, msg = cw.launch_kaggle_cloud_job(
        vod_url=vod_url,
        mode=mode,
        exclude=exclude,
        interval_minutes=settings["interval_minutes"],
        auto_upload=settings["auto_upload"],
        bot_token=settings["bot_token"],
        chat_id=settings["chat_id"],
        kaggle_token=settings["kaggle_token"],
        kernel_id=settings["kernel_id"],
        dataset_id=settings["dataset_id"],
    )
    if ok:
        vod_id_m = re.search(r"/videos/(\d+)", vod_url)
        if vod_id_m:
            with STATE_LOCK:
                cfg = load_config()
                cfg["last_vod_id"] = vod_id_m.group(1)
                cfg["last_dispatched_vod_id"] = vod_id_m.group(1)
                cfg["pending_vod_id"] = ""
                save_config(cfg)
    return ok, msg


def create_vod_card_markup(vod_id: str, url: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🌐 Открыть на Twitch", url=url))
    markup.add(types.InlineKeyboardButton("🚀 Всё сразу (HD + Shorts -> YouTube)", callback_data=f"act_both:{vod_id}"))
    markup.add(
        types.InlineKeyboardButton("🎬 Длинные (1080p60)", callback_data=f"act_long:{vod_id}"),
        types.InlineKeyboardButton("📱 Только Shorts", callback_data=f"act_shorts:{vod_id}"),
    )
    markup.add(
        types.InlineKeyboardButton("✂️ Вырезать таймкоды", callback_data=f"act_cut:{vod_id}"),
        types.InlineKeyboardButton("❌ Пропустить", callback_data=f"act_skip:{vod_id}"),
    )
    return markup


def send_vod_card(bot, chat_id: int, vod: dict, prefix: str = "📢 Запись стрима готова к нарезке!") -> None:
    text = (
        f"{prefix}\n\n"
        f"📺 {vod.get('title') or 'Запись стрима'}\n"
        f"⏱ Длительность: {cw.format_duration(vod.get('duration'))}\n"
        f"🔗 {vod['url']}\n\n"
        f"Выберите действие (облачный сервер Kaggle Tesla T4):"
    )
    bot.send_message(
        chat_id,
        text,
        reply_markup=create_vod_card_markup(vod["id"], vod["url"]),
        disable_web_page_preview=True,
    )


def build_status_keyboard(state: dict) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    mon_on = bool(state.get("monitoring_enabled", True))
    auto_on = bool(state.get("auto_start", True))
    markup.add(
        types.InlineKeyboardButton(
            "⏸ Пауза мониторинга" if mon_on else "▶️ Вкл мониторинг",
            callback_data="cfg_toggle_monitor",
        ),
        types.InlineKeyboardButton(
            "🤖 Автозапуск: ВКЛ" if auto_on else "⏸ Автозапуск: ВЫКЛ",
            callback_data="cfg_toggle_auto",
        ),
    )
    markup.add(types.InlineKeyboardButton("🔄 Проверить Twitch сейчас", callback_data="cfg_check_now"))
    return markup


def monitor_loop() -> None:
    """Фоновый 24/7 цикл проверки стрима поверх единой машины состояний cloud_watcher."""
    time.sleep(5)
    while True:
        sleep_sec = 120
        try:
            with STATE_LOCK:
                state = load_config()
                settings = cw.load_secrets_and_settings(state)
                sleep_sec = 45 if state.get("waiting_for_vod_finalize") else settings["check_interval_seconds"]
                res = cw.process_tick(state, settings)
                save_config(state)
            if res.get("action") not in ("idle_offline", "still_live", "monitoring_paused"):
                print(f"[Monitor] {res}")
        except Exception as e:
            print(f"[Monitor error] {e}", file=sys.stderr)
            sleep_sec = 60
        time.sleep(sleep_sec)


def start_bot(token: str) -> None:
    bot = telebot.TeleBot(token)

    @bot.message_handler(commands=["start", "help"])
    def cmd_start(message):
        chat_id = message.chat.id
        with STATE_LOCK:
            cfg = load_config()
            if not cfg.get("authorized_chat_id"):
                cfg["authorized_chat_id"] = chat_id
                save_config(cfg)
        if not is_authorized(chat_id):
            bot.reply_to(message, "⛔ Нет доступа.")
            return

        bot.reply_to(
            message,
            "🤖 Автономный менеджер нарезки T2x2 готов!\n\n"
            "• При начале стрима пришлю уведомление в Telegram.\n"
            "• После завершения и сохранения VOD (RECORDED) автоматически запущу Kaggle Tesla T4 (Long 1080p60 + Shorts).\n\n"
            "Команды:\n"
            "• /status — статус Twitch, VOD и Kaggle\n"
            "• /check — проверить последний VOD и показать карточку\n"
            "• /run — принудительно запустить Kaggle на последний VOD\n"
            "• /auto — вкл/выкл автозапуск Kaggle после стрима\n"
            "• /monitor — вкл/выкл отслеживание Twitch\n"
            "• /kaggle — статус облачного GPU-ядра\n"
            "• /queue — локальный лог отправленных задач\n\n"
            "Или просто пришлите ссылку https://www.twitch.tv/videos/...",
            disable_web_page_preview=True,
        )

    @bot.message_handler(commands=["status"])
    def cmd_status(message):
        if not is_authorized(message.chat.id):
            bot.reply_to(message, "⛔ Нет доступа.")
            return
        msg_wait = bot.reply_to(message, "⏳ Опрашиваю Twitch и Kaggle...")
        with STATE_LOCK:
            state = load_config()
            settings = cw.load_secrets_and_settings(state)
        twitch = cw.check_twitch_status(settings["channel"])
        text = cw._format_status_text(twitch, state, settings)
        bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=msg_wait.message_id,
            reply_markup=build_status_keyboard(state),
            disable_web_page_preview=True,
        )

    @bot.message_handler(commands=["monitor"])
    def cmd_monitor(message):
        if not is_authorized(message.chat.id):
            return
        with STATE_LOCK:
            cfg = load_config()
            new_val = not bool(cfg.get("monitoring_enabled", True))
            cfg["monitoring_enabled"] = new_val
            save_config(cfg)
        bot.reply_to(message, f"🤖 Мониторинг Twitch теперь {'ВКЛЮЧЁН' if new_val else 'ВЫКЛЮЧЕН'}.")

    @bot.message_handler(commands=["auto"])
    def cmd_auto(message):
        if not is_authorized(message.chat.id):
            return
        with STATE_LOCK:
            cfg = load_config()
            new_val = not bool(cfg.get("auto_start", True))
            cfg["auto_start"] = new_val
            save_config(cfg)
            mode = cfg.get("auto_mode", "both")
        bot.reply_to(
            message,
            f"⚡ Автозапуск Kaggle после окончания стрима: {'ВКЛЮЧЁН' if new_val else 'ВЫКЛЮЧЕН'} (режим: {mode}).",
        )

    @bot.message_handler(commands=["kaggle"])
    def cmd_kaggle(message):
        if not is_authorized(message.chat.id):
            return
        with STATE_LOCK:
            settings = cw.load_secrets_and_settings(load_config())
        ks = cw.get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
        txt = f"🖥 Kaggle ({settings['kernel_id']}): {ks.get('status')}"
        if ks.get("failure_message"):
            txt += f"\n⚠️ {ks['failure_message']}"
        bot.reply_to(message, txt)

    @bot.message_handler(commands=["check"])
    def cmd_check(message):
        if not is_authorized(message.chat.id):
            return
        wait = bot.reply_to(message, "🔍 Проверяю канал через Twitch API...")
        with STATE_LOCK:
            settings = cw.load_secrets_and_settings(load_config())
        twitch = cw.check_twitch_status(settings["channel"])
        vod = twitch.get("vod")
        if twitch.get("is_live"):
            bot.edit_message_text(
                f"🔴 {settings['channel']} сейчас в эфире: {twitch.get('stream_title') or 'Без названия'}\n"
                f"Запуск начнётся автоматически после завершения и сохранения VOD.",
                chat_id=message.chat.id,
                message_id=wait.message_id,
            )
            return
        if not vod:
            bot.edit_message_text("❌ Не найдено записей стрима (VOD).", chat_id=message.chat.id, message_id=wait.message_id)
            return
        bot.delete_message(message.chat.id, wait.message_id)
        send_vod_card(bot, message.chat.id, vod, prefix="📺 Последний доступный VOD на канале:")

    @bot.message_handler(commands=["run"])
    def cmd_run(message):
        if not is_authorized(message.chat.id):
            return
        wait = bot.reply_to(message, "🔍 Ищу последний готовый VOD и запускаю Kaggle...")
        with STATE_LOCK:
            state = load_config()
            settings = cw.load_secrets_and_settings(state)
        twitch = cw.check_twitch_status(settings["channel"])
        vod = twitch.get("vod")
        if not vod:
            bot.edit_message_text("❌ Архивный VOD не найден.", chat_id=message.chat.id, message_id=wait.message_id)
            return
        add_task_to_queue(vod["url"], mode=settings["auto_mode"])
        ok, msg = _launch_from_bot(vod["url"], mode=settings["auto_mode"])
        bot.edit_message_text(
            f"{'🚀' if ok else '❌'} VOD #{vod['id']} ({vod['url']})\n{msg}",
            chat_id=message.chat.id,
            message_id=wait.message_id,
            disable_web_page_preview=True,
        )

    @bot.message_handler(commands=["queue"])
    def cmd_queue(message):
        if not is_authorized(message.chat.id):
            return
        if not QUEUE_FILE.exists() or not QUEUE_FILE.read_text(encoding="utf-8").strip():
            bot.reply_to(message, "📭 Лог очереди пуст.")
            return
        lines = QUEUE_FILE.read_text(encoding="utf-8").strip().splitlines()[-10:]
        q_text = "📋 Последние задачи:\n\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))
        bot.reply_to(message, q_text, disable_web_page_preview=True)

    @bot.message_handler(func=lambda m: m.text and "twitch.tv/videos/" in m.text.lower())
    def handle_twitch_url(message):
        if not is_authorized(message.chat.id):
            return
        m_url = re.search(r"(https?://(?:www\.)?twitch\.tv/videos/(\d+))", message.text, re.IGNORECASE)
        if not m_url:
            bot.reply_to(message, "❌ Пришлите прямую ссылку вида https://www.twitch.tv/videos/1234567890")
            return
        vod = {
            "id": m_url.group(2),
            "url": m_url.group(1),
            "title": f"Twitch VOD #{m_url.group(2)}",
            "duration": 0,
        }
        send_vod_card(bot, message.chat.id, vod, prefix="🔗 Ссылка получена! Выберите действие:")

    @bot.callback_query_handler(func=lambda call: True)
    def handle_callbacks(call):
        chat_id = call.message.chat.id
        if not is_authorized(chat_id):
            bot.answer_callback_query(call.id, "⛔ Нет доступа.")
            return

        data = str(call.data or "")
        if data == "cfg_toggle_monitor":
            with STATE_LOCK:
                cfg = load_config()
                cfg["monitoring_enabled"] = not bool(cfg.get("monitoring_enabled", True))
                save_config(cfg)
                settings = cw.load_secrets_and_settings(cfg)
            twitch = cw.check_twitch_status(settings["channel"])
            bot.answer_callback_query(call.id, "Обновлено")
            bot.edit_message_text(
                cw._format_status_text(twitch, cfg, settings),
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=build_status_keyboard(cfg),
                disable_web_page_preview=True,
            )
            return

        if data == "cfg_toggle_auto":
            with STATE_LOCK:
                cfg = load_config()
                cfg["auto_start"] = not bool(cfg.get("auto_start", True))
                save_config(cfg)
                settings = cw.load_secrets_and_settings(cfg)
            twitch = cw.check_twitch_status(settings["channel"])
            bot.answer_callback_query(call.id, "Обновлено")
            bot.edit_message_text(
                cw._format_status_text(twitch, cfg, settings),
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=build_status_keyboard(cfg),
                disable_web_page_preview=True,
            )
            return

        if data == "cfg_check_now":
            with STATE_LOCK:
                cfg = load_config()
                settings = cw.load_secrets_and_settings(cfg)
            twitch = cw.check_twitch_status(settings["channel"])
            bot.answer_callback_query(call.id, "Проверено!")
            bot.edit_message_text(
                cw._format_status_text(twitch, cfg, settings),
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=build_status_keyboard(cfg),
                disable_web_page_preview=True,
            )
            return

        sep = ":" if ":" in data else ("|" if "|" in data else None)
        if not sep:
            return
        action, vod_id = data.split(sep, 1)
        vod_url = f"https://www.twitch.tv/videos/{vod_id}"

        if action == "act_skip":
            with STATE_LOCK:
                cfg = load_config()
                cfg["last_vod_id"] = vod_id
                cfg["pending_vod_id"] = ""
                save_config(cfg)
            bot.answer_callback_query(call.id, "Пропущено")
            bot.edit_message_text(f"⏭ VOD #{vod_id} пропущен.", chat_id=chat_id, message_id=call.message.message_id)
            return

        if action == "act_cut":
            USER_STATES[chat_id] = {"action": "wait_exclude", "vod_id": vod_id}
            bot.answer_callback_query(call.id)
            bot.send_message(
                chat_id,
                "✏️ Введи таймкоды исключений через запятую (например: 01:15:00-01:25:00):",
            )
            return

        mode_map = {
            "act_both": "both",
            "act_long": "full",
            "act_shorts": "shorts",
        }
        if action in mode_map:
            mode = mode_map[action]
            add_task_to_queue(vod_url, mode=mode)
            ok, msg = _launch_from_bot(vod_url, mode=mode)
            bot.answer_callback_query(call.id, "Запущено!" if ok else "Ошибка!")
            bot.edit_message_text(
                f"{'🚀' if ok else '❌'} Режим: {cw._describe_mode(mode)}\n{vod_url}\n\n{msg}",
                chat_id=chat_id,
                message_id=call.message.message_id,
                disable_web_page_preview=True,
            )

    @bot.message_handler(
        func=lambda m: m.chat.id in USER_STATES and USER_STATES[m.chat.id].get("action") == "wait_exclude"
    )
    def handle_exclude_input(message):
        chat_id = message.chat.id
        state = USER_STATES.pop(chat_id)
        vod_id = state["vod_id"]
        vod_url = f"https://www.twitch.tv/videos/{vod_id}"
        exclude_val = (message.text or "").strip()

        add_task_to_queue(vod_url, mode="both", exclude=exclude_val)
        ok, msg = _launch_from_bot(vod_url, mode="both", exclude=exclude_val)
        bot.reply_to(
            message,
            f"✂️ Исключения `{exclude_val}` добавлены.\n{'🚀' if ok else '❌'} {msg}",
            disable_web_page_preview=True,
        )

    print("🤖 Telegram-бот @t2x2_my_clips_bot запущен (опрос Twitch каждые 120 сек)...")
    t_mon = threading.Thread(target=monitor_loop, daemon=True)
    t_mon.start()
    bot.infinity_polling(timeout=60, long_polling_timeout=60)


def main() -> None:
    with STATE_LOCK:
        cfg = load_config()
        settings = cw.load_secrets_and_settings(cfg)
    token = settings["bot_token"]
    if not token:
        print("❌ Не задан bot_token в bot_config.json или TELEGRAM_BOT_TOKEN в окружении.")
        sys.exit(1)
    start_bot(token)


if __name__ == "__main__":
    main()
