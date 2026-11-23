"""
PREMFSUB-V00.02 - Telegram media storage bot
Basis: DMG Cloud Storage Bot v1.5

Fitur:
- Maksimal 3 Database dan 3 tujuan Force Subscribe (FSub).
- Semua pengguna boleh mengakses media setelah bergabung ke seluruh FSub.
- Tidak memakai allowlist, sistem premium, mode global, atau Logs.
- Upload foto/video tunggal dan album hanya oleh Super Owner.
- Deep link unik, Autopost (dengan format LINK NONTON & opsional thumbnail /addpic, /delpic), Auto-delete DM, Protect Content, /stats.
- Konfigurasi JSON persisten dan worker penghapusan yang pulih setelah restart.

Gunakan hanya untuk media milik sendiri atau media yang penggunaannya telah diizinkan.
Install: pip install "python-telegram-bot>=21.6,<23"
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==================== CONFIGURATION ====================
DEFAULT_TOKEN = "9046821392:AAHe2KZAjw-TZeGJKS-MEQSCLOXzSbrsGoI"
DEFAULT_OWNER_ID = 6719844402

BOT_TOKEN = os.getenv("BOT_TOKEN", DEFAULT_TOKEN).strip()
try:
    OWNER_ID = int(os.getenv("OWNER_ID", str(DEFAULT_OWNER_ID)).strip())
except ValueError:
    OWNER_ID = 0

MAX_DATABASES = 3
MAX_FSUB = 3
DEFAULT_AUTODELETE_MINUTES = 30
ALBUM_WAIT_SECONDS = 2.0
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "premfsub_state.json"
LEGACY_CONFIG_FILE = BASE_DIR / "dmg_cloud_settings.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("PREMFSUB")

STATE_LOCK = asyncio.Lock()
ALBUM_LOCK = asyncio.Lock()
ALBUMS = {}
INVITE_LINKS = {}


def default_state():
    return {
        "storage_chat_ids": [],
        "fsub_chat_ids": [],
        "autopost_chat_id": None,
        "thumbnail_file_id": None,
        "protect_content": True,
        "autodelete_minutes": DEFAULT_AUTODELETE_MINUTES,
        "media": {},
        "pending_uploads": {},
        "delete_queue": [],
        "users_seen": [],
    }


def validate_config():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE":
        raise SystemExit("Isi BOT_TOKEN dengan token bot baru.")
    if not isinstance(OWNER_ID, int) or OWNER_ID <= 0:
        raise SystemExit("Isi OWNER_ID dengan Telegram user ID milikmu.")


def atomic_save(data):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="premfsub_", suffix=".tmp", dir=str(BASE_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass


def load_state():
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if not isinstance(loaded, dict):
                raise ValueError("Format state bukan object.")
            state = default_state()
            state.update(loaded)
        except Exception as exc:
            raise RuntimeError(
                f"Gagal membaca {STATE_FILE}. Jangan hapus file data."
            ) from exc
    else:
        state = default_state()
        if LEGACY_CONFIG_FILE.exists():
            try:
                with LEGACY_CONFIG_FILE.open("r", encoding="utf-8") as stream:
                    legacy = json.load(stream)
                old_ids = legacy.get("storage_chat_ids", [])
                if isinstance(old_ids, list):
                    state["storage_chat_ids"] = old_ids
            except Exception:
                log.exception("Konfigurasi lama tidak dapat dimigrasikan.")

    for key, maximum in (
        ("storage_chat_ids", MAX_DATABASES),
        ("fsub_chat_ids", MAX_FSUB),
    ):
        cleaned = []
        for value in state.get(key, []):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value not in cleaned:
                cleaned.append(value)
        state[key] = cleaned[:maximum]

    if not isinstance(state.get("media"), dict):
        state["media"] = {}
    if not isinstance(state.get("pending_uploads"), dict):
        state["pending_uploads"] = {}
    if not isinstance(state.get("delete_queue"), list):
        state["delete_queue"] = []
    if not isinstance(state.get("users_seen"), list):
        state["users_seen"] = []
    return state


async def get_state():
    async with STATE_LOCK:
        return load_state()


async def update_state(mutator):
    async with STATE_LOCK:
        state = load_state()
        mutator(state)
        atomic_save(state)
        return state


async def owner_only(update: Update):
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or user.id != OWNER_ID:
        if message:
            await message.reply_text("Maaf, perintah ini khusus Super Owner.")
        return False
    if not chat or chat.type != ChatType.PRIVATE:
        if message:
            await message.reply_text("Gunakan perintah melalui chat pribadi bot.")
        return False
    return True


def parse_id(context):
    if not context.args:
        return None
    try:
        return int(context.args[0])
    except (TypeError, ValueError):
        return None


def chat_type_name(chat):
    if chat.type == ChatType.CHANNEL:
        return "Channel"
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return "Grup"
    return str(chat.type)


async def get_join_link(bot, chat_id):
    try:
        chat = await bot.get_chat(chat_id)
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}"
        cached = INVITE_LINKS.get(chat_id)
        if cached:
            return cached
        invite = await bot.create_chat_invite_link(
            chat_id=chat_id, name="PREMFSUB FSub"
        )
        INVITE_LINKS[chat_id] = invite.invite_link
        return invite.invite_link
    except TelegramError:
        log.exception("Tidak dapat mengambil link FSub %s", chat_id)
        return None


async def check_fsub(bot, user_id):
    state = await get_state()
    missing = []
    for chat_id in state["fsub_chat_ids"]:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            status = str(member.status).lower()
            is_member = status in ("creator", "administrator", "member")
            if status == "restricted":
                is_member = bool(getattr(member, "is_member", False))
            if not is_member:
                missing.append(chat_id)
        except TelegramError as exc:
            log.warning("Pengecekan FSub gagal untuk %s: %s", chat_id, exc)
            missing.append(chat_id)
    return missing


async def fsub_keyboard(bot, missing, code=None):
    rows = []
    for index, chat_id in enumerate(missing, 1):
        link = await get_join_link(bot, chat_id)
        if link:
            rows.append([InlineKeyboardButton(f"📢 Join Channel/Grup {index}", url=link)])
    callback_data = f"fsub_check:{code}" if code else "fsub_check"
    rows.append([InlineKeyboardButton("🔄 Saya Sudah Join", callback_data=callback_data)])
    return InlineKeyboardMarkup(rows)


async def show_fsub_gate(update, context, missing, code=None, edit_message=False):
    keyboard = await fsub_keyboard(context.bot, missing, code)
    user = update.effective_user
    full_name = html.escape(user.full_name) if user else "Pengguna"
    text = (
        f"👋 Hello {full_name}\n\n"
        "Anda harus bergabung di Channel/Grup saya terlebih dahulu untuk melihat file yang saya bagikan.\n\n"
        "Silakan join ke Channel & Group terlebih dahulu, lalu tekan <b>Saya Sudah Join</b>."
    )
    if edit_message and update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.edit_text(
                text, reply_markup=keyboard, parse_mode="HTML"
            )
            return
        except TelegramError:
            pass
    message = update.effective_message
    if message:
        await message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def deliver_media(update, context, code):
    user = update.effective_user
    message = update.effective_message
    if not user:
        return

    clean_code = (code or "").strip()
    if not clean_code or not CODE_PATTERN.match(clean_code):
        if message:
            await message.reply_text("❌ Tautan tidak valid atau media sudah tidak tersedia.")
        return

    state = await get_state()
    item = state["media"].get(clean_code)
    if not item:
        log.warning("Kode media tidak ditemukan di state: %s", clean_code)
        if message:
            await message.reply_text("❌ Tautan tidak valid atau media sudah tidak tersedia.")
        return

    backups = item.get("backups", [])
    if not backups:
        if message:
            await message.reply_text("❌ Media sudah tidak tersedia di Database.")
        return

    protect = bool(state.get("protect_content", True))
    sent_ids = None
    last_error = None

    for backup in backups:
        chat_id = backup.get("chat_id")
        message_ids = backup.get("message_ids", [])
        if not chat_id or not message_ids:
            continue
        try:
            if len(message_ids) > 1:
                copied = await context.bot.copy_messages(
                    chat_id=user.id,
                    from_chat_id=chat_id,
                    message_ids=message_ids,
                    protect_content=protect,
                )
                sent_ids = [entry.message_id for entry in copied]
            else:
                copied = await context.bot.copy_message(
                    chat_id=user.id,
                    from_chat_id=chat_id,
                    message_id=message_ids[0],
                    protect_content=protect,
                )
                sent_ids = [copied.message_id]
            if sent_ids:
                break
        except TelegramError as exc:
            last_error = exc
            log.warning("Backup %s tidak dapat dikirim ke %s: %s", chat_id, user.id, exc)

    if not sent_ids:
        log.error("Semua backup gagal untuk kode %s: %s", clean_code, last_error)
        if message:
            await message.reply_text("❌ Media sedang tidak tersedia. Coba lagi nanti.")
        return

    minutes = state.get("autodelete_minutes", DEFAULT_AUTODELETE_MINUTES)
    if minutes:
        entry = {
            "chat_id": user.id,
            "message_ids": sent_ids,
            "delete_at": time.time() + int(minutes) * 60,
            "attempts": 0,
        }
        await update_state(lambda current: current["delete_queue"].append(entry))
        await context.bot.send_message(
            chat_id=user.id,
            text=f"⏳ Media yang dikirim di chat pribadi ini akan otomatis dihapus dalam {minutes} menit.",
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    def remember(state):
        if user.id not in state["users_seen"]:
            state["users_seen"].append(user.id)

    await update_state(remember)

    code = None
    if context.args:
        code = context.args[0].strip()
    elif message.text and " " in message.text:
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 2:
            code = parts[1].strip()

    if user.id == OWNER_ID:
        if code:
            await deliver_media(update, context, code)
            return
        await message.reply_text(
            f"👋 Hello {html.escape(user.full_name)}\n\n"
            "Kirim foto/video atau album untuk disimpan ke Database, "
            "atau ketik /help untuk melihat daftar perintah."
        )
        return

    if not code:
        await message.reply_text(
            f"👋 Hello {html.escape(user.full_name)}\n\n"
            "Silakan buka tautan media yang dibagikan di channel untuk mengakses konten."
        )
        return

    missing = await check_fsub(context.bot, user.id)
    if missing:
        await show_fsub_gate(update, context, missing, code=code)
        return

    await deliver_media(update, context, code)


async def fsub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    await query.answer()
    data = query.data or ""
    code = data.split(":", 1)[1].strip() if ":" in data else None

    missing = await check_fsub(context.bot, user.id)
    if missing:
        await show_fsub_gate(update, context, missing, code=code, edit_message=True)
        return

    try:
        await query.message.edit_text("✅ Keanggotaan FSub terverifikasi! Mengirim media...")
    except TelegramError:
        pass

    if code:
        await deliver_media(update, context, code)
    else:
        await context.bot.send_message(
            chat_id=user.id,
            text="✅ Anda sudah bergabung ke seluruh FSub. Silakan buka tautan media yang dibagikan.",
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    await update.effective_message.reply_text(
        "🛠️ PANEL PERINTAH SUPER OWNER\n\n"
        "DATABASE (maksimal 3)\n"
        "/setdb ID — Tambah Database\n"
        "/cekdb — Lihat Database\n"
        "/deldb ID — Hapus Database\n\n"
        "FORCE SUBSCRIBE (maksimal 3)\n"
        "/addfsub ID — Tambah channel/grup wajib join\n"
        "/delfsub ID — Hapus tujuan FSub\n"
        "/listfsub — Lihat daftar FSub\n\n"
        "AUTOPOST & THUMBNAIL\n"
        "/setautopost ID — Atur channel Autopost\n"
        "/cekautopost — Cek Autopost\n"
        "/delautopost — Hapus Autopost\n"
        "/addpic — Reply foto untuk pasang thumbnail postingan\n"
        "/delpic — Hapus thumbnail postingan\n\n"
        "PENGATURAN\n"
        "/autodelete 30 — Hapus salinan DM setelah 30 menit\n"
        "/autodelete off — Matikan Auto-delete\n"
        "/protect true — Aktifkan perlindungan konten\n"
        "/protect false — Nonaktifkan perlindungan konten\n"
        "/stats — Lihat statistik bot\n"
        "/cancelupload — Batalkan sesi upload\n\n"
        "Upload foto/video tunggal dan album dilakukan dengan mengirim media ke chat pribadi bot. "
        "Kirim pesan yang diawali # beserta link untuk Autopost otomatis (LINK NONTON 1, 2, dst.), maksimal 10 link."
    )


async def addpic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    msg = update.effective_message
    replied = msg.reply_to_message if msg else None
    if not replied or not replied.photo:
        await msg.reply_text("❌ Balas (reply) sebuah foto dengan perintah /addpic.")
        return
    file_id = replied.photo[-1].file_id
    await update_state(lambda s: s.update({"thumbnail_file_id": file_id}))
    await msg.reply_text("✅ Foto thumbnail postingan berhasil disimpan.")


async def delpic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    state = await get_state()
    if not state.get("thumbnail_file_id"):
        await update.effective_message.reply_text("ℹ️ Tidak ada thumbnail yang aktif.")
        return
    await update_state(lambda s: s.update({"thumbnail_file_id": None}))
    await update.effective_message.reply_text("🗑️ Thumbnail postingan berhasil dihapus.")


async def setdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    msg = update.effective_message
    chat_id = parse_id(context)
    if chat_id is None:
        await msg.reply_text("Format: /setdb -1001234567890")
        return
    state = await get_state()
    ids = state["storage_chat_ids"]
    if chat_id in ids:
        await msg.reply_text("ℹ️ Database tersebut sudah terdaftar.")
        return
    if len(ids) >= MAX_DATABASES:
        await msg.reply_text("❌ Maksimal 3 Database. Hapus salah satu dengan /deldb ID.")
        return
    try:
        target = await context.bot.get_chat(chat_id)
        if target.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
            await msg.reply_text("❌ Tujuan harus berupa channel atau grup.")
            return
        bot_user = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(chat_id, bot_user.id)
        if str(bot_member.status).lower() not in ("administrator", "creator"):
            await msg.reply_text(
                "❌ Jadikan bot admin di Database agar bot dapat menyimpan media."
            )
            return
    except TelegramError as exc:
        await msg.reply_text(
            "❌ Bot tidak dapat mengakses tujuan. Pastikan ID benar dan bot sudah "
            f"ditambahkan dengan izin yang sesuai.\nDetail: {str(exc)[:180]}"
        )
        return
    await update_state(lambda s: s["storage_chat_ids"].append(chat_id))
    await msg.reply_text(
        f"✅ Database ditambahkan.\nNama: {target.title or chat_id}\n"
        f"ID: {chat_id}\nTotal: {len(ids) + 1}/{MAX_DATABASES}"
    )


async def cekdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    state = await get_state()
    ids = state["storage_chat_ids"]
    if not ids:
        await update.effective_message.reply_text("📦 Belum ada Database. Gunakan /setdb ID.")
        return
    lines = [f"📦 DATABASE ({len(ids)}/{MAX_DATABASES})"]
    for i, chat_id in enumerate(ids, 1):
        try:
            chat = await context.bot.get_chat(chat_id)
            lines.append(f"{i}. {chat.title or chat_id}\n   {chat_type_name(chat)} | {chat_id}")
        except TelegramError:
            lines.append(f"{i}. ⚠️ Tidak dapat diakses | {chat_id}")
    await update.effective_message.reply_text("\n".join(lines))


async def deldb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    chat_id = parse_id(context)
    if chat_id is None:
        await update.effective_message.reply_text("Format: /deldb -1001234567890")
        return
    state = await get_state()
    if chat_id not in state["storage_chat_ids"]:
        await update.effective_message.reply_text("ℹ️ ID tidak ditemukan di Database.")
        return
    await update_state(lambda s: s["storage_chat_ids"].remove(chat_id))
    await update.effective_message.reply_text(f"🗑️ Database {chat_id} dihapus dari konfigurasi.")


async def addfsub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    chat_id = parse_id(context)
    msg = update.effective_message
    if chat_id is None:
        await msg.reply_text("Format: /addfsub -1001234567890")
        return
    state = await get_state()
    ids = state["fsub_chat_ids"]
    if chat_id in ids:
        await msg.reply_text("ℹ️ Tujuan FSub sudah terdaftar.")
        return
    if len(ids) >= MAX_FSUB:
        await msg.reply_text("❌ Maksimal 3 tujuan FSub.")
        return
    try:
        target = await context.bot.get_chat(chat_id)
        if target.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
            await msg.reply_text("❌ FSub harus berupa channel atau grup.")
            return
        bot_user = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(chat_id, bot_user.id)
        if str(bot_member.status).lower() not in ("administrator", "creator"):
            await msg.reply_text(
                "❌ Jadikan bot admin di seluruh tujuan FSub agar pemeriksaan "
                "keanggotaan pengguna dapat berjalan."
            )
            return
    except TelegramError as exc:
        await msg.reply_text(
            "❌ Bot belum dapat memeriksa grup/channel tersebut. Pastikan bot "
            "sudah ditambahkan dan dijadikan admin.\n"
            f"Detail: {str(exc)[:160]}"
        )
        return
    await update_state(lambda s: s["fsub_chat_ids"].append(chat_id))
    link = await get_join_link(context.bot, chat_id)
    await msg.reply_text(
        f"✅ FSub ditambahkan.\nNama: {target.title or chat_id}\nID: {chat_id}\n"
        f"Link: {link or 'Tidak tersedia — cek izin invite link bot.'}\n"
        f"Total: {len(ids) + 1}/{MAX_FSUB}"
    )


async def delfsub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    chat_id = parse_id(context)
    if chat_id is None:
        await update.effective_message.reply_text("Format: /delfsub -1001234567890")
        return
    state = await get_state()
    if chat_id not in state["fsub_chat_ids"]:
        await update.effective_message.reply_text("ℹ️ ID tidak ditemukan di daftar FSub.")
        return
    await update_state(lambda s: s["fsub_chat_ids"].remove(chat_id))
    INVITE_LINKS.pop(chat_id, None)
    await update.effective_message.reply_text(f"🗑️ FSub {chat_id} berhasil dihapus.")


async def listfsub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    state = await get_state()
    ids = state["fsub_chat_ids"]
    if not ids:
        await update.effective_message.reply_text("📢 Belum ada FSub. Gunakan /addfsub ID.")
        return
    lines = [f"📢 DAFTAR FORCE SUBSCRIBE ({len(ids)}/{MAX_FSUB})"]
    for i, chat_id in enumerate(ids, 1):
        try:
            chat = await context.bot.get_chat(chat_id)
            link = await get_join_link(context.bot, chat_id)
            lines.append(
                f"{i}. {chat.title or chat_id}\n   {chat_type_name(chat)} | {chat_id}\n"
                f"   Link: {link or 'Tidak tersedia'}"
            )
        except TelegramError:
            lines.append(f"{i}. ⚠️ Tidak dapat diakses | {chat_id}")
    await update.effective_message.reply_text("\n\n".join(lines), disable_web_page_preview=True)


async def setautopost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    chat_id = parse_id(context)
    msg = update.effective_message
    if chat_id is None:
        await msg.reply_text("Format: /setautopost -1001234567890")
        return
    try:
        chat = await context.bot.get_chat(chat_id)
        if chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
            await msg.reply_text("❌ Autopost harus berupa channel atau grup.")
            return
        bot_user = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(chat_id, bot_user.id)
        if str(bot_member.status).lower() not in ("administrator", "creator"):
            await msg.reply_text(
                "❌ Jadikan bot admin di Autopost agar dapat menerbitkan postingan."
            )
            return
    except TelegramError as exc:
        await msg.reply_text(f"❌ Bot tidak dapat mengakses tujuan: {str(exc)[:180]}")
        return
    await update_state(lambda s: s.update({"autopost_chat_id": chat_id}))
    await msg.reply_text(f"✅ Autopost diatur ke {chat.title or chat_id} ({chat_id}).")


async def cekautopost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    state = await get_state()
    chat_id = state.get("autopost_chat_id")
    if not chat_id:
        await update.effective_message.reply_text("Autopost: ❌ Belum diatur.")
        return
    try:
        chat = await context.bot.get_chat(chat_id)
        await update.effective_message.reply_text(
            f"Autopost: ✅ Aktif\nNama: {chat.title or chat_id}\nID: {chat_id}"
        )
    except TelegramError:
        await update.effective_message.reply_text(
            f"Autopost tersimpan tetapi bot tidak dapat mengaksesnya.\nID: {chat_id}"
        )


async def delautopost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    await update_state(lambda s: s.update({"autopost_chat_id": None}))
    await update.effective_message.reply_text("🗑️ Konfigurasi Autopost dihapus.")


async def autodelete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Format: /autodelete 30 atau /autodelete off")
        return
    value = context.args[0].lower()
    if value == "off":
        minutes = None
    else:
        try:
            minutes = int(value)
            if minutes < 1 or minutes > 10080:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text(
                "❌ Masukkan durasi 1–10080 menit atau gunakan /autodelete off."
            )
            return
    await update_state(lambda s: s.update({"autodelete_minutes": minutes}))
    await update.effective_message.reply_text(
        "✅ Auto-delete dinonaktifkan." if minutes is None
        else f"✅ Auto-delete diatur ke {minutes} menit."
    )


async def protect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    if not context.args or context.args[0].lower() not in ("true", "false"):
        await update.effective_message.reply_text("Format: /protect true atau /protect false")
        return
    enabled = context.args[0].lower() == "true"
    await update_state(lambda s: s.update({"protect_content": enabled}))
    await update.effective_message.reply_text(
        f"✅ Protection {'aktif' if enabled else 'nonaktif'}."
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    state = await get_state()
    autopost = "Aktif" if state.get("autopost_chat_id") else "Belum diatur"
    thumbnail = "Aktif" if state.get("thumbnail_file_id") else "Belum diatur"
    minutes = state.get("autodelete_minutes", DEFAULT_AUTODELETE_MINUTES)
    autodelete = "Nonaktif" if not minutes else f"Aktif ({minutes} menit)"
    protection = "Aktif" if state.get("protect_content", True) else "Nonaktif"
    await update.effective_message.reply_text(
        "📊 PREMFSUB — STATISTIK BOT\n\n"
        f"📦 Database terpasang: {len(state['storage_chat_ids'])}/{MAX_DATABASES}\n"
        f"📢 Grup/Channel FSub: {len(state['fsub_chat_ids'])}/{MAX_FSUB}\n"
        f"👥 Pengguna pernah mengakses: {len(state['users_seen'])}\n"
        f"🗂️ Item media tersimpan: {len(state['media'])}\n\n"
        f"📣 Autopost: {autopost}\n"
        f"🖼️ Thumbnail: {thumbnail}\n"
        f"🕒 Auto-delete: {autodelete}\n"
        f"🛡️ Protection: {protection}"
    )


def supported_media(message):
    return bool(message and (message.photo or message.video))


async def make_unique_code(state):
    while True:
        candidate = secrets.token_urlsafe(19)[:25]
        if candidate not in state.get("media", {}):
            return candidate


async def store_upload(messages, context):
    messages.sort(key=lambda m: m.message_id)
    state = await get_state()
    destinations = state["storage_chat_ids"]
    if not destinations:
        await messages[0].reply_text("❌ Database belum diatur. Tambahkan dengan /setdb ID.")
        return
    backups, errors = [], []
    for chat_id in destinations:
        try:
            if len(messages) > 1:
                copied = await context.bot.copy_messages(
                    chat_id=chat_id,
                    from_chat_id=messages[0].chat_id,
                    message_ids=[m.message_id for m in messages],
                )
                ids = [entry.message_id for entry in copied]
            else:
                copied = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=messages[0].chat_id,
                    message_id=messages[0].message_id,
                )
                ids = [copied.message_id]
            backups.append({"chat_id": chat_id, "message_ids": ids})
        except RetryAfter as exc:
            wait = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else float(exc.retry_after)
            )
            await asyncio.sleep(max(wait, 1) + 0.5)
            try:
                if len(messages) > 1:
                    copied = await context.bot.copy_messages(
                        chat_id=chat_id,
                        from_chat_id=messages[0].chat_id,
                        message_ids=[m.message_id for m in messages],
                    )
                    ids = [entry.message_id for entry in copied]
                else:
                    copied = await context.bot.copy_message(
                        chat_id=chat_id,
                        from_chat_id=messages[0].chat_id,
                        message_id=messages[0].message_id,
                    )
                    ids = [copied.message_id]
                backups.append({"chat_id": chat_id, "message_ids": ids})
            except Exception as retry_exc:
                errors.append(f"{chat_id}: {str(retry_exc)[:120]}")
        except Exception as exc:
            log.exception("Upload gagal ke Database %s", chat_id)
            errors.append(f"{chat_id}: {str(exc)[:120]}")

    if not backups:
        await messages[0].reply_text(
            "❌ Media gagal disimpan ke seluruh Database.\n" + "\n".join(errors)
        )
        return

    state = await get_state()
    code = await make_unique_code(state)
    item = {
        "backups": backups,
        "media_count": len(messages),
        "created_at": int(time.time()),
    }
    await update_state(lambda current: current["media"].update({code: item}))
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    note = (
        "\n⚠️ Sebagian Database gagal menerima media:\n" + "\n".join(errors)
        if errors
        else ""
    )
    await messages[0].reply_text(
        f"✅ Media berhasil disimpan ke {len(backups)} Database.\n"
        f"Jumlah file dalam unit ini: {len(messages)}\nLink media:\n{link}\n\n"
        "Salin link ini untuk menyusun postingan Autopost. Pesan yang diawali # akan diteruskan otomatis dengan format LINK NONTON 1, 2, dst. Maksimal 10 link per postingan."
        + note,
        disable_web_page_preview=True,
    )


async def manual_autopost_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, message = update.effective_user, update.effective_message
    if (
        not user
        or user.id != OWNER_ID
        or not message
        or not message.text
        or message.chat.type != ChatType.PRIVATE
    ):
        return
    raw_text = message.text.strip()
    if not raw_text.startswith("#"):
        return

    state = await get_state()
    autopost_id = state.get("autopost_chat_id")
    if not autopost_id:
        await message.reply_text("❌ Channel Autopost belum diatur. Gunakan /setautopost ID.")
        return

    me = await context.bot.get_me()
    pattern = re.compile(
        r"https://t\.me/[A-Za-z0-9_]+(?:\?start=|\?startapp=)([A-Za-z0-9_-]{4,64})"
    )
    found_codes = pattern.findall(raw_text)
    codes = list(dict.fromkeys(found_codes))

    if not codes:
        await message.reply_text("❌ Tidak ditemukan link media PREMFSUB yang valid di pesan hashtag.")
        return
    if len(found_codes) > 10:
        await message.reply_text("❌ Maksimal 10 link media dalam satu postingan Autopost.")
        return
    if any(code not in state.get("media", {}) for code in codes):
        await message.reply_text("❌ Ada kode link yang tidak terdaftar. Periksa kembali link media.")
        return

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    first_line = lines[0] if lines else raw_text
    title = re.sub(r"^#+\s*", "", first_line).strip()
    title = re.sub(r"</?b>", "", title, flags=re.IGNORECASE).strip()

    body_parts = [f"<b>{html.escape(title)}</b>"]
    for idx, code in enumerate(codes, 1):
        url = f"https://t.me/{me.username}?start={code}"
        body_parts.append(f"LINK NONTON {idx}:\n{url}")

    body_parts.append(f"<blockquote>Fsub By @{me.username}</blockquote>")
    post_text = "\n\n".join(body_parts)
    thumb_id = state.get("thumbnail_file_id")

    try:
        if thumb_id:
            if len(post_text) <= 1024:
                await context.bot.send_photo(
                    chat_id=autopost_id,
                    photo=thumb_id,
                    caption=post_text,
                    parse_mode="HTML",
                )
            else:
                await context.bot.send_photo(
                    chat_id=autopost_id,
                    photo=thumb_id,
                )
                await context.bot.send_message(
                    chat_id=autopost_id,
                    text=post_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        else:
            await context.bot.send_message(
                chat_id=autopost_id,
                text=post_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await message.reply_text(
            f"✅ Postingan berhasil dikirim ke Autopost.\nJumlah link: {len(codes)}"
        )
    except TelegramError as exc:
        await message.reply_text(
            f"⚠️ Postingan gagal dikirim ke Autopost.\nDetail: {str(exc)[:180]}"
        )


async def cancelupload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    async with ALBUM_LOCK:
        ALBUMS.clear()
    await update.effective_message.reply_text("Media yang sudah tersimpan di Database tetap tersedia.")


async def flush_album(key, context):
    try:
        while True:
            await asyncio.sleep(ALBUM_WAIT_SECONDS)
            async with ALBUM_LOCK:
                album = ALBUMS.get(key)
                if not album:
                    return
                if time.monotonic() - album["last_update"] >= ALBUM_WAIT_SECONDS:
                    messages = list(album["messages"].values())
                    del ALBUMS[key]
                    break
        await store_upload(messages, context)
    except Exception:
        log.exception("Gagal memproses album %s", key)


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or user.id != OWNER_ID or not message or not supported_media(message):
        return
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.media_group_id:
        key = (message.chat_id, message.media_group_id)
        async with ALBUM_LOCK:
            album = ALBUMS.setdefault(
                key, {"messages": {}, "last_update": time.monotonic()}
            )
            album["messages"][message.message_id] = message
            album["last_update"] = time.monotonic()
            if not album.get("task_started"):
                album["task_started"] = True
                context.application.create_task(flush_album(key, context))
        return
    await store_upload([message], context)


async def delete_worker(app: Application):
    while True:
        try:
            state = await get_state()
            now = time.time()
            due = [
                entry
                for entry in state["delete_queue"]
                if entry.get("delete_at", 0) <= now
            ]
            if due:
                due_keys = {
                    (e["chat_id"], tuple(e["message_ids"]), e.get("delete_at"))
                    for e in due
                }

                def remove_due(s):
                    s["delete_queue"] = [
                        e
                        for e in s["delete_queue"]
                        if (
                            e.get("chat_id"),
                            tuple(e.get("message_ids", [])),
                            e.get("delete_at"),
                        )
                        not in due_keys
                    ]

                await update_state(remove_due)
                retry_entries = []
                for entry in due:
                    transient_failed = []
                    for message_id in entry.get("message_ids", []):
                        try:
                            await app.bot.delete_message(
                                chat_id=entry["chat_id"], message_id=message_id
                            )
                        except (RetryAfter, NetworkError, TimedOut) as exc:
                            log.warning(
                                "Auto-delete sementara gagal untuk %s: %s",
                                message_id,
                                exc,
                            )
                            transient_failed.append(message_id)
                        except TelegramError as exc:
                            log.info(
                                "Auto-delete pesan %s dilewati: %s",
                                message_id,
                                exc,
                            )
                    attempts = int(entry.get("attempts", 0)) + 1
                    if transient_failed and attempts < 10:
                        retry_entries.append(
                            {
                                "chat_id": entry["chat_id"],
                                "message_ids": transient_failed,
                                "delete_at": time.time() + 60,
                                "attempts": attempts,
                            }
                        )
                if retry_entries:
                    await update_state(
                        lambda s: s["delete_queue"].extend(retry_entries)
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Delete worker mengalami error; worker akan mencoba lagi.")
        await asyncio.sleep(5)


async def post_init(app: Application):
    me = await app.bot.get_me()
    log.info("PREMFSUB aktif sebagai @%s", me.username)
    state = await get_state()
    log.info(
        "Database %s/%s | FSub %s/%s",
        len(state["storage_chat_ids"]),
        MAX_DATABASES,
        len(state["fsub_chat_ids"]),
        MAX_FSUB,
    )
    app.create_task(delete_worker(app))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if error:
        log.error(
            "Unhandled exception saat memproses update",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        log.error("Unhandled exception tanpa detail error.")


def build_application():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(40)
        .write_timeout(40)
        .pool_timeout(30)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(fsub_callback, pattern=r"^fsub_check(?::.*)?$"))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("addpic", addpic_cmd))
    app.add_handler(CommandHandler("delpic", delpic_cmd))
    app.add_handler(CommandHandler("setdb", setdb_cmd))
    app.add_handler(CommandHandler("cekdb", cekdb_cmd))
    app.add_handler(CommandHandler("deldb", deldb_cmd))
    app.add_handler(CommandHandler("addfsub", addfsub_cmd))
    app.add_handler(CommandHandler("delfsub", delfsub_cmd))
    app.add_handler(CommandHandler("listfsub", listfsub_cmd))
    app.add_handler(CommandHandler("setautopost", setautopost_cmd))
    app.add_handler(CommandHandler("cekautopost", cekautopost_cmd))
    app.add_handler(CommandHandler("delautopost", delautopost_cmd))
    app.add_handler(CommandHandler("autodelete", autodelete_cmd))
    app.add_handler(CommandHandler("protect", protect_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("cancelupload", cancelupload_cmd))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, manual_autopost_handler),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO, media_handler), group=1
    )
    app.add_error_handler(error_handler)
    return app


def main():
    validate_config()
    if not STATE_FILE.exists():
        atomic_save(load_state())

    delay = 5
    while True:
        app = build_application()
        try:
            log.info("Menjalankan PREMFSUB-V00.02...")
            app.run_polling(
                timeout=30,
                bootstrap_retries=-1,
                drop_pending_updates=False,
                allowed_updates=Update.ALL_TYPES,
                close_loop=False,
            )
            break
        except KeyboardInterrupt:
            log.info("Bot dihentikan oleh pengguna.")
            break
        except Exception:
            log.exception(
                "Polling berhenti. Mencoba memulai ulang dalam %s detik.", delay
            )
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
