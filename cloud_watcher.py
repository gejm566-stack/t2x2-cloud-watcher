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
DEFAULT_CHANNELS = ["t2x2", "5opka"]

CHANNEL_META = {
    "t2x2": {
        "profile": "t2x2",
        "brand": "T2x2 CLIPS",
        "display": "T2x2",
        "twitch_login": "t2x2",
    },
    "5opka": {
        "profile": "5opka",
        "brand": "5opka CLIPS",
        "display": "5opka",
        "twitch_login": "5opka",
    },
}


def normalize_channel(name: str) -> str:
    s = str(name or "t2x2").strip().lower()
    if s in ("5opka", "пятёрка", "пятерка", "5opkaclips"):
        return "5opka"
    if s in ("t2x2", "тоха", "тохи"):
        return "t2x2"
    return s or "t2x2"


def get_channel_meta(channel: str) -> dict:
    ch = normalize_channel(channel)
    if ch in CHANNEL_META:
        return dict(CHANNEL_META[ch])
    return {"profile": ch, "brand": f"{ch} CLIPS", "display": ch, "twitch_login": ch}


def _default_channel_state() -> dict:
    return {
        "auto_start": True,
        "auto_mode": "both",
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
        "initialized": False,
    }


def _make_default_state() -> dict:
    return {
        "streamer_channel": "t2x2",
        "streamer_channels": list(DEFAULT_CHANNELS),
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
        "channels": {},
        "pending_jobs": [],
        "vod_channels": {},
        "wait_exclude_channel": "",
        "update_offset": 0,
        "initialized": False,
    }


# Настройки по умолчанию для автономного облачного состояния
DEFAULT_STATE = _make_default_state()


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


def _normalize_channels_list(raw) -> list[str]:
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,;\s]+", raw) if p.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw if str(p).strip()]
    else:
        parts = []
    seen = []
    for p in parts:
        ch = normalize_channel(p)
        if ch and ch not in seen:
            seen.append(ch)
    return seen or list(DEFAULT_CHANNELS)


def ensure_channel_state(state: dict, channel: str, settings: dict = None) -> dict:
    ch = normalize_channel(channel)
    channels_map = state.setdefault("channels", {})
    if ch not in channels_map or not isinstance(channels_map.get(ch), dict):
        base = _default_channel_state()
        if ch == "t2x2" and (state.get("last_vod_id") or state.get("initialized") or state.get("pending_vod_id")):
            for k in list(base.keys()):
                if k in state and state[k] is not None:
                    base[k] = state[k]
        elif settings is not None:
            base["auto_start"] = bool(settings.get("auto_start", True))
            base["auto_mode"] = str(settings.get("auto_mode") or "both")
        channels_map[ch] = base
    else:
        dflt = _default_channel_state()
        for k, v in dflt.items():
            channels_map[ch].setdefault(k, v)
    return channels_map[ch]


def _sync_legacy_t2x2_top_level(state: dict) -> None:
    t2 = (state.get("channels") or {}).get("t2x2")
    if not isinstance(t2, dict):
        return
    for k in (
        "is_streamer_live",
        "stream_end_notified",
        "waiting_for_vod_finalize",
        "live_stream_id",
        "live_stream_title",
        "live_vod_id",
        "pending_vod_id",
        "last_vod_id",
        "last_dispatched_vod_id",
        "last_dispatched_at",
        "initialized",
    ):
        if k in t2:
            state[k] = t2[k]


def load_state(state_path: Path = None) -> dict:
    path = resolve_state_file(str(state_path) if state_path else None)
    data = _make_default_state()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                raw_channels = raw.get("channels")
                data.update(raw)
                data["channels"] = dict(raw_channels) if isinstance(raw_channels, dict) else {}
                data["pending_jobs"] = list(raw.get("pending_jobs") or []) if isinstance(raw.get("pending_jobs"), list) else []
                data["vod_channels"] = dict(raw.get("vod_channels") or {}) if isinstance(raw.get("vod_channels"), dict) else {}
        except Exception as e:
            print(f"[State] Не удалось прочитать {path}: {e}", file=sys.stderr)

    data["streamer_channels"] = _normalize_channels_list(data.get("streamer_channels"))
    for ch in data["streamer_channels"]:
        ensure_channel_state(data, ch)
    ensure_channel_state(data, "t2x2")
    return data


def save_state(state: dict, state_path: Path = None) -> Path:
    path = resolve_state_file(str(state_path) if state_path else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    _sync_legacy_t2x2_top_level(state)
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

    env_channels = os.environ.get("STREAMER_CHANNELS", "").strip()
    if env_channels:
        channels = _normalize_channels_list(env_channels)
    elif state.get("streamer_channels"):
        channels = _normalize_channels_list(state.get("streamer_channels"))
    else:
        single_env = os.environ.get("STREAMER_CHANNEL", "").strip()
        if single_env and normalize_channel(single_env) not in DEFAULT_CHANNELS:
            channels = _normalize_channels_list([single_env] + DEFAULT_CHANNELS)
        else:
            channels = list(DEFAULT_CHANNELS)

    channel = channels[0] if channels else "t2x2"
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
        "channels": channels,
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
    ch = normalize_channel(channel)
    try:
        return check_twitch_status_gql(ch)
    except Exception as gql_err:
        print(f"[Twitch GQL warning] {gql_err}; trying yt-dlp fallback...", file=sys.stderr)
        return check_twitch_status_ytdlp_fallback(ch)


def lookup_vod_channel(
    vod_id: str,
    state: dict = None,
    default: str = "t2x2",
    timeout: int = 10,
) -> str:
    """Определяет канал Twitch (t2x2 / 5opka) по ID VOD и кэширует результат в state['vod_channels']."""
    vid = str(vod_id or "").strip().rstrip("/").split("/")[-1].lstrip("vV")
    dflt = normalize_channel(default)
    if not vid or not vid.isdigit():
        return dflt

    if isinstance(state, dict):
        cached = (state.get("vod_channels") or {}).get(vid)
        if cached:
            return normalize_channel(cached)

    query = """
    query($id: ID!) {
      video(id: $id) {
        id
        owner { login }
      }
    }
    """
    try:
        body = json.dumps([{"query": query, "variables": {"id": vid}}]).encode("utf-8")
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
        video = (data[0].get("data") or {}).get("video") if isinstance(data, list) and data else None
        owner = (video or {}).get("owner") or {}
        login = owner.get("login")
        if login:
            detected = normalize_channel(login)
            if isinstance(state, dict):
                vmap = state.setdefault("vod_channels", {})
                vmap[vid] = detected
                if len(vmap) > 60:
                    for old_k in list(vmap.keys())[:-60]:
                        vmap.pop(old_k, None)
            return detected
    except Exception:
        pass
    return dflt


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
    profile: str = "t2x2",
) -> tuple[bool, str]:
    """Собирает временную приватную конфигурацию Kaggle-ядра и запускает нарезку на Tesla T4."""
    try:
        meta_ch = get_channel_meta(profile)
        prof = meta_ch["profile"]
        brand = meta_ch["brand"]

        template_path = _find_processor_template()
        template_code = template_path.read_text(encoding="utf-8")

        task_cfg = {
            "url": vod_url,
            "profile": prof,
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
                f"[DRY-RUN] Готово к push в {kernel_id} (profile={prof}, brand={brand}, dataset={dataset_id}, mode={mode}, url={vod_url})",
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
            return True, f"🚀 Облачный GPU Kaggle ({brand}, {ref}) успешно запущен! Результат и ссылки придут сюда автоматически."
    except Exception as e:
        return False, f"Ошибка запуска Kaggle API: {e}"


def build_vod_card_markup_dict(vod_id: str, channel: str = "t2x2") -> dict:
    ch = normalize_channel(channel)
    meta_ch = get_channel_meta(ch)
    other = "5opka" if ch == "t2x2" else "t2x2"
    other_brand = get_channel_meta(other)["brand"]
    return {
        "inline_keyboard": [
            [{"text": f"🔥 Всё сразу ({meta_ch['brand']} -> YouTube)", "callback_data": f"act_both:{vod_id}"}],
            [
                {"text": "🎬 Длинные (1080p60)", "callback_data": f"act_long:{vod_id}"},
                {"text": "📱 Shorts (9:16)", "callback_data": f"act_shorts:{vod_id}"},
            ],
            [
                {"text": "✂️ Вырезать таймкоды", "callback_data": f"act_cut:{vod_id}"},
                {"text": "⏭ Пропустить", "callback_data": f"act_skip:{vod_id}"},
            ],
            [
                {"text": f"🔄 Канал: {meta_ch['brand']} -> сменить на {other_brand}", "callback_data": f"act_swap:{vod_id}"},
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


def _channel_auto_start(channel: str, state: dict, settings: dict) -> bool:
    ch_state = ensure_channel_state(state, channel, settings)
    if "auto_start_override" in ch_state:
        return bool(ch_state["auto_start_override"])
    return bool(settings.get("auto_start", True))


def _remove_pending_job(state: dict, vod_id: str) -> None:
    vid = str(vod_id or "")
    jobs = state.get("pending_jobs")
    if isinstance(jobs, list) and vid:
        state["pending_jobs"] = [j for j in jobs if str(j.get("vod_id")) != vid]


def _enqueue_pending_job(
    state: dict,
    vod_url: str,
    vod_id: str,
    channel: str,
    mode: str = "both",
    duration: int = 0,
    title: str = "",
    exclude: str = None,
) -> bool:
    vid = str(vod_id or "")
    ch = normalize_channel(channel)
    meta = get_channel_meta(ch)
    jobs = state.setdefault("pending_jobs", [])
    for item in jobs:
        if str(item.get("vod_id")) == vid:
            return False
    jobs.append(
        {
            "vod_id": vid,
            "url": vod_url,
            "channel": ch,
            "profile": meta["profile"],
            "mode": mode,
            "duration": int(duration or 0),
            "title": title or "Запись стрима",
            "exclude": exclude,
            "queued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )
    return True


def _launch_or_queue_job(
    vod_url: str,
    vod_id: str,
    channel: str,
    state: dict,
    settings: dict,
    mode: str = None,
    exclude: str = None,
    duration: int = 0,
    title: str = "",
    dry_run: bool = False,
    force_queue: bool = False,
) -> tuple[bool, str, str]:
    """
    Пытается запустить Kaggle GPU для VOD. Если ядро занято -> ставит в очередь pending_jobs.
    Возвращает (dispatched_now, status_code['launched'|'queued'|'error'], message).
    """
    ch = normalize_channel(channel)
    meta = get_channel_meta(ch)
    ch_state = ensure_channel_state(state, ch, settings)
    use_mode = mode or ch_state.get("auto_mode") or settings["auto_mode"]
    vid = str(vod_id or vod_url.rstrip("/").split("/")[-1])

    state.setdefault("vod_channels", {})[vid] = ch

    if force_queue:
        _enqueue_pending_job(state, vod_url, vid, ch, use_mode, duration=duration, title=title, exclude=exclude)
        ch_state["pending_vod_id"] = vid
        return (
            False,
            "queued",
            f"⏳ Запись #{vid} ({meta['brand']}) поставлена в очередь Kaggle GPU — запустится сразу после текущей задачи!",
        )

    if not dry_run:
        kstat = get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
        if kstat.get("status") in ("RUNNING", "QUEUED"):
            newly_queued = _enqueue_pending_job(
                state, vod_url, vid, ch, use_mode, duration=duration, title=title, exclude=exclude
            )
            ch_state["pending_vod_id"] = vid
            msg = (
                f"⏳ Запись #{vid} ({meta['brand']}) готова, но Kaggle GPU сейчас занят "
                f"({kstat['status']}).\n"
                + ("Поставил в очередь — запущу автоматически, как только освободится!" if newly_queued else "Уже в очереди ожидания Kaggle.")
            )
            return False, "queued", msg

    ok, launch_msg = launch_kaggle_cloud_job(
        vod_url=vod_url,
        mode=use_mode,
        exclude=exclude,
        interval_minutes=settings["interval_minutes"],
        auto_upload=settings["auto_upload"],
        bot_token=settings["bot_token"],
        chat_id=settings["chat_id"],
        kaggle_token=settings["kaggle_token"],
        kernel_id=settings["kernel_id"],
        dataset_id=settings["dataset_id"],
        dry_run=dry_run,
        profile=meta["profile"],
    )
    if ok:
        if not dry_run:
            ch_state["last_vod_id"] = vid
            ch_state["last_dispatched_vod_id"] = vid
            ch_state["last_dispatched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            ch_state["pending_vod_id"] = ""
            ch_state["waiting_for_vod_finalize"] = False
            ch_state["stream_end_notified"] = False
            _remove_pending_job(state, vid)
        return True, "launched", launch_msg
    return False, "error", launch_msg


def _format_single_channel_block(ch: str, twitch: dict, state: dict, settings: dict) -> str:
    meta = get_channel_meta(ch)
    ch_state = ensure_channel_state(state, ch, settings)
    vod = (twitch or {}).get("vod")
    if (twitch or {}).get("is_live"):
        live_line = f"🔴 В ЭФИРЕ: {twitch.get('stream_title') or 'Без названия'}"
        if twitch.get("game_name"):
            live_line += f" [{twitch['game_name']}]"
    else:
        live_line = "⚫ Сейчас оффлайн"

    vod_line = "нет данных"
    if vod:
        vod_line = (
            f"#{vod['id']} | {vod['status']} | {format_duration(vod['duration'])}\n"
            f"   {vod['url']}"
        )

    auto_on = _channel_auto_start(ch, state, settings)
    return (
        f"👤 {meta['display']} ({meta['brand']}):\n"
        f"  {live_line}\n"
        f"  🎞 Последний VOD: {vod_line}\n"
        f"  ⚡ Автозапуск: {'ВКЛ' if auto_on else 'ВЫКЛ'} | Обработан: {ch_state.get('last_vod_id') or 'нет'}"
    )


def _format_status_text(twitch_data: dict, state: dict, settings: dict) -> str:
    k_stat = get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
    # Обратная совместимость: если передан одиночный twitch dict
    if isinstance(twitch_data, dict) and ("is_live" in twitch_data or "vod" in twitch_data):
        ch = normalize_channel(twitch_data.get("channel") or settings["channel"])
        per_channel = {ch: twitch_data}
    elif isinstance(twitch_data, dict):
        per_channel = twitch_data
    else:
        per_channel = {}

    blocks = [
        _format_single_channel_block(ch, tdata, state, settings)
        for ch, tdata in per_channel.items()
    ]
    queue = state.get("pending_jobs") or []
    queue_line = f"⏳ В очереди Kaggle: {len(queue)} шт." if queue else "⏳ В очереди Kaggle: пусто"
    channels_label = ", ".join(per_channel.keys()) or settings["channel"]

    return (
        f"📡 Статус наблюдателя 24/7 ({channels_label})\n\n"
        + "\n\n".join(blocks)
        + "\n\n"
        + f"🖥 Kaggle GPU ({settings['kernel_id']}): {k_stat.get('status', 'UNKNOWN')}\n"
        + f"🤖 Мониторинг: {'ВКЛ' if state.get('monitoring_enabled', True) else 'ВЫКЛ'} | "
        + f"Режим: {_describe_mode(settings['auto_mode'])}\n"
        + queue_line
    )


def _parse_channel_arg(cmd_token: str, rest_tokens: list[str], valid_channels: list[str]) -> str | None:
    if "_" in cmd_token:
        suffix = cmd_token.split("_", 1)[1].strip()
        if suffix in ("t2x2", "5opka"):
            return normalize_channel(suffix)
    for tok in rest_tokens:
        clean = tok.strip().lower()
        if clean in ("t2x2", "тоха", "5opka", "пятерка", "пятёрка") or clean in valid_channels:
            return normalize_channel(clean)
    return None


def _handle_telegram_command_with_markup(
    text: str,
    state: dict,
    settings: dict,
    dry_run: bool = False,
) -> tuple[str, dict | None]:
    """Обработчик текстовых команд Telegram с опциональной inline-клавиатурой."""
    cmd = (text or "").strip()
    parts = cmd.split()
    raw_head = parts[0].lower() if parts else ""
    head = raw_head.split("@")[0] if raw_head else ""
    rest = parts[1:] if len(parts) > 1 else []
    channels = settings.get("channels") or list(DEFAULT_CHANNELS)

    if head in ("/start", "/help"):
        return (
            "🤖 Автономный менеджер нарезки T2x2 & 5opka (24/7 Cloud)\n\n"
            "• Бот одновременно следит за **t2x2** (T2x2 CLIPS) и **5opka** (5opka CLIPS) на Twitch и сам запускает Kaggle Tesla T4.\n"
            "• Команды:\n"
            "  /status — статус обоих каналов, VOD и Kaggle\n"
            "  /check_t2x2 или /check_5opka — проверить конкретный канал\n"
            "  /run — выбрать канал и запустить последний VOD\n"
            "  /run_t2x2 | /run_5opka — сразу запустить последний VOD нужного канала\n"
            "  /auto [t2x2|5opka] — переключить автозапуск (общий или по каналу)\n"
            "  /kaggle — статус облачного GPU-ядра и очереди\n"
            "  Или просто пришлите ссылку https://www.twitch.tv/videos/... (канал определится автоматически)",
            None,
        )

    if head.startswith("/auto"):
        target_ch = _parse_channel_arg(head, rest, channels)
        action_channels = [target_ch] if target_ch else channels

        if head.startswith("/auto_on"):
            new_val = True
        elif head.startswith("/auto_off"):
            new_val = False
        else:
            curr = any(_channel_auto_start(ch, state, settings) for ch in action_channels)
            new_val = not curr

        for ch in action_channels:
            ch_state = ensure_channel_state(state, ch, settings)
            ch_state["auto_start"] = new_val
            ch_state["auto_start_override"] = new_val
        if not target_ch:
            state["auto_start"] = new_val

        label = get_channel_meta(target_ch)["brand"] if target_ch else "всех каналов (T2x2 + 5opka)"
        status_str = "ВКЛЮЧЁН" if new_val else "ВЫКЛЮЧЕН"
        hint = "будет автозапуск Kaggle." if new_val else "после стрима пришлю карточку с кнопками."
        return (
            f"⚡ Автозапуск для {label}: {status_str} — {hint}",
            None,
        )

    if head.startswith("/kaggle"):
        ks = get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
        queue = state.get("pending_jobs") or []
        msg = f"🖥 Kaggle ({settings['kernel_id']}): {ks.get('status')}"
        if queue:
            q_desc = ", ".join(f"#{j.get('vod_id')} ({get_channel_meta(j.get('channel', 't2x2'))['brand']})" for j in queue)
            msg += f"\n⏳ В очереди ({len(queue)}): {q_desc}"
        if ks.get("failure_message"):
            msg += f"\n⚠️ Причина: {ks['failure_message']}"
        return msg, None

    if head.startswith("/status") or head.startswith("/check"):
        target_ch = _parse_channel_arg(head, rest, channels)
        check_list = [target_ch] if target_ch else channels
        twitch_map = {}
        for ch in check_list:
            try:
                twitch_map[ch] = check_twitch_status(ch)
            except Exception as e:
                twitch_map[ch] = {"ok": False, "channel": ch, "is_live": False, "vod": None, "error": str(e)}
        return _format_status_text(twitch_map, state, settings), None

    vod_match = re.search(r"(https?://(?:www\.)?twitch\.tv/videos/\d+)", cmd, re.IGNORECASE)
    if vod_match:
        target_url = vod_match.group(1)
        target_id = target_url.rstrip("/").split("/")[-1]
        explicit_ch = _parse_channel_arg(head, rest, channels)
        detected_ch = explicit_ch or lookup_vod_channel(target_id, state, default=settings["channel"])
        _, _, msg = _launch_or_queue_job(
            vod_url=target_url,
            vod_id=target_id,
            channel=detected_ch,
            state=state,
            settings=settings,
            mode=settings["auto_mode"],
            dry_run=dry_run,
        )
        return msg, None

    if head.startswith("/run"):
        target_ch = _parse_channel_arg(head, rest, channels)
        if not target_ch:
            forced = os.environ.get("INPUT_FORCE_CHANNEL", "").strip()
            if forced:
                target_ch = normalize_channel(forced)
        if not target_ch:
            kb = {
                "inline_keyboard": [
                    [{"text": "🚀 Последний VOD T2x2 (T2x2 CLIPS)", "callback_data": "act_runch:t2x2"}],
                    [{"text": "🚀 Последний VOD 5opka (5opka CLIPS)", "callback_data": "act_runch:5opka"}],
                ]
            }
            return "👇 Выбери канал, чей последний VOD запустить на Kaggle Tesla T4:", kb

        meta = get_channel_meta(target_ch)
        twitch = check_twitch_status(target_ch)
        vod = twitch.get("vod")
        if not vod:
            return f"❌ Не найден ни один архивный VOD на канале {meta['display']}.", None
        _, _, msg = _launch_or_queue_job(
            vod_url=vod["url"],
            vod_id=vod["id"],
            channel=target_ch,
            state=state,
            settings=settings,
            mode=settings["auto_mode"],
            duration=vod.get("duration", 0),
            title=vod.get("title", ""),
            dry_run=dry_run,
        )
        return msg, None

    return (
        "Не понял команду. Доступно: /status, /check, /check_t2x2, /check_5opka, "
        "/run, /run_t2x2, /run_5opka, /auto, /kaggle или ссылка twitch.tv/videos/...",
        None,
    )


def handle_telegram_command_text(text: str, state: dict, settings: dict, dry_run: bool = False) -> str:
    """Единый текстовый обработчик команд (/status, /check, /auto, /run, /run_t2x2, /run_5opka, /kaggle, URL)."""
    reply, _ = _handle_telegram_command_with_markup(text, state, settings, dry_run=dry_run)
    return reply


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
                excl_ch = state.get("wait_exclude_channel") or lookup_vod_channel(
                    pending_excl_vod, state, default=settings["channel"]
                )
                state["wait_exclude_vod_id"] = ""
                state["wait_exclude_channel"] = ""
                _, _, launch_msg = _launch_or_queue_job(
                    vod_url=f"https://www.twitch.tv/videos/{pending_excl_vod}",
                    vod_id=pending_excl_vod,
                    channel=excl_ch,
                    state=state,
                    settings=settings,
                    mode="both",
                    exclude=text,
                    dry_run=dry_run,
                )
                tg_send(token, auth_chat_id, f"✂️ Исключения `{text}` приняты!\n{launch_msg}")
                continue
            reply, markup = _handle_telegram_command_with_markup(text, state, settings, dry_run=dry_run)
            if reply:
                tg_send(token, auth_chat_id, reply, reply_markup=markup)
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
                action, payload_arg = data.split(sep, 1)
                if action == "act_runch":
                    target_ch = normalize_channel(payload_arg)
                    tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": f"Запуск {target_ch}..."})
                    reply, _ = _handle_telegram_command_with_markup(f"/run {target_ch}", state, settings, dry_run=dry_run)
                    tg_send(token, auth_chat_id, reply)
                elif action == "act_swap":
                    vod_id = payload_arg
                    curr_ch = lookup_vod_channel(vod_id, state, default=settings["channel"])
                    new_ch = "5opka" if curr_ch == "t2x2" else "t2x2"
                    state.setdefault("vod_channels", {})[vod_id] = new_ch
                    new_brand = get_channel_meta(new_ch)["brand"]
                    tg_api_call(
                        token,
                        "answerCallbackQuery",
                        {"callback_query_id": cb_id, "text": f"Канал переключён на {new_brand}"},
                    )
                    if cb_msg.get("message_id"):
                        tg_api_call(
                            token,
                            "editMessageReplyMarkup",
                            {
                                "chat_id": auth_chat_id,
                                "message_id": cb_msg["message_id"],
                                "reply_markup": build_vod_card_markup_dict(vod_id, channel=new_ch),
                            },
                        )
                elif action == "act_skip":
                    vod_id = payload_arg
                    vod_ch = lookup_vod_channel(vod_id, state, default=settings["channel"])
                    ch_state = ensure_channel_state(state, vod_ch, settings)
                    ch_state["last_vod_id"] = vod_id
                    ch_state["pending_vod_id"] = ""
                    _remove_pending_job(state, vod_id)
                    tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Пропущено"})
                    tg_send(token, auth_chat_id, f"⏭ VOD #{vod_id} ({get_channel_meta(vod_ch)['brand']}) пропущен.")
                elif action == "act_cut":
                    vod_id = payload_arg
                    state["wait_exclude_vod_id"] = vod_id
                    state["wait_exclude_channel"] = lookup_vod_channel(vod_id, state, default=settings["channel"])
                    tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb_id})
                    tg_send(
                        token,
                        auth_chat_id,
                        f"✏️ Введи таймкоды исключений для VOD #{vod_id} (например: 01:15:00-01:25:00):",
                    )
                elif action in ("act_both", "act_long", "act_shorts"):
                    vod_id = payload_arg
                    vod_ch = lookup_vod_channel(vod_id, state, default=settings["channel"])
                    mode_map = {
                        "act_both": "both",
                        "act_long": "full",
                        "act_shorts": "shorts",
                    }
                    mode = mode_map[action]
                    ok, st_code, launch_msg = _launch_or_queue_job(
                        vod_url=f"https://www.twitch.tv/videos/{vod_id}",
                        vod_id=vod_id,
                        channel=vod_ch,
                        state=state,
                        settings=settings,
                        mode=mode,
                        dry_run=dry_run,
                    )
                    ans_text = "Запущено!" if ok else ("В очереди!" if st_code == "queued" else "Ошибка")
                    tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": ans_text})
                    brand = get_channel_meta(vod_ch)["brand"]
                    tg_send(token, auth_chat_id, f"🚀 Ручной запуск [{brand}] ({_describe_mode(mode)}):\n{launch_msg}")


def _dispatch_vod(
    vod: dict,
    state: dict,
    settings: dict,
    dry_run: bool = False,
    channel: str = None,
    force_queue: bool = False,
) -> bool:
    """Запускает Kaggle на готовый VOD (с очередью при занятости) или отправляет карточку с кнопками."""
    token = settings["bot_token"]
    chat_id = settings["chat_id"]
    ch = normalize_channel(channel or vod.get("channel") or settings["channel"])
    meta = get_channel_meta(ch)
    ch_state = ensure_channel_state(state, ch, settings)
    auto_on = _channel_auto_start(ch, state, settings)

    vod_id = str(vod["id"])
    vod_url = vod["url"]
    dur = int(vod.get("duration") or 0)
    dur_str = format_duration(dur)
    title = vod.get("title") or "Запись стрима"
    state.setdefault("vod_channels", {})[vod_id] = ch

    if not auto_on:
        ch_state["last_vod_id"] = vod_id
        ch_state["waiting_for_vod_finalize"] = False
        ch_state["stream_end_notified"] = False
        tg_send(
            token,
            chat_id,
            (
                f"🎬 Запись стрима готова! [{meta['brand']}]\n"
                f"📺 {title}\n"
                f"⏱ Длительность: {dur_str}\n"
                f"🔗 {vod_url}\n\n"
                f"Выбери режим нарезки (автозапуск выключен):"
            ),
            reply_markup=build_vod_card_markup_dict(vod_id, channel=ch),
        )
        return True

    ok, status_code, msg = _launch_or_queue_job(
        vod_url=vod_url,
        vod_id=vod_id,
        channel=ch,
        state=state,
        settings=settings,
        mode=ch_state.get("auto_mode") or settings["auto_mode"],
        duration=dur,
        title=title,
        dry_run=dry_run,
        force_queue=force_queue,
    )

    if ok:
        ch_state["last_vod_id"] = vod_id
        ch_state["last_dispatched_vod_id"] = vod_id
        ch_state["last_dispatched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ch_state["pending_vod_id"] = ""
        ch_state["waiting_for_vod_finalize"] = False
        ch_state["stream_end_notified"] = False
        _remove_pending_job(state, vod_id)
        tg_send(
            token,
            chat_id,
            (
                f"🎬 Стрим {meta['display']} завершился и сохранился на Twitch! -> **{meta['brand']}**\n"
                f"📺 {title}\n"
                f"⏱ Длительность: {dur_str}\n"
                f"🔗 {vod_url}\n\n"
                f"⚡ Режим: {_describe_mode(settings['auto_mode'])}\n"
                f"{msg}"
            ),
        )
        return True

    if status_code == "queued":
        # Уведомляем в Telegram один раз при постановке в очередь
        tg_send(token, chat_id, msg)
        return False

    ch_state["pending_vod_id"] = vod_id
    _enqueue_pending_job(
        state,
        vod_url=vod_url,
        vod_id=vod_id,
        channel=ch,
        mode=ch_state.get("auto_mode") or settings["auto_mode"],
        duration=dur,
        title=title,
    )
    tg_send(
        token,
        chat_id,
        f"❌ Не удалось автозапустить Kaggle [{meta['brand']}] для VOD #{vod_id}:\n{msg}\nПовторю на следующем тике.",
    )
    return False


def _drain_pending_jobs(state: dict, settings: dict, dry_run: bool = False) -> dict | None:
    """Если в очереди pending_jobs есть отложенные задачи и Kaggle свободен — запускает следующую."""
    jobs = state.get("pending_jobs") or []
    if not jobs:
        return None

    if not dry_run:
        kstat = get_kaggle_kernel_status(settings["kernel_id"], settings["kaggle_token"])
        if kstat.get("status") in ("RUNNING", "QUEUED"):
            return None

    job = jobs[0]
    vod_id = str(job.get("vod_id") or "")
    vod_url = job.get("url") or f"https://www.twitch.tv/videos/{vod_id}"
    ch = normalize_channel(job.get("channel") or settings["channel"])
    meta = get_channel_meta(ch)
    ch_state = ensure_channel_state(state, ch, settings)
    mode = job.get("mode") or settings["auto_mode"]

    ok, launch_msg = launch_kaggle_cloud_job(
        vod_url=vod_url,
        mode=mode,
        exclude=job.get("exclude"),
        interval_minutes=settings["interval_minutes"],
        auto_upload=settings["auto_upload"],
        bot_token=settings["bot_token"],
        chat_id=settings["chat_id"],
        kaggle_token=settings["kaggle_token"],
        kernel_id=settings["kernel_id"],
        dataset_id=settings["dataset_id"],
        dry_run=dry_run,
        profile=job.get("profile") or meta["profile"],
    )
    if ok:
        state["pending_jobs"] = [j for j in jobs if str(j.get("vod_id")) != vod_id]
        ch_state["last_vod_id"] = vod_id
        ch_state["last_dispatched_vod_id"] = vod_id
        ch_state["last_dispatched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ch_state["pending_vod_id"] = ""
        ch_state["waiting_for_vod_finalize"] = False
        ch_state["stream_end_notified"] = False
        tg_send(
            settings["bot_token"],
            settings["chat_id"],
            (
                f"🚀 Очередь Kaggle освободилась! Запускаю отложенную нарезку [{meta['brand']}]:\n"
                f"🎬 VOD #{vod_id} ({format_duration(job.get('duration'))})\n"
                f"🔗 {vod_url}\n{launch_msg}"
            ),
        )
        return {"action": "dispatched_from_queue", "channel": ch, "vod_id": vod_id, "mode": mode}
    return None


def _process_single_channel_tick(
    channel: str,
    twitch: dict,
    state: dict,
    settings: dict,
    dry_run: bool = False,
    kaggle_busy: bool = False,
) -> dict:
    ch = normalize_channel(channel)
    meta = get_channel_meta(ch)
    ch_state = ensure_channel_state(state, ch, settings)
    token = settings["bot_token"]
    chat_id = settings["chat_id"]
    auto_on = _channel_auto_start(ch, state, settings)

    was_live = bool(ch_state.get("is_streamer_live"))
    is_live = bool(twitch.get("is_live"))
    vod = twitch.get("vod")
    if vod and vod.get("id"):
        state.setdefault("vod_channels", {})[str(vod["id"])] = ch

    # 1. Начался новый стрим
    if is_live and not was_live:
        ch_state["is_streamer_live"] = True
        ch_state["stream_end_notified"] = False
        ch_state["waiting_for_vod_finalize"] = False
        ch_state["live_stream_id"] = twitch.get("stream_id") or ""
        ch_state["live_stream_title"] = twitch.get("stream_title") or ""
        if vod and vod.get("status") == "RECORDING":
            ch_state["live_vod_id"] = vod["id"]
        else:
            ch_state["live_vod_id"] = ""
        ch_state["initialized"] = True

        game_info = f"\n🎮 Категория: {twitch['game_name']}" if twitch.get("game_name") else ""
        auto_note = (
            f"🤖 Автозапуск Kaggle ({settings['auto_mode']}) для **{meta['brand']}** сработает сразу после окончания."
            if auto_on
            else "⏸ Автозапуск выключен — после конца пришлю кнопки."
        )
        tg_send(
            token,
            chat_id,
            (
                f"🔴 СТРИМ НАЧАЛСЯ! {twitch.get('display_name') or meta['display']} ({meta['brand']})\n"
                f"📺 {twitch.get('stream_title') or 'Без названия'}"
                f"{game_info}\n"
                f"🔗 https://www.twitch.tv/{ch}\n\n"
                f"{auto_note}"
            ),
        )
        return {"action": "stream_started", "channel": ch, "stream_title": ch_state["live_stream_title"]}

    # 2. Стрим продолжает идти
    if is_live and was_live:
        if vod and vod.get("status") == "RECORDING":
            ch_state["live_vod_id"] = vod["id"]
        ch_state["initialized"] = True
        return {"action": "still_live", "channel": ch, "live_vod_id": ch_state.get("live_vod_id")}

    # 3. Оффлайн (либо только что закончился, либо дожидаемся RECORDED, либо отложенный VOD)
    if was_live and not is_live:
        ch_state["is_streamer_live"] = False
        ch_state["waiting_for_vod_finalize"] = True
        if not ch_state.get("stream_end_notified"):
            ch_state["stream_end_notified"] = True
            vod_ready_now = (
                vod
                and vod.get("status") == "RECORDED"
                and str(vod["id"]) != str(ch_state.get("last_vod_id"))
            )
            if not vod_ready_now:
                tg_send(
                    token,
                    chat_id,
                    (
                        f"⏹ Стрим {meta['display']} ({meta['brand']}) закончился!\n"
                        f"⏳ Жду, пока Twitch досохранит запись (RECORDED), "
                        f"и сразу {'запущу Kaggle GPU' if auto_on else 'пришлю карточку'}..."
                    ),
                )

    # Первая инициализация канала в облачном состоянии (чтобы не запускать старый VOD случайно)
    if not ch_state.get("initialized") and not ch_state.get("last_vod_id") and not ch_state.get("waiting_for_vod_finalize"):
        ch_state["initialized"] = True
        if vod and vod.get("id"):
            ch_state["last_vod_id"] = str(vod["id"])
        tg_send(
            token,
            chat_id,
            (
                f"🤖 Облачный наблюдатель 24/7 запущен для **{meta['display']}** ({meta['brand']})!\n"
                f"📌 Зафиксировал текущий последний VOD #{vod['id'] if vod else 'нет'}, "
                f"чтобы не перенарезать старое.\n"
                f"🔴 Буду писать о каждом новом стриме и запускать Kaggle Tesla T4 автоматически!\n"
                f"💡 Для ручного запуска последнего VOD прямо сейчас напиши /run_{ch}"
            ),
        )
        return {"action": "initialized", "channel": ch, "last_vod_id": ch_state.get("last_vod_id")}

    ch_state["initialized"] = True

    # Если есть отложенный pending_vod_id или новый RECORDED VOD
    if vod and vod.get("status") == "RECORDED":
        vod_id = str(vod["id"])
        already_handled = (
            vod_id == str(ch_state.get("last_vod_id"))
            or vod_id == str(ch_state.get("last_dispatched_vod_id"))
        )
        is_pending = (vod_id == str(ch_state.get("pending_vod_id")))

        if is_pending or not already_handled:
            dur = int(vod.get("duration") or 0)
            if dur < MIN_VOD_DURATION_SEC:
                had_wait = bool(ch_state.get("waiting_for_vod_finalize"))
                ch_state["last_vod_id"] = vod_id
                ch_state["waiting_for_vod_finalize"] = False
                ch_state["stream_end_notified"] = False
                ch_state["pending_vod_id"] = ""
                _remove_pending_job(state, vod_id)
                if had_wait:
                    tg_send(
                        token,
                        chat_id,
                        f"⚠️ Запись #{vod_id} ({meta['brand']}) длилась всего {format_duration(dur)} (<3 мин) — пропускаю.",
                    )
                return {"action": "skipped_short_vod", "channel": ch, "vod_id": vod_id, "duration": dur}

            dispatched = _dispatch_vod(
                vod,
                state,
                settings,
                dry_run=dry_run,
                channel=ch,
                force_queue=kaggle_busy,
            )
            return {
                "action": "dispatched" if dispatched else "queued_waiting_kaggle",
                "channel": ch,
                "vod_id": vod_id,
                "mode": settings["auto_mode"],
            }

    return {
        "action": "waiting_vod_finalize" if ch_state.get("waiting_for_vod_finalize") else "idle_offline",
        "channel": ch,
        "latest_vod_status": vod.get("status") if vod else None,
    }


def process_tick(state: dict, settings: dict, twitch: dict = None, dry_run: bool = False) -> dict:
    """
    Один цикл проверки всех отслеживаемых каналов (t2x2, 5opka) + запуск очереди pending_jobs:
      1) Дренаж очереди Kaggle, если GPU освободился
      2) По каждому каналу: старт стрима / активный VOD / завершение RECORDED -> запуск или очередь
    """
    if not state.get("monitoring_enabled", True):
        return {"action": "monitoring_paused"}

    queued_result = _drain_pending_jobs(state, settings, dry_run=dry_run)
    dispatched_this_tick = bool(queued_result)

    # Обратная совместимость: одиночный twitch dict против словаря {channel: twitch_dict}
    single_mode = False
    if isinstance(twitch, dict) and ("is_live" in twitch or "vod" in twitch):
        ch_key = normalize_channel(twitch.get("channel") or settings["channel"])
        twitch_map = {ch_key: twitch}
        single_mode = True
    elif isinstance(twitch, dict):
        twitch_map = {normalize_channel(k): v for k, v in twitch.items()}
    else:
        twitch_map = {}
        for ch in settings.get("channels") or list(DEFAULT_CHANNELS):
            try:
                twitch_map[ch] = check_twitch_status(ch)
            except Exception as e:
                print(f"[Twitch check error] {ch}: {e}", file=sys.stderr)

    per_channel = {}
    for ch, tdata in twitch_map.items():
        res = _process_single_channel_tick(
            ch,
            tdata,
            state,
            settings,
            dry_run=dry_run,
            kaggle_busy=dispatched_this_tick,
        )
        per_channel[ch] = res
        if res.get("action") == "dispatched":
            dispatched_this_tick = True

    if single_mode and len(per_channel) == 1:
        only_res = dict(next(iter(per_channel.values())))
        if queued_result:
            only_res["queue_drained"] = queued_result
        only_res["pending_jobs_count"] = len(state.get("pending_jobs") or [])
        return only_res

    priority = (
        "dispatched_from_queue",
        "dispatched",
        "queued_waiting_kaggle",
        "stream_started",
        "waiting_vod_finalize",
        "still_live",
        "initialized",
        "skipped_short_vod",
        "idle_offline",
    )
    actions = ([queued_result["action"]] if queued_result else []) + [
        r.get("action", "") for r in per_channel.values()
    ]
    summary_action = next((p for p in priority if p in actions), "idle_offline")

    return {
        "action": summary_action,
        "channels": per_channel,
        "queue_drained": queued_result,
        "pending_jobs_count": len(state.get("pending_jobs") or []),
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
        # Настройки могли измениться командой (/auto_on, /auto_off, /auto)
        settings = load_secrets_and_settings(state)

    result = process_tick(state, settings, dry_run=dry_run)
    save_state(state, state_path)
    return result


def run_daemon(state_file: str = None, dry_run: bool = False) -> None:
    print("🚀 Запуск 24/7 облачного демона Twitch (T2x2 & 5opka) -> Telegram -> Kaggle...")
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
            any_waiting = any(
                bool((state.get("channels") or {}).get(ch, {}).get("waiting_for_vod_finalize"))
                for ch in (settings.get("channels") or [])
            )
            sleep_sec = 45 if (any_waiting or state.get("pending_jobs")) else settings["check_interval_seconds"]
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
