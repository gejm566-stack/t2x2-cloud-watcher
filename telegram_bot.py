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


def add_task_to_queue(vod_url: str, mode: str = "both", exclude: str = None, channel: str = None) -> None:
    entry = f"{vod_url} | channel={channel or 'auto'} | mode={mode}"
    if exclude:
        entry += f" | exclude={exclude}"
    existing = []
    if QUEUE_FILE.exists():
        existing = [l.strip() for l in QUEUE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    existing.append(entry)
    QUEUE_FILE.write_text("\n".join(existing) + "\n", encoding="utf-8")


def _fetch_twitch_map(settings: dict, channel: str = None) -> dict:
    channels = [cw.normalize_channel(channel)] if channel else (settings.get("channels") or list(cw.DEFAULT_CHANNELS))
    res = {}
    for ch in channels:
        try:
            res[ch] = cw.check_twitch_status(ch)
        except Exception as e:
            res[ch] = {"ok": False, "channel": ch, "is_live": False, "vod": None, "error": str(e)}
    return res


def _launch_from_bot(
    vod_url: str,
    mode: str = "both",
    exclude: str = None,
    channel: str = None,
) -> tuple[bool, str]:
    vod_id_m = re.search(r"/videos/(\d+)", vod_url)
    vod_id = vod_id_m.group(1) if vod_id_m else ""

    with STATE_LOCK:
        state = load_config()
        settings = cw.load_secrets_and_settings(state)

    ch = cw.normalize_channel(channel) if channel else cw.lookup_vod_channel(vod_id, state, default=settings["channel"])
    meta = cw.get_channel_meta(ch)

    kstat = cw.get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
    if kstat.get("status") in ("RUNNING", "QUEUED"):
        with STATE_LOCK:
            cfg = load_config()
            st = cw.load_secrets_and_settings(cfg)
            cw._enqueue_pending_job(cfg, vod_url, vod_id, ch, mode=mode, exclude=exclude)
            ch_state = cw.ensure_channel_state(cfg, ch, st)
            ch_state["pending_vod_id"] = vod_id
            cfg.setdefault("vod_channels", {})[vod_id] = ch
            save_config(cfg)
        return (
            False,
            f"⏳ Kaggle GPU сейчас занят ({kstat.get('status')}). Запись #{vod_id} ({meta['brand']}) добавлена в очередь — запустится автоматически!",
        )

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
        profile=meta["profile"],
    )
    if ok and vod_id:
        with STATE_LOCK:
            cfg = load_config()
            st = cw.load_secrets_and_settings(cfg)
            ch_state = cw.ensure_channel_state(cfg, ch, st)
            ch_state["last_vod_id"] = vod_id
            ch_state["last_dispatched_vod_id"] = vod_id
            ch_state["pending_vod_id"] = ""
            cw._remove_pending_job(cfg, vod_id)
            cfg.setdefault("vod_channels", {})[vod_id] = ch
            cw._record_active_kaggle_job(
                cfg,
                vod_id=vod_id,
                channel=ch,
                brand=str(meta.get("brand") or meta.get("display") or ch),
                mode=mode,
                url=vod_url,
            )
            save_config(cfg)
    return ok, msg


def create_vod_card_markup(vod_id: str, url: str, channel: str = "t2x2") -> types.InlineKeyboardMarkup:
    ch = cw.normalize_channel(channel)
    meta = cw.get_channel_meta(ch)
    other = "5opka" if ch == "t2x2" else "t2x2"
    other_brand = cw.get_channel_meta(other)["brand"]

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🌐 Открыть на Twitch", url=url))
    markup.add(types.InlineKeyboardButton(f"🚀 Всё сразу ({meta['brand']} -> YouTube)", callback_data=f"act_both:{vod_id}"))
    markup.add(
        types.InlineKeyboardButton("🎬 Длинные (1080p60)", callback_data=f"act_long:{vod_id}"),
        types.InlineKeyboardButton("📱 Только Shorts", callback_data=f"act_shorts:{vod_id}"),
    )
    markup.add(
        types.InlineKeyboardButton("✂️ Вырезать таймкоды", callback_data=f"act_cut:{vod_id}"),
        types.InlineKeyboardButton("❌ Пропустить", callback_data=f"act_skip:{vod_id}"),
    )
    markup.add(
        types.InlineKeyboardButton(
            f"🔄 Канал: {meta['brand']} -> сменить на {other_brand}",
            callback_data=f"act_swap:{vod_id}",
        )
    )
    return markup


def send_vod_card(
    bot,
    chat_id: int,
    vod: dict,
    prefix: str = "📢 Запись стрима готова к нарезке!",
    channel: str = "t2x2",
) -> None:
    ch = cw.normalize_channel(channel)
    meta = cw.get_channel_meta(ch)
    text = (
        f"{prefix}\n\n"
        f"🎯 Канал: {meta['display']} ({meta['brand']})\n"
        f"📺 {vod.get('title') or 'Запись стрима'}\n"
        f"⏱ Длительность: {cw.format_duration(vod.get('duration'))}\n"
        f"🔗 {vod['url']}\n\n"
        f"Выберите действие (облачный сервер Kaggle Tesla T4):"
    )
    bot.send_message(
        chat_id,
        text,
        reply_markup=create_vod_card_markup(vod["id"], vod["url"], channel=ch),
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
    """Фоновый 24/7 цикл проверки стримов (T2x2 & 5opka) поверх единой машины состояний cloud_watcher."""
    time.sleep(5)
    while True:
        sleep_sec = 120
        try:
            with STATE_LOCK:
                state = load_config()
                settings = cw.load_secrets_and_settings(state)
                any_waiting = any(
                    bool((state.get("channels") or {}).get(ch, {}).get("waiting_for_vod_finalize"))
                    for ch in settings["channels"]
                )
                sleep_sec = 45 if (any_waiting or state.get("pending_jobs")) else settings["check_interval_seconds"]
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
            "🤖 Автономный менеджер нарезки T2x2 & 5opka готов!\n\n"
            "• Бот одновременно отслеживает **t2x2** (T2x2 CLIPS) и **5opka** (5opka CLIPS).\n"
            "• При завершении стрима и сохранении VOD (RECORDED) автоматически запускает Kaggle Tesla T4.\n\n"
            "Команды:\n"
            "• /status — статус обоих каналов, VOD и Kaggle\n"
            "• /check — проверить последний VOD обоих каналов и показать карточки\n"
            "• /check_t2x2 | /check_5opka — проверить только один канал\n"
            "• /run — выбрать канал и запустить последний VOD\n"
            "• /run_t2x2 | /run_5opka — сразу запустить последний VOD нужного канала\n"
            "• /auto [t2x2|5opka] — вкл/выкл автозапуск Kaggle после стрима\n"
            "• /monitor — вкл/выкл отслеживание Twitch\n"
            "• /kaggle — статус облачного GPU-ядра и очереди\n"
            "• /queue — локальный лог отправленных задач\n\n"
            "Или просто пришлите ссылку https://www.twitch.tv/videos/... (канал определится сам)",
            disable_web_page_preview=True,
        )

    @bot.message_handler(commands=["status"])
    def cmd_status(message):
        if not is_authorized(message.chat.id):
            bot.reply_to(message, "⛔ Нет доступа.")
            return
        msg_wait = bot.reply_to(message, "⏳ Опрашиваю Twitch (t2x2 & 5opka) и Kaggle...")
        with STATE_LOCK:
            state = load_config()
            settings = cw.load_secrets_and_settings(state)
        twitch_map = _fetch_twitch_map(settings)
        text = cw._format_status_text(twitch_map, state, settings)
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
        bot.reply_to(message, f"🤖 Мониторинг Twitch (t2x2 & 5opka) теперь {'ВКЛЮЧЁН' if new_val else 'ВЫКЛЮЧЕН'}.")

    @bot.message_handler(commands=["auto", "auto_on", "auto_off", "auto_t2x2", "auto_5opka"])
    def cmd_auto(message):
        if not is_authorized(message.chat.id):
            return
        parts = (message.text or "").strip().split()
        head = parts[0].lower().split("@")[0] if parts else "/auto"
        rest = parts[1:]
        with STATE_LOCK:
            cfg = load_config()
            settings = cw.load_secrets_and_settings(cfg)
            target_ch = cw._parse_channel_arg(head, rest, settings["channels"])
            action_chs = [target_ch] if target_ch else settings["channels"]
            if head.startswith("/auto_on"):
                new_val = True
            elif head.startswith("/auto_off"):
                new_val = False
            else:
                curr = any(cw._channel_auto_start(ch, cfg, settings) for ch in action_chs)
                new_val = not curr
            for ch in action_chs:
                ch_state = cw.ensure_channel_state(cfg, ch, settings)
                ch_state["auto_start"] = new_val
                ch_state["auto_start_override"] = new_val
            if not target_ch:
                cfg["auto_start"] = new_val
            save_config(cfg)
            mode = settings.get("auto_mode", "both")
        label = cw.get_channel_meta(target_ch)["brand"] if target_ch else "всех каналов (T2x2 + 5opka)"
        bot.reply_to(
            message,
            f"⚡ Автозапуск Kaggle для {label}: {'ВКЛЮЧЁН' if new_val else 'ВЫКЛЮЧЕН'} (режим: {mode}).",
        )

    @bot.message_handler(commands=["kaggle"])
    def cmd_kaggle(message):
        if not is_authorized(message.chat.id):
            return
        with STATE_LOCK:
            cfg = load_config()
            settings = cw.load_secrets_and_settings(cfg)
        ks = cw.get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
        txt = f"🖥 Kaggle ({settings['kernel_id']}): {ks.get('status')}"
        queue = cfg.get("pending_jobs") or []
        if queue:
            q_str = ", ".join(f"#{j.get('vod_id')} ({cw.get_channel_meta(j.get('channel', 't2x2'))['brand']})" for j in queue)
            txt += f"\n⏳ Очередь ({len(queue)}): {q_str}"
        if ks.get("failure_message"):
            txt += f"\n⚠️ {ks['failure_message']}"
        bot.reply_to(message, txt)

    @bot.message_handler(commands=["check", "check_t2x2", "check_5opka"])
    def cmd_check(message):
        if not is_authorized(message.chat.id):
            return
        parts = (message.text or "").strip().split()
        head = parts[0].lower().split("@")[0] if parts else "/check"
        rest = parts[1:]
        wait = bot.reply_to(message, "🔍 Проверяю каналы через Twitch API...")
        with STATE_LOCK:
            cfg = load_config()
            settings = cw.load_secrets_and_settings(cfg)
        target_ch = cw._parse_channel_arg(head, rest, settings["channels"])
        check_list = [target_ch] if target_ch else settings["channels"]
        twitch_map = _fetch_twitch_map(settings, channel=target_ch)

        with STATE_LOCK:
            cfg = load_config()
            for ch, tw in twitch_map.items():
                if tw.get("vod") and tw["vod"].get("id"):
                    cfg.setdefault("vod_channels", {})[str(tw["vod"]["id"])] = ch
            save_config(cfg)

        try:
            bot.delete_message(message.chat.id, wait.message_id)
        except Exception:
            pass

        for ch in check_list:
            tw = twitch_map.get(ch) or {}
            meta = cw.get_channel_meta(ch)
            if tw.get("is_live"):
                bot.send_message(
                    message.chat.id,
                    f"🔴 {meta['display']} ({meta['brand']}) сейчас в эфире: {tw.get('stream_title') or 'Без названия'}\n"
                    f"Запуск начнётся автоматически после завершения и сохранения VOD.",
                )
                continue
            vod = tw.get("vod")
            if not vod:
                bot.send_message(message.chat.id, f"❌ Не найдено записей стрима (VOD) для {meta['display']}.")
                continue
            send_vod_card(
                bot,
                message.chat.id,
                vod,
                prefix=f"📺 Последний доступный VOD ({meta['brand']}):",
                channel=ch,
            )

    def _run_channel_latest(chat_id: int, message_id: int | None, target_ch: str, settings: dict) -> None:
        meta = cw.get_channel_meta(target_ch)
        twitch = cw.check_twitch_status(target_ch)
        vod = twitch.get("vod")
        if not vod:
            txt = f"❌ Архивный VOD для {meta['display']} не найден."
            if message_id:
                bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id)
            else:
                bot.send_message(chat_id, txt)
            return
        add_task_to_queue(vod["url"], mode=settings["auto_mode"], channel=target_ch)
        ok, msg = _launch_from_bot(vod["url"], mode=settings["auto_mode"], channel=target_ch)
        txt = f"{'🚀' if ok else 'ℹ️'} [{meta['brand']}] VOD #{vod['id']} ({vod['url']})\n{msg}"
        if message_id:
            bot.edit_message_text(txt, chat_id=chat_id, message_id=message_id, disable_web_page_preview=True)
        else:
            bot.send_message(chat_id, txt, disable_web_page_preview=True)

    @bot.message_handler(commands=["run", "run_t2x2", "run_5opka"])
    def cmd_run(message):
        if not is_authorized(message.chat.id):
            return
        parts = (message.text or "").strip().split()
        head = parts[0].lower().split("@")[0] if parts else "/run"
        rest = parts[1:]
        with STATE_LOCK:
            state = load_config()
            settings = cw.load_secrets_and_settings(state)
        target_ch = cw._parse_channel_arg(head, rest, settings["channels"])
        if not target_ch:
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                types.InlineKeyboardButton("🚀 Последний VOD T2x2 (T2x2 CLIPS)", callback_data="act_runch:t2x2"),
                types.InlineKeyboardButton("🚀 Последний VOD 5opka (5opka CLIPS)", callback_data="act_runch:5opka"),
            )
            bot.reply_to(
                message,
                "👇 Выбери канал, чей последний VOD запустить на Kaggle Tesla T4:",
                reply_markup=kb,
            )
            return
        wait = bot.reply_to(message, f"🔍 Ищу последний VOD канала {target_ch} и запускаю Kaggle...")
        _run_channel_latest(message.chat.id, wait.message_id, target_ch, settings)

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
        vod_id = m_url.group(2)
        with STATE_LOCK:
            cfg = load_config()
            settings = cw.load_secrets_and_settings(cfg)
        explicit_ch = cw._parse_channel_arg("", (message.text or "").split(), settings["channels"])
        detected_ch = explicit_ch or cw.lookup_vod_channel(vod_id, cfg, default=settings["channel"])
        with STATE_LOCK:
            cfg2 = load_config()
            cfg2.setdefault("vod_channels", {})[vod_id] = detected_ch
            save_config(cfg2)
        vod = {
            "id": vod_id,
            "url": m_url.group(1),
            "title": f"Twitch VOD #{vod_id}",
            "duration": 0,
        }
        send_vod_card(bot, message.chat.id, vod, prefix="🔗 Ссылка получена! Выберите действие:", channel=detected_ch)

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
            twitch_map = _fetch_twitch_map(settings)
            bot.answer_callback_query(call.id, "Обновлено")
            bot.edit_message_text(
                cw._format_status_text(twitch_map, cfg, settings),
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=build_status_keyboard(cfg),
                disable_web_page_preview=True,
            )
            return

        if data == "cfg_toggle_auto":
            with STATE_LOCK:
                cfg = load_config()
                settings = cw.load_secrets_and_settings(cfg)
                new_val = not bool(cfg.get("auto_start", True))
                cfg["auto_start"] = new_val
                for ch in settings["channels"]:
                    ch_state = cw.ensure_channel_state(cfg, ch, settings)
                    ch_state["auto_start"] = new_val
                    ch_state["auto_start_override"] = new_val
                save_config(cfg)
                settings = cw.load_secrets_and_settings(cfg)
            twitch_map = _fetch_twitch_map(settings)
            bot.answer_callback_query(call.id, "Обновлено")
            bot.edit_message_text(
                cw._format_status_text(twitch_map, cfg, settings),
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
            twitch_map = _fetch_twitch_map(settings)
            bot.answer_callback_query(call.id, "Проверено!")
            bot.edit_message_text(
                cw._format_status_text(twitch_map, cfg, settings),
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=build_status_keyboard(cfg),
                disable_web_page_preview=True,
            )
            return

        sep = ":" if ":" in data else ("|" if "|" in data else None)
        if not sep:
            return
        action, payload_arg = data.split(sep, 1)

        if action == "act_runch":
            target_ch = cw.normalize_channel(payload_arg)
            with STATE_LOCK:
                settings = cw.load_secrets_and_settings(load_config())
            bot.answer_callback_query(call.id, f"Запуск {target_ch}...")
            _run_channel_latest(chat_id, call.message.message_id, target_ch, settings)
            return

        vod_id = payload_arg
        vod_url = f"https://www.twitch.tv/videos/{vod_id}"

        if action == "act_swap":
            with STATE_LOCK:
                cfg = load_config()
                settings = cw.load_secrets_and_settings(cfg)
                curr_ch = cw.lookup_vod_channel(vod_id, cfg, default=settings["channel"])
                new_ch = "5opka" if curr_ch == "t2x2" else "t2x2"
                cfg.setdefault("vod_channels", {})[vod_id] = new_ch
                save_config(cfg)
            new_brand = cw.get_channel_meta(new_ch)["brand"]
            bot.answer_callback_query(call.id, f"Канал переключён на {new_brand}")
            bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=create_vod_card_markup(vod_id, vod_url, channel=new_ch),
            )
            return

        if action == "act_skip":
            with STATE_LOCK:
                cfg = load_config()
                settings = cw.load_secrets_and_settings(cfg)
                vod_ch = cw.lookup_vod_channel(vod_id, cfg, default=settings["channel"])
                ch_state = cw.ensure_channel_state(cfg, vod_ch, settings)
                ch_state["last_vod_id"] = vod_id
                ch_state["pending_vod_id"] = ""
                cw._remove_pending_job(cfg, vod_id)
                save_config(cfg)
            bot.answer_callback_query(call.id, "Пропущено")
            bot.edit_message_text(
                f"⏭ VOD #{vod_id} ({cw.get_channel_meta(vod_ch)['brand']}) пропущен.",
                chat_id=chat_id,
                message_id=call.message.message_id,
            )
            return

        if action == "act_cut":
            with STATE_LOCK:
                cfg = load_config()
                settings = cw.load_secrets_and_settings(cfg)
                vod_ch = cw.lookup_vod_channel(vod_id, cfg, default=settings["channel"])
            USER_STATES[chat_id] = {"action": "wait_exclude", "vod_id": vod_id, "channel": vod_ch}
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
            with STATE_LOCK:
                cfg = load_config()
                settings = cw.load_secrets_and_settings(cfg)
                vod_ch = cw.lookup_vod_channel(vod_id, cfg, default=settings["channel"])
            brand = cw.get_channel_meta(vod_ch)["brand"]
            add_task_to_queue(vod_url, mode=mode, channel=vod_ch)
            ok, msg = _launch_from_bot(vod_url, mode=mode, channel=vod_ch)
            bot.answer_callback_query(call.id, "Запущено!" if ok else "В очереди / Ошибка")
            bot.edit_message_text(
                f"{'🚀' if ok else '⏳'} [{brand}] Режим: {cw._describe_mode(mode)}\n{vod_url}\n\n{msg}",
                chat_id=chat_id,
                message_id=call.message.message_id,
                disable_web_page_preview=True,
            )

    @bot.message_handler(
        func=lambda m: m.chat.id in USER_STATES and USER_STATES[m.chat.id].get("action") == "wait_exclude"
    )
    def handle_exclude_input(message):
        chat_id = message.chat.id
        state_u = USER_STATES.pop(chat_id)
        vod_id = state_u["vod_id"]
        vod_ch = state_u.get("channel") or "t2x2"
        brand = cw.get_channel_meta(vod_ch)["brand"]
        vod_url = f"https://www.twitch.tv/videos/{vod_id}"
        exclude_val = (message.text or "").strip()

        add_task_to_queue(vod_url, mode="both", exclude=exclude_val, channel=vod_ch)
        ok, msg = _launch_from_bot(vod_url, mode="both", exclude=exclude_val, channel=vod_ch)
        bot.reply_to(
            message,
            f"✂️ [{brand}] Исключения `{exclude_val}` добавлены.\n{'🚀' if ok else '⏳'} {msg}",
            disable_web_page_preview=True,
        )

    print("🤖 Telegram-бот @t2x2_my_clips_bot запущен (T2x2 & 5opka, опрос каждые 120 сек)...")
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
