import os
import re
import asyncio
import sqlite3
import logging
import tempfile

import discord
from discord.ext import commands, tasks
from discord import app_commands

import edge_tts
import emoji


# ==================================================
# CẤU HÌNH
# ==================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Chỉ User ID này được sử dụng các slash command
OWNER_ID = 1356498767788118099

# Dùng chung database cho khóa Voice và từ thay thế
DB_FILE = "voice_lock.sqlite3"

# Cấu hình Edge TTS
TTS_VOICE = os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural")
TTS_RATE = os.getenv("TTS_RATE", "-10%")
TTS_PITCH = os.getenv("TTS_PITCH", "+10Hz")

MAX_TEXT_LENGTH = 1500
MAX_QUEUE_SIZE = 30
VOICE_MONITOR_INTERVAL = 10


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("voice_lock_bot")


# ==================================================
# DATABASE
# ==================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.execute("PRAGMA busy_timeout = 5000")

db.execute("""
CREATE TABLE IF NOT EXISTS voice_lock (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS word_replacements (
    original TEXT PRIMARY KEY COLLATE NOCASE,
    replacement TEXT NOT NULL
)
""")

db.commit()


def save_lock(guild_id: int, channel_id: int):
    db.execute(
        """
        INSERT INTO voice_lock (guild_id, channel_id, enabled)
        VALUES (?, ?, 1)
        ON CONFLICT(guild_id)
        DO UPDATE SET channel_id = excluded.channel_id, enabled = 1
        """,
        (guild_id, channel_id),
    )
    db.commit()


def remove_lock(guild_id: int):
    db.execute(
        "DELETE FROM voice_lock WHERE guild_id = ?",
        (guild_id,),
    )
    db.commit()


def get_locks():
    rows = db.execute(
        """
        SELECT guild_id, channel_id
        FROM voice_lock
        WHERE enabled = 1
        """
    ).fetchall()
    return {guild_id: channel_id for guild_id, channel_id in rows}


# ==================================================
# QUẢN LÝ TỪ THAY THẾ
# ==================================================

def add_word_replacement(original: str, replacement: str):
    original = original.strip().lower()
    replacement = replacement.strip()

    db.execute(
        """
        INSERT INTO word_replacements (original, replacement)
        VALUES (?, ?)
        ON CONFLICT(original)
        DO UPDATE SET replacement = excluded.replacement
        """,
        (original, replacement),
    )
    db.commit()


def get_word_replacements():
    return db.execute(
        """
        SELECT original, replacement
        FROM word_replacements
        ORDER BY original COLLATE NOCASE
        """
    ).fetchall()


def remove_word_replacement(original: str) -> bool:
    cursor = db.execute(
        """
        DELETE FROM word_replacements
        WHERE original = ? COLLATE NOCASE
        """,
        (original.strip(),),
    )
    db.commit()
    return cursor.rowcount > 0


def replace_words(text: str) -> str:
    rows = get_word_replacements()

    # Thay từ/cụm từ dài trước để hạn chế xung đột.
    rows.sort(key=lambda item: len(item[0]), reverse=True)

    for original, replacement in rows:
        if not original:
            continue

        pattern = r"(?<!\w)" + re.escape(original) + r"(?!\w)"
        text = re.sub(
            pattern,
            lambda _match, value=replacement: value,
            text,
            flags=re.IGNORECASE,
        )

    return text


# ==================================================
# INTENTS
# ==================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.messages = True
intents.message_content = True


# ==================================================
# TRẠNG THÁI TTS
# ==================================================

speech_enabled = {}
speech_queues = {}
speech_workers = {}
speech_locks = {}
active_audio_files = {}


def is_speech_enabled(guild_id: int) -> bool:
    return speech_enabled.get(guild_id, False)


def get_queue(guild_id: int):
    if guild_id not in speech_queues:
        speech_queues[guild_id] = asyncio.Queue()
    return speech_queues[guild_id]


def get_lock(guild_id: int):
    if guild_id not in speech_locks:
        speech_locks[guild_id] = asyncio.Lock()
    return speech_locks[guild_id]


# ==================================================
# BOT
# ==================================================

class VoiceLockBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
        )
        self.voice_targets = get_locks()
        self.cleaned_guild_commands = False

    async def setup_hook(self):
        try:
            synced = await self.tree.sync()
            logger.info("Đã đồng bộ %s lệnh toàn cục.", len(synced))
        except Exception:
            logger.exception("Không thể đồng bộ lệnh toàn cục.")

        if not self.voice_monitor.is_running():
            self.voice_monitor.start()

    async def on_ready(self):
        logger.info(
            "Bot online: %s | ID: %s",
            self.user,
            self.user.id if self.user else "unknown",
        )

        # Dọn lệnh đăng ký riêng theo server; lệnh toàn cục không bị xóa.
        if not self.cleaned_guild_commands:
            self.cleaned_guild_commands = True

            for guild in self.guilds:
                try:
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
                    logger.info(
                        "Đã dọn lệnh riêng tại server %s (%s).",
                        guild.name,
                        guild.id,
                    )
                except Exception:
                    logger.exception(
                        "Không thể dọn lệnh riêng tại server %s.",
                        guild.id,
                    )

    async def try_reconnect(self, guild_id: int):
        channel_id = self.voice_targets.get(guild_id)
        if channel_id is None:
            return

        guild = self.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            logger.warning("Không tìm thấy phòng đã khóa: %s", channel_id)
            return

        member = guild.me
        if member is None:
            return

        permissions = channel.permissions_for(member)
        if not permissions.view_channel or not permissions.connect:
            logger.warning("Bot không có quyền truy cập phòng %s.", channel_id)
            return

        try:
            voice_client = guild.voice_client

            if voice_client and voice_client.is_connected():
                if voice_client.channel.id != channel_id:
                    await voice_client.move_to(channel)
                    logger.info("Bot đã quay về phòng %s.", channel_id)
            else:
                await channel.connect(timeout=20, reconnect=True)
                logger.info("Bot đã kết nối lại phòng %s.", channel_id)

        except discord.Forbidden:
            logger.warning("Discord từ chối quyền kết nối phòng %s.", channel_id)
        except discord.HTTPException:
            logger.exception("Lỗi Discord khi kết nối phòng %s.", channel_id)
        except asyncio.TimeoutError:
            logger.warning("Hết thời gian chờ kết nối phòng %s.", channel_id)
        except Exception:
            logger.exception("Lỗi không xác định khi kết nối phòng %s.", channel_id)

    @tasks.loop(seconds=VOICE_MONITOR_INTERVAL)
    async def voice_monitor(self):
        for guild_id in list(self.voice_targets.keys()):
            try:
                await self.try_reconnect(guild_id)
            except Exception:
                logger.exception("Lỗi kiểm tra Voice tại server %s.", guild_id)

    @voice_monitor.before_loop
    async def before_voice_monitor(self):
        await self.wait_until_ready()

    async def on_voice_state_update(self, member, before, after):
        # Chỉ theo dõi trạng thái Voice của bot.
        if self.user is None or member.id != self.user.id:
            return

        guild_id = member.guild.id
        if guild_id not in self.voice_targets:
            return

        target_id = self.voice_targets[guild_id]
        moved_away = after.channel is None or after.channel.id != target_id

        if moved_away:
            await asyncio.sleep(2)
            await self.try_reconnect(guild_id)


bot = VoiceLockBot()


# ==================================================
# LỌC NỘI DUNG TIN NHẮN CHO TTS
# ==================================================

URL_PATTERN = re.compile(
    r"""(?ix)\b(?:https?://|www\.|discord\.gg/|discord(?:app)?\.com/)[^\s<>]+"""
)
DISCORD_ID_PATTERN = re.compile(r"(?<!\d)\d{15,22}(?!\d)")
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]+:\d{15,22}>")
MARKDOWN_LINK_PATTERN = re.compile(
    r"\[([^\]]*)\]\(\s*https?://[^)]*\)",
    re.IGNORECASE,
)
USER_MENTION_PATTERN = re.compile(r"<@!?\d{15,22}>")
ROLE_MENTION_PATTERN = re.compile(r"<@&\d{15,22}>")
CHANNEL_MENTION_PATTERN = re.compile(r"<#\d{15,22}>")


def resolve_mentions(message: discord.Message, text: str) -> str:
    if message.guild is None:
        return text

    for member in message.mentions:
        for mention in (f"<@{member.id}>", f"<@!{member.id}>"):
            text = text.replace(mention, member.display_name)

    text = ROLE_MENTION_PATTERN.sub(" ", text)
    text = CHANNEL_MENTION_PATTERN.sub(" ", text)
    return USER_MENTION_PATTERN.sub(" ", text)


def clean_message(message: discord.Message) -> str:
    text = message.content or ""
    if not text.strip():
        return ""

    text = resolve_mentions(message, text)
    text = MARKDOWN_LINK_PATTERN.sub(" ", text)
    text = URL_PATTERN.sub(" ", text)
    text = CUSTOM_EMOJI_PATTERN.sub(" ", text)
    text = DISCORD_ID_PATTERN.sub(" ", text)

    text = emoji.replace_emoji(text, replace=" ")
    text = USER_MENTION_PATTERN.sub(" ", text)
    text = ROLE_MENTION_PATTERN.sub(" ", text)
    text = CHANNEL_MENTION_PATTERN.sub(" ", text)
    text = text.replace("\u200b", " ").replace("\ufeff", " ")
    text = re.sub(r"\s+", " ", text).strip()

    if not re.search(r"[^\W_]", text, flags=re.UNICODE):
        return ""

    # Áp dụng từ thay thế trước khi đưa văn bản vào TTS.
    text = replace_words(text)
    return text[:MAX_TEXT_LENGTH].strip()


# ==================================================
# VOICE CLIENT
# ==================================================

def get_voice_client(guild: discord.Guild):
    voice_client = guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        return None
    return voice_client


# ==================================================
# TẠO FILE TTS
# ==================================================

async def create_tts_file(text: str) -> str:
    fd, file_path = tempfile.mkstemp(
        prefix="discord_tts_",
        suffix=".mp3",
    )
    os.close(fd)

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=TTS_VOICE,
            rate=TTS_RATE,
            pitch=TTS_PITCH,
        )
        await communicate.save(file_path)

        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            raise RuntimeError("TTS không tạo được file âm thanh.")

        return file_path
    except Exception:
        delete_audio_file(file_path)
        raise


# ==================================================
# XÓA FILE ÂM THANH
# ==================================================

def delete_audio_file(file_path: str):
    if not file_path:
        return

    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        logger.warning("Không xóa được file âm thanh: %s", file_path)


# ==================================================
# QUẢN LÝ HÀNG ĐỢI
# ==================================================

def clear_speech_queue(guild_id: int):
    queue = get_queue(guild_id)

    while True:
        try:
            queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            break


# ==================================================
# WORKER TTS
# ==================================================

async def speech_worker(guild_id: int):
    queue = get_queue(guild_id)

    while not bot.is_closed():
        text = await queue.get()
        file_path = None
        source = None

        try:
            if not is_speech_enabled(guild_id):
                continue

            guild = bot.get_guild(guild_id)
            if guild is None:
                continue

            voice_client = get_voice_client(guild)
            if voice_client is None:
                continue

            try:
                file_path = await create_tts_file(text)
            except Exception:
                logger.exception("Không tạo được âm thanh TTS.")
                continue

            if not is_speech_enabled(guild_id):
                continue

            voice_client = get_voice_client(guild)
            if voice_client is None:
                continue

            async with get_lock(guild_id):
                if not is_speech_enabled(guild_id):
                    continue

                voice_client = get_voice_client(guild)
                if voice_client is None:
                    continue

                while voice_client.is_playing():
                    if not is_speech_enabled(guild_id):
                        break
                    await asyncio.sleep(0.2)

                if not is_speech_enabled(guild_id) or not voice_client.is_connected():
                    continue

                source = discord.FFmpegPCMAudio(file_path, options="-vn")
                active_audio_files[guild_id] = file_path

                try:
                    voice_client.play(source)

                    while voice_client.is_playing():
                        if not is_speech_enabled(guild_id):
                            voice_client.stop()
                            break
                        if not voice_client.is_connected():
                            break
                        await asyncio.sleep(0.2)

                except Exception:
                    logger.exception("Lỗi phát âm thanh TTS.")

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Lỗi worker TTS của server %s", guild_id)
        finally:
            if source is not None:
                try:
                    source.cleanup()
                except Exception:
                    pass

            if file_path:
                delete_audio_file(file_path)

            if active_audio_files.get(guild_id) == file_path:
                active_audio_files.pop(guild_id, None)

            queue.task_done()


def ensure_worker(guild_id: int):
    task = speech_workers.get(guild_id)
    if task is None or task.done():
        speech_workers[guild_id] = asyncio.create_task(speech_worker(guild_id))


# ==================================================
# KIỂM TRA OWNER
# ==================================================

def owner_only():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            raise app_commands.CheckFailure(
                "❌ Chỉ Owner bot mới được sử dụng lệnh này!"
            )
        return True

    return app_commands.check(predicate)


# ==================================================
# /join — KHÓA PHÒNG VOICE
# ==================================================

@bot.tree.command(name="join", description="Khóa vị trí phòng thoại của bot.")
@owner_only()
async def join(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ dùng được trong server.", ephemeral=True
        )
        return

    member = interaction.guild.get_member(interaction.user.id)
    if member is None or member.voice is None:
        await interaction.response.send_message(
            "Bạn cần vào phòng thoại trước khi dùng /join.", ephemeral=True
        )
        return

    channel = member.voice.channel
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        await interaction.response.send_message(
            "Không thể khóa vị trí ở phòng này.", ephemeral=True
        )
        return

    bot_member = interaction.guild.me
    if bot_member is None:
        await interaction.response.send_message(
            "Không thể lấy thông tin bot trong server.", ephemeral=True
        )
        return

    permissions = channel.permissions_for(bot_member)
    if not permissions.view_channel or not permissions.connect:
        await interaction.response.send_message(
            "Bot không có quyền vào phòng thoại này.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    bot.voice_targets[interaction.guild.id] = channel.id
    save_lock(interaction.guild.id, channel.id)

    try:
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_connected():
            if voice_client.channel.id != channel.id:
                await voice_client.move_to(channel)
        else:
            await channel.connect(timeout=20, reconnect=True)

        await interaction.followup.send(
            f"🔒 Đã khóa vị trí phòng thoại tại {channel.mention}.\n"
            "Bot sẽ cố gắng quay lại đây nếu bị chuyển phòng hoặc ngắt kết nối.",
            ephemeral=True,
        )
    except Exception:
        logger.exception("Không thể kết nối phòng thoại.")
        await interaction.followup.send(
            "Đã lưu phòng cần khóa, nhưng bot chưa kết nối được. "
            "Hãy kiểm tra quyền Connect và thử lại.",
            ephemeral=True,
        )


# ==================================================
# /leave — TẮT KHÓA PHÒNG
# ==================================================

@bot.tree.command(
    name="leave",
    description="Tắt khóa vị trí và cho bot rời phòng thoại.",
)
@owner_only()
async def leave(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ dùng được trong server.", ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    speech_enabled[guild_id] = False
    clear_speech_queue(guild_id)

    bot.voice_targets.pop(guild_id, None)
    remove_lock(guild_id)

    voice_client = interaction.guild.voice_client
    if voice_client and (
        voice_client.is_connected() or voice_client.is_connecting()
    ):
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            logger.exception("Lỗi khi ngắt kết nối Voice.")
            await interaction.response.send_message(
                "Đã tắt chế độ khóa, nhưng bot gặp lỗi khi rời phòng.",
                ephemeral=True,
            )
            return

    await interaction.response.send_message(
        "🔓 Đã tắt khóa vị trí. Bot sẽ không tự quay lại phòng cũ.",
        ephemeral=True,
    )


# ==================================================
# /on — BẬT TTS
# ==================================================

@bot.tree.command(
    name="on",
    description="Bật chế độ đọc tin nhắn thành giọng nói.",
)
@owner_only()
@app_commands.guild_only()
async def turn_on(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ dùng được trong server.", ephemeral=True
        )
        return

    voice_client = get_voice_client(guild)
    if voice_client is None:
        await interaction.response.send_message(
            "Bot chưa ở trong phòng Voice. Hãy dùng `/join` trước.",
            ephemeral=True,
        )
        return

    speech_enabled[guild.id] = True
    ensure_worker(guild.id)

    await interaction.response.send_message(
        "Đã bật chế độ đọc tin nhắn! 🔊", ephemeral=True
    )


# ==================================================
# /off — TẮT TTS
# ==================================================

@bot.tree.command(
    name="off",
    description="Tắt chế độ đọc giọng nói nhưng vẫn ở Voice.",
)
@owner_only()
@app_commands.guild_only()
async def turn_off(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ dùng được trong server.", ephemeral=True
        )
        return

    speech_enabled[guild.id] = False
    clear_speech_queue(guild.id)

    voice_client = get_voice_client(guild)
    if voice_client is not None and (
        voice_client.is_playing() or voice_client.is_paused()
    ):
        voice_client.stop()

    await interaction.response.send_message(
        "Đã tắt chế độ đọc. Bot vẫn ở trong Voice. 🔇",
        ephemeral=True,
    )


# ==================================================
# ĐỌC TIN NHẮN
# ==================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    guild = message.guild
    guild_id = guild.id

    if not is_speech_enabled(guild_id):
        return

    voice_client = get_voice_client(guild)
    if voice_client is None:
        return

    # Giữ nguyên logic gốc: kênh chat có ID trùng với phòng Voice.
    if message.channel.id != voice_client.channel.id:
        return

    text = clean_message(message)
    if not text:
        return

    queue = get_queue(guild_id)
    if queue.qsize() >= MAX_QUEUE_SIZE:
        logger.info("Bỏ qua tin nhắn vì hàng đợi TTS đã đầy.")
        return

    await queue.put(text)
    ensure_worker(guild_id)


# ==================================================
# /sen — GỬI TIN NHẮN
# ==================================================

@bot.tree.command(name="sen", description="Gửi tin nhắn vào kênh hiện tại.")
@app_commands.describe(noi_dung="Nội dung tin nhắn muốn gửi")
@owner_only()
async def sen(interaction: discord.Interaction, noi_dung: str):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ dùng được trong server.", ephemeral=True
        )
        return

    if not interaction.channel or not hasattr(interaction.channel, "send"):
        await interaction.response.send_message(
            "Không thể gửi tin nhắn ở kênh này.", ephemeral=True
        )
        return

    if len(noi_dung) > 2000:
        await interaction.response.send_message(
            "Nội dung vượt quá giới hạn 2000 ký tự của Discord.",
            ephemeral=True,
        )
        return

    try:
        await interaction.channel.send(
            noi_dung,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.response.send_message(
            "Đã gửi tin nhắn.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "Bot không có quyền gửi tin nhắn ở kênh này.",
            ephemeral=True,
        )
    except discord.HTTPException:
        logger.exception("Không thể gửi tin nhắn.")
        await interaction.response.send_message(
            "Discord gặp lỗi khi gửi tin nhắn.", ephemeral=True
        )


# ==================================================
# /add — THÊM TỪ THAY THẾ
# ==================================================

@bot.tree.command(
    name="add",
    description="Thêm từ thay thế cho giọng đọc TTS.",
)
@app_commands.describe(
    tu_goc="Từ cần nhận diện, ví dụ: chx",
    tu_thay_the="Từ bot sẽ đọc thay, ví dụ: chưa",
)
@owner_only()
async def add_word(
    interaction: discord.Interaction,
    tu_goc: str,
    tu_thay_the: str,
):
    tu_goc = tu_goc.strip()
    tu_thay_the = tu_thay_the.strip()

    if not tu_goc or not tu_thay_the:
        await interaction.response.send_message(
            "❌ Từ gốc và từ thay thế không được để trống.",
            ephemeral=True,
        )
        return

    if len(tu_goc) > 100 or len(tu_thay_the) > 200:
        await interaction.response.send_message(
            "❌ Từ gốc tối đa 100 ký tự, từ thay thế tối đa 200 ký tự.",
            ephemeral=True,
        )
        return

    if tu_goc.casefold() == tu_thay_the.casefold():
        await interaction.response.send_message(
            "⚠️ Từ gốc và từ thay thế đang giống nhau.",
            ephemeral=True,
        )
        return

    add_word_replacement(tu_goc, tu_thay_the)

    await interaction.response.send_message(
        f"✅ Đã lưu từ thay thế:\n`{tu_goc}` → `{tu_thay_the}`",
        ephemeral=True,
    )


# ==================================================
# /list — XEM DANH SÁCH TỪ ĐÃ THÊM
# ==================================================

@bot.tree.command(
    name="list",
    description="Xem danh sách từ thay thế đã lưu.",
)
@owner_only()
async def list_words(interaction: discord.Interaction):
    rows = get_word_replacements()

    if not rows:
        await interaction.response.send_message(
            "📭 Chưa có từ thay thế nào được lưu.",
            ephemeral=True,
        )
        return

    lines = [
        f"`{original}` → `{replacement}`"
        for original, replacement in rows
    ]

    chunks = []
    current = "📚 **DANH SÁCH TỪ THAY THẾ**\n\n"

    for line in lines:
        if len(current) + len(line) + 1 > 1800:
            chunks.append(current)
            current = ""
        current += line + "\n"

    if current:
        chunks.append(current)

    await interaction.response.send_message(
        chunks[0],
        ephemeral=True,
    )

    for chunk in chunks[1:]:
        await interaction.followup.send(
            chunk,
            ephemeral=True,
        )


# ==================================================
# /remove — XÓA TỪ THAY THẾ
# ==================================================

async def remove_word_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    if interaction.user.id != OWNER_ID:
        return []

    rows = get_word_replacements()
    choices = []

    for original, replacement in rows:
        if current.casefold() not in original.casefold():
            continue

        choices.append(
            app_commands.Choice(
                name=f"{original} → {replacement}"[:100],
                value=original,
            )
        )

        if len(choices) >= 25:
            break

    return choices


@bot.tree.command(
    name="remove",
    description="Xóa một từ thay thế đã lưu.",
)
@app_commands.describe(tu="Chọn từ cần xóa trong danh sách gợi ý")
@app_commands.autocomplete(tu=remove_word_autocomplete)
@owner_only()
async def remove_word(
    interaction: discord.Interaction,
    tu: str,
):
    deleted = remove_word_replacement(tu)

    if deleted:
        await interaction.response.send_message(
            f"🗑️ Đã xóa từ thay thế `{tu}`.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"⚠️ Không tìm thấy từ `{tu}` trong danh sách.",
            ephemeral=True,
        )


# ==================================================
# XỬ LÝ LỖI SLASH COMMAND
# ==================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.CheckFailure):
        message = (
            "❌ Chỉ Owner bot mới được sử dụng lệnh này!"
            if interaction.user.id != OWNER_ID
            else "Lệnh này không thể thực hiện trong ngữ cảnh hiện tại."
        )
    else:
        logger.error(
            "Lỗi slash command:",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Lệnh gặp lỗi khi thực thi."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Không thể gửi thông báo lỗi.")


# ==================================================
# CHẠY BOT
# ==================================================

if not TOKEN:
    raise RuntimeError("Thiếu biến môi trường DISCORD_TOKEN.")

bot.run(TOKEN)
