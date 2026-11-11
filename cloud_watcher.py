#!/usr/bin/env python3
"""
24/7 Twitch -> Telegram -> Kaggle GPU Autonomous Cloud Watcher.

Работает в трёх режимах:
  1. Импортируемый модуль для локального/VPS telegram_bot.py
  2. Однократный тик (`python cloud_watcher.py --once`) для GitHub Actions Cron (ПК выключен)
  3. Бесконечный облачный демон (`python cloud_watcher.py --daemon`) для Google Cloud e2-micro / VPS
"""

import argparse
import ast
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
DEFAULT_KAGGLE_KERNEL = "istfarmen/t2x2-clips-processor"
DEFAULT_KAGGLE_DATASET = "istfarmen/t2x2-clips-bundle"
MIN_VOD_DURATION_SEC = 180

# Настройки по умолчанию для автономного облачного состояния
DEFAULT_STATE = {
    "streamer_channel": "t2x2",
    "check_interval_seconds": 120,
    "monitoring_enabled": True,
    "auto_start": True,
    "auto_mode": "both",
    "interval_minutes": 30,
    "auto_upload": True,
    "is_streamer_live": False,
    "stream_end_notified": False,
    "waiting_for_vod_finalize": False,
    "live_stream_id": "",
    "live_stream_title": "",
    "live_vod_id": "",
    "pending_vod_id": "",
    "last_vod_id": "",
    "last_dispatched_vod_id": "",
    "last_dispatched_at": "",
    "update_offset": 0,
    "initialized": False,
}


def _parse_bool(val, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on", "да", "вкл"):
        return True
    if s in ("0", "false", "no", "off", "нет", "выкл"):
        return False
    return default


def resolve_state_file(explicit_path: str = None) -> Path:
    """Выбирает файл состояния: bot_config.json (если рядом есть) или bot_state.json."""
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    env_path = os.environ.get("STATE_FILE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    cfg_path = BASE_DIR / "bot_config.json"
    if cfg_path.exists():
        return cfg_path
    return BASE_DIR / "bot_state.json"


def load_state(state_path: Path = None) -> dict:
    path = resolve_state_file(str(state_path) if state_path else None)
    data = dict(DEFAULT_STATE)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                data.update(raw)
        except Exception as e:
            print(f"[State] Не удалось прочитать {path}: {e}", file=sys.stderr)
    return data


def save_state(state: dict, state_path: Path = None) -> Path:
    path = resolve_state_file(str(state_path) if state_path else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return path


def load_secrets_and_settings(state: dict) -> dict:
    """
    Собирает токены и настройки из env (приоритет для GitHub Actions / VPS)
    с безопасным фоллбэком на локальный bot_config.json и ~/.kaggle/access_token.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or state.get("bot_token") or ""
    raw_chat = os.environ.get("TELEGRAM_CHAT_ID") or state.get("authorized_chat_id") or 0
    try:
        chat_id = int(str(raw_chat).strip()) if raw_chat else 0
    except ValueError:
        chat_id = 0

    kaggle_token = os.environ.get("KAGGLE_API_TOKEN") or ""
    if not kaggle_token:
        for candidate in (
            Path.home() / ".kaggle" / "access_token",
            Path.home() / ".config" / "kaggle" / "access_token",
        ):
            if candidate.exists():
                kaggle_token = candidate.read_text(encoding="utf-8").strip()
                if kaggle_token:
                    break
    if kaggle_token:
        os.environ["KAGGLE_API_TOKEN"] = kaggle_token

    channel = (os.environ.get("STREAMER_CHANNEL") or state.get("streamer_channel") or "t2x2").strip()
    kernel_id = (os.environ.get("KAGGLE_KERNEL_ID") or state.get("kaggle_kernel_id") or DEFAULT_KAGGLE_KERNEL).strip()
    dataset_id = (os.environ.get("KAGGLE_DATASET_ID") or state.get("kaggle_dataset_id") or DEFAULT_KAGGLE_DATASET).strip()
    auto_start = _parse_bool(os.environ.get("AUTO_START"), bool(state.get("auto_start", True)))
    auto_mode = (os.environ.get("AUTO_MODE") or state.get("auto_mode") or "both").strip().lower()
    if auto_mode not in ("both", "full", "long", "shorts", "cut_only"):
        auto_mode = "both"

    try:
        interval_minutes = int(os.environ.get("INTERVAL_MINUTES") or state.get("interval_minutes") or 30)
    except ValueError:
        interval_minutes = 30

    auto_upload = _parse_bool(os.environ.get("AUTO_UPLOAD"), bool(state.get("auto_upload", True)))

    try:
        check_interval = int(os.environ.get("CHECK_INTERVAL_SECONDS") or state.get("check_interval_seconds") or 120)
    except ValueError:
        check_interval = 120

    return {
        "bot_token": str(bot_token).strip(),
        "chat_id": chat_id,
        "kaggle_token": str(kaggle_token).strip(),
        "channel": channel,
        "kernel_id": kernel_id,
        "dataset_id": dataset_id,
        "auto_start": auto_start,
        "auto_mode": auto_mode,
        "interval_minutes": interval_minutes,
        "auto_upload": auto_upload,
        "check_interval_seconds": max(30, check_interval),
    }


def format_duration(seconds) -> str:
    if not seconds:
        return "неизвестно"
    sec = int(float(seconds))
    hrs, rem = divmod(sec, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}ч {mins:02d}м"
    return f"{mins}м {secs:02d}с"


def tg_api_call(bot_token: str, method: str, payload: dict = None, timeout: int = 20) -> dict:
    if not bot_token:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN is empty"}
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        return {"ok": False, "error_code": e.code, "description": body or str(e)}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def tg_send(bot_token: str, chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = None) -> dict:
    if not bot_token or not chat_id:
        print(f"[TG dry-message -> {chat_id}] {text}")
        return {"ok": False, "description": "Missing token or chat_id"}
    payload = {
        "chat_id": int(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = tg_api_call(bot_token, "sendMessage", payload)
    if not res.get("ok"):
        print(f"[TG send error] {res}", file=sys.stderr)
    return res


def check_twitch_status_gql(channel: str = "t2x2", timeout: int = 12) -> dict:
    """
    Сверхбыстрая (~30-150 мс) проверка Twitch без yt-dlp через публичный GraphQL:
    возвращает статус онлайна + последний ARCHIVE VOD со статусом RECORDING / RECORDED.
    """
    query = """
    query($login: String!) {
      user(login: $login) {
        id
        login
        displayName
        stream {
          id
          title
          createdAt
          viewersCount
          game { name }
        }
        videos(first: 2, type: ARCHIVE) {
          edges {
            node {
              id
              title
              lengthSeconds
              createdAt
              publishedAt
              status
            }
          }
        }
      }
    }
    """
    body = json.dumps([{"query": query, "variables": {"login": channel.lower()}}]).encode("utf-8")
    req = urllib.request.Request(
        TWITCH_GQL_URL,
        data=body,
        headers={
            "Client-ID": TWITCH_GQL_CLIENT_ID,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    user = (data[0].get("data") or {}).get("user") if isinstance(data, list) and data else None
    if not user:
        raise RuntimeError(f"Twitch user '{channel}' not found in GQL response")

    stream = user.get("stream")
    edges = ((user.get("videos") or {}).get("edges") or [])
    latest_vod = edges[0].get("node") if edges else None

    vod_id = str(latest_vod.get("id")) if latest_vod and latest_vod.get("id") else ""
    vod_status = str(latest_vod.get("status") or "").upper() if latest_vod else ""
    vod_duration = int(latest_vod.get("lengthSeconds") or 0) if latest_vod else 0
    vod_title = str(latest_vod.get("title") or "") if latest_vod else ""

    return {
        "ok": True,
        "source": "gql",
        "channel": user.get("login") or channel,
        "display_name": user.get("displayName") or channel,
        "is_live": bool(stream),
        "stream_id": str((stream or {}).get("id") or ""),
        "stream_title": str((stream or {}).get("title") or ""),
        "stream_started_at": str((stream or {}).get("createdAt") or ""),
        "viewers_count": int((stream or {}).get("viewersCount") or 0),
        "game_name": str(((stream or {}).get("game") or {}).get("name") or ""),
        "vod": {
            "id": vod_id,
            "url": f"https://www.twitch.tv/videos/{vod_id}" if vod_id else "",
            "title": vod_title,
            "duration": vod_duration,
            "status": vod_status,
            "created_at": str((latest_vod or {}).get("createdAt") or ""),
        } if vod_id else None,
    }


def check_twitch_status_ytdlp_fallback(channel: str = "t2x2") -> dict:
    """Резервный опрос через yt-dlp, если Twitch GQL временно недоступен."""
    is_live = False
    stream_title = ""
    try:
        res_live = subprocess.run(
            ["yt-dlp", "--dump-single-json", "--no-warnings", f"https://www.twitch.tv/{channel}"],
            capture_output=True, text=True, timeout=25, encoding="utf-8", errors="ignore"
        )
        if res_live.returncode == 0 and res_live.stdout.strip().startswith("{"):
            info = json.loads(res_live.stdout)
            is_live = bool(info.get("is_live"))
            stream_title = str(info.get("title") or "")
    except Exception:
        pass

    vod = None
    try:
        res_vod = subprocess.run(
            ["yt-dlp", "--dump-single-json", "--playlist-items", "1", "--no-warnings",
             f"https://www.twitch.tv/{channel}/videos?filter=archives"],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="ignore"
        )
        if res_vod.returncode == 0 and res_vod.stdout.strip().startswith("{"):
            vinfo = json.loads(res_vod.stdout)
            if vinfo.get("entries"):
                vinfo = vinfo["entries"][0]
            raw_id = str(vinfo.get("id") or "").lstrip("v")
            if raw_id:
                live_stat = str(vinfo.get("live_status") or "").lower()
                status = "RECORDING" if (vinfo.get("is_live") or live_stat == "is_live") else "RECORDED"
                vod = {
                    "id": raw_id,
                    "url": vinfo.get("webpage_url") or f"https://www.twitch.tv/videos/{raw_id}",
                    "title": str(vinfo.get("title") or "Запись стрима"),
                    "duration": int(vinfo.get("duration") or 0),
                    "status": status,
                    "created_at": "",
                }
    except Exception:
        pass

    return {
        "ok": True,
        "source": "yt-dlp",
        "channel": channel,
        "display_name": channel,
        "is_live": is_live,
        "stream_id": "",
        "stream_title": stream_title,
        "stream_started_at": "",
        "viewers_count": 0,
        "game_name": "",
        "vod": vod,
    }


def check_twitch_status(channel: str = "t2x2") -> dict:
    """Проверяет статус канала и последнего VOD через Twitch GQL (с фоллбэком на yt-dlp)."""
    try:
        return check_twitch_status_gql(channel)
    except Exception as gql_err:
        print(f"[Twitch GQL warning] {gql_err}; trying yt-dlp fallback...", file=sys.stderr)
        return check_twitch_status_ytdlp_fallback(channel)


def _find_processor_template() -> Path:
    candidates = [
        BASE_DIR / "processor_template.py",
        BASE_DIR / "cloud_kaggle" / "kernel" / "processor.py",
        BASE_DIR.parent / "cloud_kaggle" / "kernel" / "processor.py",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "Не найден шаблон processor_template.py или cloud_kaggle/kernel/processor.py"
    )


def _render_processor_code(
    template_code: str,
    task_config: dict,
    bot_token: str = "",
    chat_id: int = 0,
) -> str:
    code = template_code

    if "__TELEGRAM_BOT_TOKEN__" in code:
        code = code.replace("__TELEGRAM_BOT_TOKEN__", bot_token or "")
    elif bot_token:
        code = re.sub(
            r'^BOT_TOKEN\s*=.*$',
            f'BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or {repr(bot_token)}',
            code,
            count=1,
            flags=re.MULTILINE,
        )

    if "__TELEGRAM_CHAT_ID__" in code:
        code = code.replace("__TELEGRAM_CHAT_ID__", str(int(chat_id or 0)))
    elif chat_id:
        code = re.sub(
            r'^CHAT_ID\s*=.*$',
            f'CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID") or {int(chat_id)})',
            code,
            count=1,
            flags=re.MULTILINE,
        )

    task_literal = "TASK_CONFIG = " + repr(task_config)
    rendered, replaced = re.subn(
        r"TASK_CONFIG\s*=\s*\{.*?\}",
        lambda _: task_literal,
        code,
        count=1,
        flags=re.DOTALL,
    )
    if replaced != 1:
        raise RuntimeError("Не удалось подставить TASK_CONFIG в шаблон Kaggle processor.py")

    # Гарантируем валидный синтаксис ещё до отправки в Kaggle
    ast.parse(rendered)
    return rendered


def _get_kaggle_api(kaggle_token: str = ""):
    if kaggle_token:
        os.environ["KAGGLE_API_TOKEN"] = kaggle_token
    if not os.environ.get("KAGGLE_API_TOKEN"):
        for candidate in (
            Path.home() / ".kaggle" / "access_token",
            Path.home() / ".config" / "kaggle" / "access_token",
        ):
            if candidate.exists():
                tok = candidate.read_text(encoding="utf-8").strip()
                if tok:
                    os.environ["KAGGLE_API_TOKEN"] = tok
                    break
    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    return api


def get_kaggle_kernel_status(kernel_id: str = DEFAULT_KAGGLE_KERNEL, kaggle_token: str = "") -> dict:
    """Возвращает статус последнего запуска Kaggle ядра: COMPLETE / RUNNING / QUEUED / ERROR."""
    try:
        api = _get_kaggle_api(kaggle_token)
        res = api.kernels_status(kernel_id)
        status_str = str(getattr(res, "status", "") or res)
        failure_msg = str(getattr(res, "failureMessage", "") or "")
        # Нормализуем Enum-представление вида 'KernelWorkerStatus.RUNNING'
        short_status = status_str.split(".")[-1].strip().upper()
        return {
            "ok": True,
            "status": short_status,
            "raw_status": status_str,
            "failure_message": failure_msg,
        }
    except Exception as e:
        return {"ok": False, "status": "UNKNOWN", "error": str(e)}


def launch_kaggle_cloud_job(
    vod_url: str,
    mode: str = "both",
    exclude: str = None,
    interval_minutes: int = 30,
    auto_upload: bool = True,
    bot_token: str = "",
    chat_id: int = 0,
    kaggle_token: str = "",
    kernel_id: str = DEFAULT_KAGGLE_KERNEL,
    dataset_id: str = DEFAULT_KAGGLE_DATASET,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Собирает временную приватную конфигурацию Kaggle-ядра и запускает нарезку на Tesla T4."""
    try:
        template_path = _find_processor_template()
        template_code = template_path.read_text(encoding="utf-8")

        task_cfg = {
            "url": vod_url,
            "mode": mode,
            "interval_minutes": int(interval_minutes),
            "auto_upload": bool(auto_upload),
            "exclude": exclude,
        }
        rendered_code = _render_processor_code(
            template_code,
            task_config=task_cfg,
            bot_token=bot_token,
            chat_id=chat_id,
        )

        kernel_slug = kernel_id.split("/")[-1]
        meta = {
            "id": kernel_id,
            "title": kernel_slug,
            "code_file": "processor.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "is_gpu": True,
            "enable_gpu": True,
            "enable_internet": True,
            "dataset_sources": [dataset_id],
            "competition_sources": [],
            "kernel_sources": [],
        }

        if dry_run:
            return (
                True,
                f"[DRY-RUN] Готово к push в {kernel_id} (private=True, dataset={dataset_id}, mode={mode}, url={vod_url})",
            )

        with tempfile.TemporaryDirectory(prefix="kaggle_push_") as tmp_dir:
            tmp = Path(tmp_dir)
            (tmp / "processor.py").write_text(rendered_code, encoding="utf-8")
            (tmp / "kernel-metadata.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            api = _get_kaggle_api(kaggle_token)
            push_res = api.kernels_push(folder=str(tmp))
            ref = getattr(push_res, "ref", None) or kernel_id
            err = getattr(push_res, "error", None)
            if err:
                return False, f"Kaggle вернул ошибку: {err}"
            return True, f"🚀 Облачный GPU Kaggle ({ref}) успешно запущен! Результат и ссылки придут сюда автоматически."
    except Exception as e:
        return False, f"Ошибка запуска Kaggle API: {e}"


def build_vod_card_markup_dict(vod_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🔥 Всё сразу (HD + Shorts -> YouTube)", "callback_data": f"act_both:{vod_id}"}],
            [
                {"text": "🎬 Длинные (1080p60)", "callback_data": f"act_long:{vod_id}"},
                {"text": "📱 Shorts (9:16)", "callback_data": f"act_shorts:{vod_id}"},
            ],
            [
                {"text": "✂️ Вырезать таймкоды", "callback_data": f"act_cut:{vod_id}"},
                {"text": "⏭ Пропустить", "callback_data": f"act_skip:{vod_id}"},
            ],
        ]
    }


def _describe_mode(mode: str) -> str:
    return {
        "both": "Длинные (1080p60) + Shorts -> YouTube",
        "full": "Длинные (1080p60) + Shorts -> YouTube",
        "long": "Только Длинные (1080p60) -> YouTube",
        "shorts": "Только Shorts (9:16) -> YouTube",
        "cut_only": "Только нарезка без автозагрузки",
    }.get(mode, mode)


def _format_status_text(twitch: dict, state: dict, settings: dict) -> str:
    vod = twitch.get("vod")
    k_stat = get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
    if twitch.get("is_live"):
        live_line = f"🔴 В ЭФИРЕ: {twitch.get('stream_title') or 'Без названия'}"
        if twitch.get("game_name"):
            live_line += f" [{twitch['game_name']}]"
    else:
        live_line = "⚫ Стример сейчас оффлайн"

    vod_line = "нет данных"
    if vod:
        vod_line = (
            f"#{vod['id']} | {vod['status']} | {format_duration(vod['duration'])}\n"
            f"   {vod['url']}"
        )

    return (
        f"📡 Статус наблюдателя 24/7 ({settings['channel']})\n\n"
        f"{live_line}\n"
        f"🎞 Последний VOD: {vod_line}\n"
        f"🖥 Kaggle GPU ({settings['kernel_id']}): {k_stat.get('status', 'UNKNOWN')}\n"
        f"🤖 Мониторинг: {'ВКЛ' if state.get('monitoring_enabled', True) else 'ВЫКЛ'}\n"
        f"⚡ Автозапуск Kaggle после стрима: {'ВКЛ' if settings['auto_start'] else 'ВЫКЛ'} ({_describe_mode(settings['auto_mode'])})\n"
        f"🆔 Обработанный VOD: {state.get('last_vod_id') or 'нет'}"
    )


def handle_telegram_command_text(text: str, state: dict, settings: dict, dry_run: bool = False) -> str:
    """Единый обработчик команд (/status, /check, /auto_on, /auto_off, /run, /kaggle, URL)."""
    cmd = (text or "").strip()
    low = cmd.lower().split()[0] if cmd else ""
    channel = settings["channel"]

    if low.startswith("/start") or low.startswith("/help"):
        return (
            "🤖 Автономный менеджер нарезки T2x2 (24/7 Cloud)\n\n"
            "• Когда начнутся/кончатся стримы на Twitch — бот сам напишет и запустит Kaggle Tesla T4.\n"
            "• Команды:\n"
            "  /status — статус Twitch, VOD и Kaggle\n"
            "  /check — внеочередная проверка стрима сейчас\n"
            "  /run — принудительно запустить Kaggle на последний VOD\n"
            "  /auto — переключить автозапуск после окончания стрима\n"
            "  /kaggle — статус облачного GPU-ядра\n"
            "  Или просто пришлите ссылку https://www.twitch.tv/videos/..."
        )

    if low.startswith("/auto_on"):
        state["auto_start"] = True
        return "✅ Автозапуск Kaggle после окончания стрима ВКЛЮЧЁН (режим: both)."
    if low.startswith("/auto_off"):
        state["auto_start"] = False
        return "⏸ Автозапуск выключен: после стрима буду присылать карточку с кнопками."
    if low.startswith("/auto"):
        new_val = not bool(state.get("auto_start", True))
        state["auto_start"] = new_val
        return (
            f"⚡ Автозапуск Kaggle после стрима: {'ВКЛ' if new_val else 'ВЫКЛ'} "
            f"(режим: {_describe_mode(settings['auto_mode'])})."
        )

    if low.startswith("/kaggle"):
        ks = get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
        msg = f"🖥 Kaggle ({settings['kernel_id']}): {ks.get('status')}"
        if ks.get("failure_message"):
            msg += f"\n⚠️ Причина: {ks['failure_message']}"
        return msg

    if low.startswith("/status") or low.startswith("/check"):
        twitch = check_twitch_status(channel)
        return _format_status_text(twitch, state, settings)

    vod_match = re.search(r"(https?://(?:www\.)?twitch\.tv/videos/\d+)", cmd, re.IGNORECASE)
    if vod_match or low.startswith("/run"):
        if vod_match:
            target_url = vod_match.group(1)
            target_id = target_url.rstrip("/").split("/")[-1]
        else:
            twitch = check_twitch_status(channel)
            vod = twitch.get("vod")
            if not vod:
                return "❌ Не найден ни один архивный VOD на канале."
            target_url = vod["url"]
            target_id = vod["id"]

        ok, msg = launch_kaggle_cloud_job(
            vod_url=target_url,
            mode=settings["auto_mode"],
            interval_minutes=settings["interval_minutes"],
            auto_upload=settings["auto_upload"],
            bot_token=settings["bot_token"],
            chat_id=settings["chat_id"],
            kaggle_token=settings["kaggle_token"],
            kernel_id=settings["kernel_id"],
            dataset_id=settings["dataset_id"],
            dry_run=dry_run,
        )
        if ok and not dry_run:
            state["last_vod_id"] = target_id
            state["last_dispatched_vod_id"] = target_id
            state["last_dispatched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return msg

    return "Не понял команду. Доступно: /status, /check, /run, /auto, /kaggle или ссылка twitch.tv/videos/..."


def poll_telegram_updates(state: dict, settings: dict, dry_run: bool = False) -> None:
    """Лёгкая обработка накопленных команд/кнопок для serverless (--once) режима."""
    token = settings["bot_token"]
    auth_chat_id = settings["chat_id"]
    if not token or not auth_chat_id:
        return

    offset = int(state.get("update_offset") or 0)
    res = tg_api_call(token, "getUpdates", {"offset": offset, "timeout": 0, "limit": 20}, timeout=12)
    if not res.get("ok"):
        return

    for upd in res.get("result") or []:
        upd_id = int(upd.get("update_id") or 0)
        if upd_id >= offset:
            state["update_offset"] = upd_id + 1

        msg = upd.get("message")
        if msg:
            chat_id = int((msg.get("chat") or {}).get("id") or 0)
            if chat_id != auth_chat_id:
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            pending_excl_vod = state.get("wait_exclude_vod_id")
            if pending_excl_vod and not text.startswith("/"):
                state["wait_exclude_vod_id"] = ""
                ok, launch_msg = launch_kaggle_cloud_job(
                    vod_url=f"https://www.twitch.tv/videos/{pending_excl_vod}",
                    mode="both",
                    exclude=text,
                    interval_minutes=settings["interval_minutes"],
                    auto_upload=settings["auto_upload"],
                    bot_token=token,
                    chat_id=auth_chat_id,
                    kaggle_token=settings["kaggle_token"],
                    kernel_id=settings["kernel_id"],
                    dataset_id=settings["dataset_id"],
                    dry_run=dry_run,
                )
                if ok and not dry_run:
                    state["last_vod_id"] = pending_excl_vod
                    state["last_dispatched_vod_id"] = pending_excl_vod
                tg_send(token, auth_chat_id, f"✂️ Исключения `{text}` приняты!\n{launch_msg}")
                continue
            reply = handle_telegram_command_text(text, state, settings, dry_run=dry_run)
            if reply:
                tg_send(token, auth_chat_id, reply)
            continue

        cb = upd.get("callback_query")
        if cb:
            cb_id = cb.get("id")
            cb_msg = cb.get("message") or {}
            chat_id = int((cb_msg.get("chat") or {}).get("id") or 0)
            if chat_id != auth_chat_id:
                continue
            data = str(cb.get("data") or "")
            sep = ":" if ":" in data else ("|" if "|" in data else None)
            if sep:
                action, vod_id = data.split(sep, 1)
                if action == "act_skip":
                    state["last_vod_id"] = vod_id
                    state["pending_vod_id"] = ""
                    tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Пропущено"})
                    tg_send(token, auth_chat_id, f"⏭ VOD #{vod_id} пропущен.")
                elif action == "act_cut":
                    state["wait_exclude_vod_id"] = vod_id
                    tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb_id})
                    tg_send(
                        token,
                        auth_chat_id,
                        f"✏️ Введи таймкоды исключений для VOD #{vod_id} (например: 01:15:00-01:25:00):",
                    )
                elif action in ("act_both", "act_long", "act_shorts"):
                    mode_map = {
                        "act_both": "both",
                        "act_long": "full",
                        "act_shorts": "shorts",
                    }
                    mode = mode_map[action]
                    ok, launch_msg = launch_kaggle_cloud_job(
                        vod_url=f"https://www.twitch.tv/videos/{vod_id}",
                        mode=mode,
                        interval_minutes=settings["interval_minutes"],
                        auto_upload=settings["auto_upload"],
                        bot_token=token,
                        chat_id=auth_chat_id,
                        kaggle_token=settings["kaggle_token"],
                        kernel_id=settings["kernel_id"],
                        dataset_id=settings["dataset_id"],
                        dry_run=dry_run,
                    )
                    if ok and not dry_run:
                        state["last_vod_id"] = vod_id
                        state["last_dispatched_vod_id"] = vod_id
                        state["pending_vod_id"] = ""
                    tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Запущено!" if ok else "Ошибка"})
                    tg_send(token, auth_chat_id, f"🚀 Ручной запуск ({_describe_mode(mode)}):\n{launch_msg}")


def _dispatch_vod(vod: dict, state: dict, settings: dict, dry_run: bool = False) -> bool:
    """Запускает Kaggle на готовый VOD (с проверкой занятости) или отправляет карточку с кнопками."""
    token = settings["bot_token"]
    chat_id = settings["chat_id"]
    vod_id = vod["id"]
    vod_url = vod["url"]
    dur_str = format_duration(vod.get("duration"))
    title = vod.get("title") or "Запись стрима"

    if not settings["auto_start"]:
        state["last_vod_id"] = vod_id
        state["waiting_for_vod_finalize"] = False
        state["stream_end_notified"] = False
        tg_send(
            token,
            chat_id,
            (
                f"🎬 Запись стрима готова!\n"
                f"📺 {title}\n"
                f"⏱ Длительность: {dur_str}\n"
                f"🔗 {vod_url}\n\n"
                f"Выбери режим нарезки (автозапуск выключен):"
            ),
            reply_markup=build_vod_card_markup_dict(vod_id),
        )
        return True

    # Проверяем, не занят ли сейчас Kaggle прошлым стримом (если не dry_run)
    if not dry_run:
        kstat = get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
        if kstat.get("status") in ("RUNNING", "QUEUED"):
            if state.get("pending_vod_id") != vod_id:
                state["pending_vod_id"] = vod_id
                tg_send(
                    token,
                    chat_id,
                    (
                        f"⏳ Запись #{vod_id} ({dur_str}) готова, но Kaggle GPU сейчас занят "
                        f"({kstat['status']}).\nПоставил в очередь — запущу автоматически, как только освободится!"
                    ),
                )
            return False

    ok, launch_msg = launch_kaggle_cloud_job(
        vod_url=vod_url,
        mode=settings["auto_mode"],
        interval_minutes=settings["interval_minutes"],
        auto_upload=settings["auto_upload"],
        bot_token=token,
        chat_id=chat_id,
        kaggle_token=settings["kaggle_token"],
        kernel_id=settings["kernel_id"],
        dataset_id=settings["dataset_id"],
        dry_run=dry_run,
    )

    if ok:
        state["last_vod_id"] = vod_id
        state["last_dispatched_vod_id"] = vod_id
        state["last_dispatched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        state["pending_vod_id"] = ""
        state["waiting_for_vod_finalize"] = False
        state["stream_end_notified"] = False
        tg_send(
            token,
            chat_id,
            (
                f"🎬 Стрим завершился и сохранился на Twitch!\n"
                f"📺 {title}\n"
                f"⏱ Длительность: {dur_str}\n"
                f"🔗 {vod_url}\n\n"
                f"⚡ Режим: {_describe_mode(settings['auto_mode'])}\n"
                f"{launch_msg}"
            ),
        )
        return True

    state["pending_vod_id"] = vod_id
    tg_send(
        token,
        chat_id,
        f"❌ Не удалось автозапустить Kaggle для VOD #{vod_id}:\n{launch_msg}\nПовторю на следующем тике.",
    )
    return False


def process_tick(state: dict, settings: dict, twitch: dict = None, dry_run: bool = False) -> dict:
    """
    Один цикл проверки:
      1) Определяет начало стрима -> шлёт уведомление в Telegram
      2) Фиксирует активный VOD во время эфира
      3) При завершении эфира дожидается статуса RECORDED и запускает Kaggle GPU
    """
    if not state.get("monitoring_enabled", True):
        return {"action": "monitoring_paused"}

    if twitch is None:
        twitch = check_twitch_status(settings["channel"])

    token = settings["bot_token"]
    chat_id = settings["chat_id"]
    was_live = bool(state.get("is_streamer_live"))
    is_live = bool(twitch.get("is_live"))
    vod = twitch.get("vod")

    # 1. Начался новый стрим
    if is_live and not was_live:
        state["is_streamer_live"] = True
        state["stream_end_notified"] = False
        state["waiting_for_vod_finalize"] = False
        state["live_stream_id"] = twitch.get("stream_id") or ""
        state["live_stream_title"] = twitch.get("stream_title") or ""
        if vod and vod.get("status") == "RECORDING":
            state["live_vod_id"] = vod["id"]
        else:
            state["live_vod_id"] = ""
        state["initialized"] = True

        game_info = f"\n🎮 Категория: {twitch['game_name']}" if twitch.get("game_name") else ""
        auto_note = (
            f"🤖 Автозапуск Kaggle ({settings['auto_mode']}) сработает сразу после окончания."
            if settings["auto_start"]
            else "⏸ Автозапуск выключен — после конца пришлю кнопки."
        )
        tg_send(
            token,
            chat_id,
            (
                f"🔴 СТРИМ НАЧАЛСЯ! ({twitch.get('display_name') or settings['channel']})\n"
                f"📺 {twitch.get('stream_title') or 'Без названия'}"
                f"{game_info}\n"
                f"🔗 https://www.twitch.tv/{settings['channel']}\n\n"
                f"{auto_note}"
            ),
        )
        return {"action": "stream_started", "stream_title": state["live_stream_title"]}

    # 2. Стрим продолжает идти
    if is_live and was_live:
        if vod and vod.get("status") == "RECORDING":
            state["live_vod_id"] = vod["id"]
        state["initialized"] = True
        return {"action": "still_live", "live_vod_id": state.get("live_vod_id")}

    # 3. Оффлайн (либо только что закончился, либо дожидаемся RECORDED, либо отложенный VOD)
    if was_live and not is_live:
        state["is_streamer_live"] = False
        state["waiting_for_vod_finalize"] = True
        if not state.get("stream_end_notified"):
            state["stream_end_notified"] = True
            # Если VOD ещё финализируется (RECORDING или не обновился), предупреждаем пользователя
            vod_ready_now = (
                vod
                and vod.get("status") == "RECORDED"
                and vod["id"] != state.get("last_vod_id")
            )
            if not vod_ready_now:
                tg_send(
                    token,
                    chat_id,
                    (
                        f"⏹ Стрим {settings['channel']} закончился!\n"
                        f"⏳ Жду, пока Twitch досохранит запись (RECORDED), "
                        f"и сразу {'запущу Kaggle GPU' if settings['auto_start'] else 'пришлю карточку'}..."
                    ),
                )

    # Первая инициализация в чистом облачном репозитории (чтобы не запускать старый VOD случайно)
    if not state.get("initialized") and not state.get("last_vod_id") and not state.get("waiting_for_vod_finalize"):
        state["initialized"] = True
        if vod and vod.get("id"):
            state["last_vod_id"] = vod["id"]
        tg_send(
            token,
            chat_id,
            (
                f"🤖 Облачный наблюдатель 24/7 запущен для {settings['channel']}!\n"
                f"📌 Зафиксировал текущий последний VOD #{vod['id'] if vod else 'нет'}, "
                f"чтобы не перенарезать старое.\n"
                f"🔴 Буду писать о каждом новом стриме и запускать Kaggle Tesla T4 автоматически!\n"
                f"💡 Для ручного запуска последнего VOD прямо сейчас напиши /run"
            ),
        )
        return {"action": "initialized", "last_vod_id": state.get("last_vod_id")}

    state["initialized"] = True

    # Если есть отложенный pending_vod_id (Kaggle раньше был занят) или новый RECORDED VOD
    if vod and vod.get("status") == "RECORDED":
        vod_id = vod["id"]
        already_handled = (
            vod_id == state.get("last_vod_id")
            or vod_id == state.get("last_dispatched_vod_id")
        )
        is_pending = (vod_id == state.get("pending_vod_id"))

        if is_pending or not already_handled:
            dur = int(vod.get("duration") or 0)
            if dur < MIN_VOD_DURATION_SEC:
                had_wait = bool(state.get("waiting_for_vod_finalize"))
                state["last_vod_id"] = vod_id
                state["waiting_for_vod_finalize"] = False
                state["stream_end_notified"] = False
                state["pending_vod_id"] = ""
                if had_wait:
                    tg_send(
                        token,
                        chat_id,
                        f"⚠️ Запись #{vod_id} длилась всего {format_duration(dur)} (<3 мин) — пропускаю.",
                    )
                return {"action": "skipped_short_vod", "vod_id": vod_id, "duration": dur}

            dispatched = _dispatch_vod(vod, state, settings, dry_run=dry_run)
            return {
                "action": "dispatched" if dispatched else "queued_waiting_kaggle",
                "vod_id": vod_id,
                "mode": settings["auto_mode"],
            }

    return {
        "action": "waiting_vod_finalize" if state.get("waiting_for_vod_finalize") else "idle_offline",
        "latest_vod_status": vod.get("status") if vod else None,
    }


def run_once(
    state_file: str = None,
    dry_run: bool = False,
    skip_tg_poll: bool = False,
) -> dict:
    state_path = resolve_state_file(state_file)
    state = load_state(state_path)
    settings = load_secrets_and_settings(state)

    if not skip_tg_poll and not dry_run:
        poll_telegram_updates(state, settings, dry_run=dry_run)
        # Настройки могли измениться командой (/auto_on, /auto_off)
        settings = load_secrets_and_settings(state)

    result = process_tick(state, settings, dry_run=dry_run)
    save_state(state, state_path)
    return result


def run_daemon(state_file: str = None, dry_run: bool = False) -> None:
    print("🚀 Запуск 24/7 облачного демона Twitch -> Telegram -> Kaggle...")
    while True:
        try:
            state_path = resolve_state_file(state_file)
            state = load_state(state_path)
            settings = load_secrets_and_settings(state)
            poll_telegram_updates(state, settings, dry_run=dry_run)
            settings = load_secrets_and_settings(state)
            res = process_tick(state, settings, dry_run=dry_run)
            save_state(state, state_path)
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {res}")
            sleep_sec = 45 if state.get("waiting_for_vod_finalize") else settings["check_interval_seconds"]
        except KeyboardInterrupt:
            print("Остановлено пользователем.")
            break
        except Exception as e:
            print(f"[Daemon error] {e}", file=sys.stderr)
            sleep_sec = 60
        time.sleep(sleep_sec)


def main():
    parser = argparse.ArgumentParser(description="24/7 Twitch -> Telegram -> Kaggle Watcher")
    parser.add_argument("--once", action="store_true", help="Выполнить 1 тик (для GitHub Actions Cron)")
    parser.add_argument("--daemon", action="store_true", help="Запустить бесконечный цикл опроса (для GCP/VPS)")
    parser.add_argument("--state-file", type=str, default=None, help="Путь к bot_state.json или bot_config.json")
    parser.add_argument("--dry-run", action="store_true", help="Проверка без реального push в Kaggle")
    parser.add_argument("--skip-tg-poll", action="store_true", help="Не опрашивать Telegram getUpdates в --once")
    args = parser.parse_args()

    if args.daemon:
        run_daemon(state_file=args.state_file, dry_run=args.dry_run)
    else:
        res = run_once(
            state_file=args.state_file,
            dry_run=args.dry_run,
            skip_tg_poll=args.skip_tg_poll,
        )
        print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
