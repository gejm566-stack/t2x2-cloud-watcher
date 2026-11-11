#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kaggle Cloud Autonomous Runner for T2X2 CLIPS STUDIO.
Executes on Kaggle's Cloud GPU (NVIDIA Tesla T4).
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "__TELEGRAM_BOT_TOKEN__"
_RAW_CHAT_ID = "__TELEGRAM_CHAT_ID__"
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID") or (_RAW_CHAT_ID if _RAW_CHAT_ID.isdigit() else 0))

# Параметры текущей задачи (автоматически обновляются ботом перед запуском)
TASK_CONFIG = {
    "url": "https://www.twitch.tv/videos/2924895000",
    "profile": "t2x2",
    "mode": "both",
    "interval_minutes": 30,
    "auto_upload": True,
    "exclude": None
}

AI_MODEL_URLS = [
    ("EDSR_x3.pb", 30_000_000, "https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x3.pb"),
    ("FSRCNN_x3.pb", 30_000, "https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x3.pb"),
    ("gfpgan_1.4.onnx", 300_000_000, "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/gfpgan_1.4.onnx"),
]


def notify_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        payload = json.dumps({"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"Telegram notify error: {e}")


def check_internet() -> bool:
    """Проверяет доступ в интернет из контейнера Kaggle."""
    for test_url in ["https://api.telegram.org", "https://www.google.com", "https://pypi.org"]:
        try:
            req = urllib.request.Request(test_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8):
                return True
        except Exception as e:
            print(f"Internet probe to {test_url} failed: {e}")
    return False


def ensure_ai_models(working_dir: Path):
    """Скачивает AI-модели улучшения портретов на 1080p обложки (EDSR, FSRCNN, GFPGAN 1.4)."""
    models_dir = working_dir / "assets" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, min_sz, url in AI_MODEL_URLS:
        dst = models_dir / name
        if dst.exists() and dst.stat().st_size >= min_sz:
            continue
        print(f"📥 Скачивание AI-модели {name}...")
        try:
            urllib.request.urlretrieve(url, dst)
            print(f"  ✓ {name}: {dst.stat().st_size / 1024 / 1024:.1f} МБ")
        except Exception as e:
            print(f"  ⚠ Не удалось скачать {name}: {e}")


def main():
    url = TASK_CONFIG.get("url")
    profile = str(TASK_CONFIG.get("profile", "t2x2") or "t2x2").strip().lower()
    if profile not in ("t2x2", "5opka"):
        profile = "t2x2"
    brand = "5opka CLIPS" if profile == "5opka" else "T2x2 CLIPS"
    mode = TASK_CONFIG.get("mode", "both")
    interval = int(TASK_CONFIG.get("interval_minutes", 30))
    auto_upload = bool(TASK_CONFIG.get("auto_upload", True))

    os.environ["STUDIO_PROFILE"] = profile
    if profile == "5opka":
        os.environ["SKIP_INTRO"] = "1"

    print("=" * 70)
    print(f"🎬 {brand.upper()} STUDIO — KAGGLE CLOUD GPU RUNNER")
    print("=" * 70)
    print(f"🎯 Стрим ({brand}): {url}")
    print(f"⚙️ Режим: {mode} | Профиль: {profile}")

    if not check_internet():
        err_msg = (
            "🚨 ВНИМАНИЕ: В ядре Kaggle заблокирован доступ в интернет!\n\n"
            "Причина: аккаунт Kaggle требует подтверждения телефона.\n"
            "Перейдите на: https://www.kaggle.com/settings -> Phone verification"
        )
        print(err_msg)
        notify_telegram(err_msg)
        return

    notify_telegram(
        f"🚀 **Облачный GPU-сервер Kaggle запущен ({brand})!**\n\n"
        f"🎬 Стрим: {url}\n"
        f"⚙️ Режим: `{mode}` | Канал: **{brand}**\n"
        f"⚡ Видеокарта: NVIDIA Tesla T4 (16 ГБ)\n"
        f"⏳ Начинаю скачивание и нарезку в 1080p60 (без субтитров)..."
    )

    working_dir = Path("/kaggle/working/studio")
    working_dir.mkdir(parents=True, exist_ok=True)

    input_dir = Path("/kaggle/input")
    bundle_copied = False
    bundle_root = None
    for p in input_dir.rglob("pipeline_shorts.py"):
        bundle_root = p.parent
        break

    if bundle_root and bundle_root.exists():
        print(f"📦 Найден каталог студии: {bundle_root}")
        for item in bundle_root.iterdir():
            target = working_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        bundle_copied = True
    else:
        for z in input_dir.rglob("*.zip"):
            print(f"📦 Распаковка архива {z}...")
            with zipfile.ZipFile(z, "r") as zf:
                zf.extractall(working_dir)
            bundle_copied = True
            break

    if not bundle_copied:
        msg = "⚠ Датасет T2X2 CLIPS не найден в `/kaggle/input`!"
        print(msg)
        notify_telegram(msg)
        return

    os.chdir(working_dir)
    print(f"✅ Рабочая папка готова: {working_dir} (файлов: {len(list(working_dir.iterdir()))})")

    print("📦 Установка/проверка библиотек AI, видео и YouTube API...")
    pkgs = [
        "yt-dlp", "faster-whisper", "av<18", "mediapipe",
        "opencv-contrib-python-headless", "pillow", "numpy", "scenedetect",
        "google-genai", "nvidia-cublas-cu12", "nvidia-cudnn-cu12",
        "rembg", "pooch", "scipy",
        "google-api-python-client", "google-auth", "google-auth-oauthlib", "google-auth-httplib2",
    ]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pkgs], check=False)
    r_ort = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "onnxruntime-gpu"], check=False)
    if r_ort.returncode != 0:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "onnxruntime"], check=False)

    ensure_ai_models(working_dir)

    import site
    cuda_paths = []
    for sp in site.getsitepackages():
        for sub in ["nvidia/cublas/lib", "nvidia/cudnn/lib", "torch/lib"]:
            p = os.path.join(sp, sub)
            if os.path.exists(p):
                cuda_paths.append(p)
    if cuda_paths:
        curr_ld = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = ":".join(cuda_paths) + (":" + curr_ld if curr_ld else "")

    import re

    def _cleanup_vod_intermediates(vod_ws: Path) -> None:
        if not vod_ws.exists():
            return
        for pat in ("vod*", "audio*"):
            for fp in vod_ws.glob(pat):
                if fp.is_file():
                    fp.unlink(missing_ok=True)
        for sub in ("shorts/raw", "shorts/final", "shorts/subtitled", "long_blocks", "long_cuts"):
            d = vod_ws / sub
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

    url_list = [u.strip() for u in re.split(r"[\s,;]+", str(url or "")) if u.strip()] or [str(url)]
    proc_returncode = 0

    if mode == "shorts":
        ready_dir = working_dir / "ready"
        for idx, cur_url in enumerate(url_list, 1):
            before_names = {p.name for p in ready_dir.glob("*.mp4")} if ready_dir.exists() else set()
            m_vod = re.search(r"(\d{6,})", cur_url)
            vod_tag = m_vod.group(1) if m_vod else f"job{idx}"

            if len(url_list) > 1:
                notify_telegram(f"🎬 [{idx}/{len(url_list)}] Нарезаю Shorts ({brand}): {cur_url}")

            cmd = [
                sys.executable, "pipeline_shorts.py",
                "--url", cur_url,
                "--interval-minutes", str(interval),
                "--skip-subtitles",
                "--min-score", "6.5",
                "--max-clips", "15",
            ]
            if len(url_list) > 1:
                cmd.append("--skip-vision")

            print(f"\n🎬 [{idx}/{len(url_list)}] Запуск команды: {' '.join(cmd)}")
            cur_proc = subprocess.run(cmd, env=os.environ)
            if cur_proc.returncode != 0:
                proc_returncode = cur_proc.returncode

            if len(url_list) > 1 and ready_dir.exists():
                for mp4 in sorted(ready_dir.glob("*.mp4")):
                    if mp4.name in before_names or mp4.name.startswith(f"v{vod_tag}_"):
                        continue
                    old_stem = mp4.stem
                    new_stem = f"v{vod_tag}_{old_stem}"
                    mp4.rename(ready_dir / f"{new_stem}.mp4")
                    for mdir_name in ("metadata", "shorts_metadata"):
                        old_meta = working_dir / mdir_name / f"{old_stem}.json"
                        if old_meta.exists():
                            old_meta.rename(working_dir / mdir_name / f"{new_stem}.json")

            _cleanup_vod_intermediates(working_dir / "twitch_work" / f"v{vod_tag}")
    else:
        cmd = [sys.executable, "pipeline_long.py", "--url", url_list[0], "--interval-minutes", str(interval)]
        if mode == "both":
            cmd.append("--with-shorts")
        print(f"\n🎬 Запуск команды: {' '.join(cmd)}")
        proc = subprocess.run(cmd, env=os.environ)
        proc_returncode = proc.returncode
        twitch_work = working_dir / "twitch_work"
        if twitch_work.exists():
            for vod_ws in twitch_work.iterdir():
                if vod_ws.is_dir():
                    _cleanup_vod_intermediates(vod_ws)

    long_ready_dir = working_dir / "long_ready"
    ready_dir = working_dir / "ready"
    long_mp4s = sorted(long_ready_dir.glob("*.mp4")) if long_ready_dir.exists() else []
    short_mp4s = sorted(ready_dir.glob("*.mp4")) if ready_dir.exists() else []
    print(f"\n📦 Готово длинных роликов (long_ready): {len(long_mp4s)}, Shorts (ready): {len(short_mp4s)}")

    upload_ok = False
    if auto_upload and (long_mp4s or short_mp4s):
        print("\n🚀 ВЫГРУЗКА НА YOUTUBE ПО РАСПИСАНИЮ...")
        src_mode = "ready" if mode == "shorts" else ("long_ready" if mode in ("full", "long") else "both")
        u_cmd = [
            sys.executable, "youtube_api_uploader.py",
            "--profile", profile,
            "--source", src_mode,
            "--interval-minutes", str(interval),
            "--max-uploads", "25",
        ]
        ur = subprocess.run(u_cmd, env=os.environ)
        upload_ok = (ur.returncode == 0)

    status_line = "✅ Опубликованы на YouTube в отложенное расписание!" if upload_ok else "⚠ Нарезка выполнена (проверьте статус выгрузки на YouTube)."
    if proc_returncode != 0 and not long_mp4s and not short_mp4s:
        notify_telegram(
            f"❌ **Ошибка на сервере Kaggle ({brand}) при обработке стрима!**\n"
            f"🎬 Стрим: {url}\n"
            f"Код выхода: `{proc_returncode}`"
        )
    else:
        notify_telegram(
            f"🎉 **НАРЕЗКА И ВЫГРУЗКА ЗАВЕРШЕНЫ ({brand})!**\n\n"
            f"🎬 Стрим: {url}\n"
            f"🎬 Длинных выпусков: **{len(long_mp4s)}**\n"
            f"📱 Shorts 9:16: **{len(short_mp4s)}**\n"
            f"{status_line}\n\n"
            f"💤 Облачный GPU-сервер Kaggle завершил работу."
        )


if __name__ == "__main__":
    main()
