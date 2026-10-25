#!/usr/bin/env python3
"""
Telegram Bot for IPATool + Defile Decryption Pipeline with Live Progress and Cloudflare R2 Upload.
Bot Token: 8739861173:AAFfeKyIAftHNFS632gb_JnkG1pkC9o0D_M
"""

import os
import sys
import re
import json
import time
import shutil
import logging
import subprocess
from pathlib import Path
import requests
import boto3
from botocore.config import Config
from dotenv import load_dotenv

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("IPAToolBot")

BASE_DIR = Path(__file__).resolve().parent
# Load environment variables from .env
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN must be set in .env or environment")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

DEFILE_DIR = BASE_DIR / "defile"
VENV_PYTHON = DEFILE_DIR / ".venv" / "bin" / "python"
CREDENTIAL_PATH = DEFILE_DIR / "minhpwhs" / "credential.bin"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Cloudflare R2 Configuration
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "https://9daa44579604e972e3ca21aa980a92d7.r2.cloudflarestorage.com").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "1c9d625f5f0c0537e21dd421073982f9").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "aac80a7ef0ad9a9eaefd855c7c216ea6f4e53ae6c7ef0ed17b3cd866c623ed33").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "ipadecrypted").strip()

MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def make_progress_bar(percent_val: float, length: int = 10) -> str:
    pct = max(0, min(100, int(percent_val)))
    filled = int(length * pct / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {pct}%"


def send_message(chat_id: int, text: str, parse_mode: str = "Markdown", reply_markup: dict = None) -> dict:
    url = f"{API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        res = requests.post(url, json=payload, timeout=15)
        return res.json()
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return {}


def edit_message(chat_id: int, message_id: int, text: str, parse_mode: str = "Markdown") -> dict:
    url = f"{API_URL}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        return res.json()
    except Exception as e:
        logger.error(f"Error editing message: {e}")
        return {}


def send_document(chat_id: int, file_path: Path, caption: str = "") -> dict:
    url = f"{API_URL}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            files = {"document": (file_path.name, f)}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
            res = requests.post(url, data=data, files=files, timeout=300)
            return res.json()
    except Exception as e:
        logger.error(f"Error sending document: {e}")
        return {}


def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def upload_to_r2(file_path: Path, on_progress=None) -> str:
    """Uploads file to Cloudflare R2 and returns a presigned download URL (valid 7 days)."""
    try:
        s3 = get_r2_client()
        file_size = file_path.stat().st_size
        uploaded_bytes = 0
        last_update_time = [0.0]

        def callback(bytes_transferred):
            nonlocal uploaded_bytes
            uploaded_bytes += bytes_transferred
            if on_progress and file_size > 0:
                pct = (uploaded_bytes / file_size) * 100.0
                now = time.time()
                if now - last_update_time[0] >= 2.5 or pct >= 100:
                    details = f"{uploaded_bytes / (1024*1024):.1f}/{file_size / (1024*1024):.1f} MB"
                    on_progress(pct, details)
                    last_update_time[0] = now

        key = file_path.name
        s3.upload_file(str(file_path), R2_BUCKET, key, Callback=callback)

        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=604800,  # 7 days
        )
        return url
    except Exception as e:
        logger.error(f"Upload to R2 failed: {e}")
        return ""


def search_app(term: str, limit: int = 5) -> list[dict]:
    cmd = ["ipatool", "search", term, "--limit", str(limit), "--format", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in reversed(proc.stdout.strip().splitlines()):
            if line.startswith("{") and line.endswith("}"):
                data = json.loads(line)
                return data.get("apps", [])
    except Exception as e:
        logger.error(f"Error searching apps: {e}")
    return []


def download_app(target: str, is_app_id: bool, output_path: Path, on_progress=None) -> tuple[bool, str]:
    """Downloads IPA via ipatool with live progress capturing."""
    cmd = ["ipatool", "download"]
    if is_app_id:
        cmd += ["-i", target]
    else:
        cmd += ["-b", target]
    cmd += ["--purchase", "-o", str(output_path)]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        pattern = re.compile(r"downloading\s+(\d+)%\s*\|.*?\|\s*\(([^)]+)\)")
        last_update_time = 0.0
        output_history = []

        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if not line:
                continue

            cleaned = line.strip()
            if cleaned:
                output_history.append(cleaned)
                match = pattern.search(cleaned)
                if match and on_progress:
                    try:
                        percent_num = float(match.group(1))
                    except Exception:
                        percent_num = 0.0
                    details = match.group(2)
                    now = time.time()
                    if now - last_update_time >= 2.5:
                        on_progress(percent_num, details)
                        last_update_time = now

        proc.wait(timeout=30)
        if proc.returncode == 0 and output_path.exists():
            return True, "Download thành công"
        err = "\n".join(output_history[-5:]) if output_history else "Lỗi không xác định khi download"
        return False, err
    except Exception as e:
        return False, str(e)


def decrypt_ipa(encrypted_path: Path, decrypted_path: Path, on_status=None) -> tuple[bool, str]:
    """Decrypts IPA via defile with status updates."""
    if not CREDENTIAL_PATH.exists():
        return False, f"Không tìm thấy file credential tại {CREDENTIAL_PATH}"

    decrypt_script = DEFILE_DIR / "decrypt.py"
    cmd = [
        str(VENV_PYTHON),
        str(decrypt_script),
        "--credential",
        str(CREDENTIAL_PATH),
        str(encrypted_path),
        str(decrypted_path),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        output_history = []
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if not line:
                continue
            cleaned = line.strip()
            if cleaned:
                output_history.append(cleaned)
                if on_status and "pages=" in cleaned:
                    on_status(cleaned)

        proc.wait(timeout=180)
        if proc.returncode == 0 and decrypted_path.exists():
            return True, "Giải mã thành công"
        err = "\n".join(output_history[-5:]) if output_history else "Lỗi giải mã"
        return False, err
    except Exception as e:
        return False, str(e)


def handle_download_and_decrypt(chat_id: int, target: str, is_app_id: bool):
    status_msg = send_message(chat_id, f"🔍 *Đang khởi tạo tải ứng dụng:* `{target}`...")
    msg_id = status_msg.get("result", {}).get("message_id")

    clean_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", target)
    enc_path = DOWNLOAD_DIR / f"{clean_name}_encrypted.ipa"
    dec_path = DOWNLOAD_DIR / f"{clean_name}_decrypted.ipa"

    if enc_path.exists():
        enc_path.unlink()
    if dec_path.exists():
        dec_path.unlink()

    # Step 1: Download
    def update_download_progress(percent_num: float, details: str):
        if not msg_id:
            return
        bar = make_progress_bar(percent_num, length=12)
        text = (
            f"⏳ *[1/3] Đang tải IPA từ App Store...*\n"
            f"📱 Target: `{target}`\n\n"
            f"📊 Tiến độ: `{bar}`\n"
            f"⚡ Chi tiết: `{details}`"
        )
        edit_message(chat_id, msg_id, text)

    if msg_id:
        edit_message(chat_id, msg_id, f"⏳ *[1/3]* Đang kết nối tới App Store để tải IPA...\nTarget: `{target}`")

    ok, err = download_app(target, is_app_id, enc_path, on_progress=update_download_progress)
    if not ok:
        if msg_id:
            edit_message(chat_id, msg_id, f"❌ *Tải IPA thất bại:*\n`{err}`")
        return

    file_size_mb = enc_path.stat().st_size / (1024 * 1024)

    # Step 2: Decrypt
    def update_decrypt_status(info_line: str):
        if not msg_id:
            return
        text = (
            f"🔓 *[2/3] Đang giải mã FairPlay DRM bằng `defile`...*\n"
            f"📦 Dung lượng: `{file_size_mb:.1f} MB`\n\n"
            f"⚙️ `{info_line}`"
        )
        edit_message(chat_id, msg_id, text)

    if msg_id:
        edit_message(
            chat_id,
            msg_id,
            f"🔓 *[2/3]* Đã tải xong `{file_size_mb:.1f} MB`!\nĐang giải mã FairPlay DRM bằng `defile`..."
        )

    ok, err = decrypt_ipa(enc_path, dec_path, on_status=update_decrypt_status)
    if not ok:
        if msg_id:
            edit_message(chat_id, msg_id, f"❌ *Giải mã thất bại:*\n`{err}`")
        if enc_path.exists():
            enc_path.unlink()
        return

    # Delete encrypted file
    if enc_path.exists():
        enc_path.unlink()

    dec_size_bytes = dec_path.stat().st_size
    dec_size_mb = dec_size_bytes / (1024 * 1024)

    # Step 3: Upload to Cloudflare R2
    def update_upload_progress(percent_num: float, details: str):
        if not msg_id:
            return
        bar = make_progress_bar(percent_num, length=12)
        text = (
            f"☁️ *[3/3] Đang tải lên Cloudflare R2...*\n"
            f"📦 Dung lượng: `{dec_size_mb:.1f} MB`\n\n"
            f"📊 Tiến độ: `{bar}`\n"
            f"⚡ Tốc độ/Chi tiết: `{details}`"
        )
        edit_message(chat_id, msg_id, text)

    if msg_id:
        edit_message(chat_id, msg_id, f"☁️ *[3/3]* Đang tải lên Cloudflare R2 ({dec_size_mb:.1f} MB)...")

    r2_url = upload_to_r2(dec_path, on_progress=update_upload_progress)

    # Optional: Send via Telegram if <= 50MB
    telegram_sent = False
    if dec_size_bytes <= MAX_TELEGRAM_FILE_SIZE:
        if msg_id:
            edit_message(chat_id, msg_id, f"📤 Đang gửi file trực tiếp qua Telegram ({dec_size_mb:.1f} MB)...")
        caption = (
            f"✅ *Decrypted IPA Thành Công!*\n"
            f"📱 Target: `{target}`\n"
            f"📦 Dung lượng: `{dec_size_mb:.1f} MB`\n"
            f"💻 Tương thích SimRun trên macOS"
        )
        res = send_document(chat_id, dec_path, caption=caption)
        if res.get("ok"):
            telegram_sent = True

    final_text = (
        f"🎉 *Hoàn tất giải mã & Lưu trữ!*\n\n"
        f"📱 *Target:* `{target}`\n"
        f"📦 *Dung lượng:* `{dec_size_mb:.1f} MB`\n\n"
    )
    if r2_url:
        final_text += f"☁️ *Link tải Cloudflare R2 (tốc độ cao, hạn 7 ngày):*\n👉 [Bấm vào đây để tải file IPA]({r2_url})\n\n"
    else:
        final_text += "⚠️ *Upload R2 thất bại, file vẫn an toàn trên máy Mac.*\n\n"

    final_text += f"📂 *Đường dẫn file trên máy Mac:*\n`{dec_path}`\n\n"
    if telegram_sent:
        final_text += "📬 *File IPA đính kèm đã được gửi trực tiếp vào tin nhắn phía trên.*"

    if msg_id:
        edit_message(chat_id, msg_id, final_text)
    else:
        send_message(chat_id, final_text)


def process_message(message: dict):
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return

    logger.info(f"Received message from {chat_id}: {text}")

    if text in ("/start", "/help"):
        welcome_text = (
            "👋 *Xin chào! Tôi là IPATool + Defile Bot.*\n\n"
            "Tôi giúp bạn tải app từ App Store, tự động giải mã FairPlay DRM và upload lên Cloudflare R2 để chạy trên Mac (SimRun).\n\n"
            "*Cách sử dụng:*\n"
            "1. 🔗 *Gửi link App Store:*\n"
            "   `https://apps.apple.com/us/app/the-calculator/id398129933`\n\n"
            "2. 📦 *Gửi Bundle ID:*\n"
            "   `com.google.ios.youtube`\n\n"
            "3. 🔍 *Tìm kiếm app:*\n"
            "   `/search <tên app>` (ví dụ: `/search calculator`)\n\n"
            "4. ℹ️ *Xem trạng thái:*\n"
            "   `/status`"
        )
        send_message(chat_id, welcome_text)
        return

    if text == "/status":
        auth_proc = subprocess.run(["ipatool", "auth", "info"], capture_output=True, text=True)
        cred_status = "✅ Sẵn sàng" if CREDENTIAL_PATH.exists() else "❌ Chưa có credential"
        status_text = (
            f"📊 *Hệ Thống Trạng Thái:*\n\n"
            f"🔑 *Credential FairPlay:* {cred_status}\n"
            f"👤 *App Store Account:*\n`{auth_proc.stdout.strip() or auth_proc.stderr.strip()}`\n"
            f"☁️ *Cloudflare R2 Bucket:* `{R2_BUCKET}`\n"
            f"📁 *Thư mục lưu trữ:* `{DOWNLOAD_DIR}`"
        )
        send_message(chat_id, status_text)
        return

    if text.startswith("/search "):
        query = text[8:].strip()
        if not query:
            send_message(chat_id, "⚠️ Vui lòng nhập từ khóa tìm kiếm. Ví dụ: `/search calculator`")
            return
        send_message(chat_id, f"🔍 Đang tìm kiếm `{query}` trên App Store...")
        apps = search_app(query, limit=5)
        if not apps:
            send_message(chat_id, f"❌ Không tìm thấy app nào phù hợp với từ khóa `{query}`.")
            return

        lines = [f"🔍 *Kết quả tìm kiếm cho:* `{query}`\n"]
        for i, app in enumerate(apps, 1):
            name = app.get("name")
            bid = app.get("bundleID")
            app_id = app.get("id")
            ver = app.get("version")
            lines.append(
                f"*{i}. {name}* (v{ver})\n"
                f"   • Bundle ID: `{bid}`\n"
                f"   • Tải ngay: `/dl_{app_id}` hoặc copy `{bid}`\n"
            )
        send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/dl_"):
        app_id = text[4:].strip()
        if app_id.isdigit():
            handle_download_and_decrypt(chat_id, app_id, is_app_id=True)
            return

    # Check if text is App Store URL
    app_store_match = re.search(r"id(\d{5,12})", text)
    if app_store_match:
        app_id = app_store_match.group(1)
        handle_download_and_decrypt(chat_id, app_id, is_app_id=True)
        return

    # Check if text looks like a Bundle ID (e.g. com.google.ios.youtube)
    if re.match(r"^[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)+$", text):
        handle_download_and_decrypt(chat_id, text, is_app_id=False)
        return

    # Otherwise, treat as search query
    send_message(chat_id, f"🔍 Đang tìm kiếm `{text}` trên App Store...")
    apps = search_app(text, limit=3)
    if not apps:
        send_message(chat_id, f"❌ Không tìm thấy app nào. Bạn có thể gửi trực tiếp link App Store hoặc Bundle ID.")
        return

    lines = [f"🔍 *Tìm thấy các ứng dụng sau:*\n"]
    for i, app in enumerate(apps, 1):
        name = app.get("name")
        bid = app.get("bundleID")
        app_id = app.get("id")
        lines.append(f"*{i}. {name}*\n   • Lệnh tải: `/dl_{app_id}`\n   • Bundle ID: `{bid}`\n")
    lines.append("👉 Bấm vào lệnh `/dl_...` phía trên để tự động tải & giải mã!")
    send_message(chat_id, "\n".join(lines))


def main():
    logger.info("Starting Telegram Bot with Live Progress and Cloudflare R2 Upload...")
    offset = 0
    while True:
        try:
            url = f"{API_URL}/getUpdates"
            params = {"offset": offset, "timeout": 30}
            res = requests.get(url, params=params, timeout=40)
            if res.status_code == 200:
                data = res.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "message" in update:
                        process_message(update["message"])
            else:
                logger.warning(f"getUpdates returned {res.status_code}")
                time.sleep(2)
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error in polling: {e}")
            time.sleep(3)
        except Exception as e:
            logger.error(f"Unexpected error in polling: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
