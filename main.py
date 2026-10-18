import os
import re
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import edge_tts
import emoji


# =====================================================
# CONFIG
# =====================================================

TOKEN = os.getenv("DISCORD_TOKEN")

# Giọng nữ tiếng Việt
TTS_VOICE = os.getenv(
    "TTS_VOICE",
    "vi-VN-HoaiMyNeural"
)

TTS_RATE = os.getenv("TTS_RATE", "+0%")

# Có thể đặt TEST_GUILD_ID để đồng bộ lệnh nhanh trong
# một server. Để trống nếu muốn đồng bộ toàn bộ server.
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")

MAX_TEXT_LENGTH = 1500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("voice_tts_bot")


# =====================================================
# DISCORD BOT
# =====================================================

intents = discord.Intents.default()

intents.guilds = True
intents.voice_states = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =====================================================
# TRẠNG THÁI
# =====================================================

# Trạng thái đọc giọng nói theo từng server
speech_enabled = {}

# Hàng đợi đọc tin nhắn theo từng server
speech_queues = {}

# Worker xử lý hàng đợi theo từng server
speech_workers = {}

# Khóa tránh hai thao tác phát âm thanh cùng lúc
speech_locks = {}

# Lưu các file âm thanh đang sử dụng
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


# =====================================================
# LỌC NỘI DUNG TIN NHẮN
# =====================================================

# Link website
URL_PATTERN = re.compile(
    r"""(?ix)
    \b
    (?:
        https?://
        |
        www\.
        |
        discord\.gg/
        |
        discord(?:app)?\.com/
    )
    [^\s<>]+
    """
)

# ID Discord gồm 15-22 chữ số
DISCORD_ID_PATTERN = re.compile(r"(?<!\d)\d{15,22}(?!\d)")

# Emoji dạng custom của Discord:
# <:name:123456789012345678>
# <a:name:123456789012345678>
CUSTOM_EMOJI_PATTERN = re.compile(
    r"<a?:[A-Za-z0-9_]+:\d{15,22}>"
)

# Markdown link: [nội dung](https://example.com)
MARKDOWN_LINK_PATTERN = re.compile(
    r"\[([^\]]*)\]\(\s*https?://[^)]*\)",
    re.IGNORECASE
)

# Xóa các mention còn sót lại nếu không xử lý được
USER_MENTION_PATTERN = re.compile(r"<@!?\d{15,22}>")
ROLE_MENTION_PATTERN = re.compile(r"<@&\d{15,22}>")
CHANNEL_MENTION_PATTERN = re.compile(r"<#\d{15,22}>")


def resolve_mentions(message: discord.Message, text: str) -> str:
    """
    Thay mention người dùng bằng tên hiển thị của người được tag.
    Không đưa tên người gửi vào nội dung đọc.
    """

    guild = message.guild

    if guild is None:
        return text

    # Thay mention người dùng bằng display_name
    for member in message.mentions:
        mention_forms = [
            f"<@{member.id}>",
            f"<@!{member.id}>"
        ]

        display_name = member.display_name

        for mention in mention_forms:
            text = text.replace(mention, display_name)

    # Không đọc tên role hoặc tên kênh được tag
    text = ROLE_MENTION_PATTERN.sub(" ", text)
    text = CHANNEL_MENTION_PATTERN.sub(" ", text)

    # Mention không phân giải được thì bỏ
    text = USER_MENTION_PATTERN.sub(" ", text)

    return text


def clean_message(message: discord.Message) -> str:
    """
    Chỉ lấy nội dung tin nhắn.
    Không lấy tên người gửi.
    """

    text = message.content or ""

    if not text.strip():
        return ""

    # Đổi mention người dùng thành tên hiển thị
    text = resolve_mentions(message, text)

    # Bỏ Markdown link trước khi bỏ URL
    text = MARKDOWN_LINK_PATTERN.sub(" ", text)

    # Bỏ link
    text = URL_PATTERN.sub(" ", text)

    # Bỏ custom emoji Discord
    text = CUSTOM_EMOJI_PATTERN.sub(" ", text)

    # Bỏ các ID Discord còn sót lại
    text = DISCORD_ID_PATTERN.sub(" ", text)

    # Bỏ emoji Unicode
    text = emoji.replace_emoji(text, replace=" ")

    # Bỏ các mention còn sót lại
    text = USER_MENTION_PATTERN.sub(" ", text)
    text = ROLE_MENTION_PATTERN.sub(" ", text)
    text = CHANNEL_MENTION_PATTERN.sub(" ", text)

    # Bỏ các ký tự định dạng vô hình thông dụng
    text = text.replace("\u200b", " ")
    text = text.replace("\ufeff", " ")

    # Chuẩn hóa khoảng trắng
    text = re.sub(r"\s+", " ", text).strip()

    # Bỏ dấu câu nếu không còn nội dung
    if not re.search(r"[^\W_]", text, flags=re.UNICODE):
        return ""

    # Giới hạn độ dài để tránh đọc quá lâu
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]

    return text.strip()


# =====================================================
# KIỂM TRA VOICE
# =====================================================

def get_voice_client(guild: discord.Guild):
    voice_client = guild.voice_client

    if voice_client is None:
        return None

    if not voice_client.is_connected():
        return None

    return voice_client


# =====================================================
# TẠO ÂM THANH TTS
# =====================================================

async def create_tts_file(text: str) -> str:
    """
    Tạo file MP3 bằng Edge TTS.
    Trả về đường dẫn file.
    """

    fd, file_path = tempfile.mkstemp(
        prefix="discord_tts_",
        suffix=".mp3"
    )

    os.close(fd)

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=TTS_VOICE,
            rate=TTS_RATE
        )

        await communicate.save(file_path)

        if not os.path.exists(file_path):
            raise RuntimeError("TTS không tạo được file âm thanh.")

        if os.path.getsize(file_path) == 0:
            raise RuntimeError("File âm thanh TTS bị rỗng.")

        return file_path

    except Exception:
        try:
            os.remove(file_path)
        except OSError:
            pass

        raise


# =====================================================
# XÓA FILE ÂM THANH
# =====================================================

def delete_audio_file(file_path: str):
    if not file_path:
        return

    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        logger.warning(
            "Không xóa được file âm thanh: %s",
            file_path
        )


# =====================================================
# XÓA HÀNG ĐỢI
# =====================================================

def clear_speech_queue(guild_id: int):
    queue = get_queue(guild_id)

    while True:
        try:
            queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            break


# =====================================================
# WORKER ĐỌC TIN NHẮN
# =====================================================

async def speech_worker(guild_id: int):
    """
    Mỗi server có một worker.
    Đọc lần lượt từng tin nhắn để âm thanh không chồng lên nhau.
    """

    queue = get_queue(guild_id)

    while not bot.is_closed():

        text = await queue.get()
        file_path = None

        try:
            if not is_speech_enabled(guild_id):
                continue

            guild = bot.get_guild(guild_id)

            if guild is None:
                continue

            voice_client = get_voice_client(guild)

            if voice_client is None:
                continue

            # Tạo âm thanh
            try:
                file_path = await create_tts_file(text)
            except Exception:
                logger.exception("Không tạo được âm thanh TTS.")
                continue

            # Kiểm tra lại trạng thái sau khi TTS hoàn tất
            if not is_speech_enabled(guild_id):
                continue

            voice_client = get_voice_client(guild)

            if voice_client is None:
                continue

            lock = get_lock(guild_id)

            async with lock:

                if not is_speech_enabled(guild_id):
                    continue

                voice_client = get_voice_client(guild)

                if voice_client is None:
                    continue

                # Đợi âm thanh đang phát kết thúc
                while voice_client.is_playing():

                    if not is_speech_enabled(guild_id):
                        break

                    await asyncio.sleep(0.2)

                if not is_speech_enabled(guild_id):
                    continue

                if not voice_client.is_connected():
                    continue

                # Phát file MP3 qua FFmpeg
                source = discord.FFmpegPCMAudio(
                    file_path,
                    options="-vn"
                )

                active_audio_files[guild_id] = file_path

                loop = asyncio.get_running_loop()

                def after_play(error):
                    if error:
                        logger.error(
                            "Lỗi khi phát âm thanh: %s",
                            error
                        )

                    # Đánh thức worker sau khi phát xong
                    try:
                        loop.call_soon_threadsafe(
                            lambda: None
                        )
                    except RuntimeError:
                        pass

                try:
                    voice_client.play(
                        source,
                        after=after_play
                    )

                    # Đợi đến khi phát xong hoặc bị dừng
                    while voice_client.is_playing():

                        if not is_speech_enabled(guild_id):
                            voice_client.stop()
                            break

                        if not voice_client.is_connected():
                            break

                        await asyncio.sleep(0.2)

                except Exception:
                    logger.exception("Lỗi phát âm thanh.")

                finally:
                    try:
                        source.cleanup()
                    except Exception:
                        pass

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Lỗi worker TTS của server %s",
                guild_id
            )

        finally:
            if file_path:
                delete_audio_file(file_path)

            if active_audio_files.get(guild_id) == file_path:
                active_audio_files.pop(guild_id, None)

            queue.task_done()


def ensure_worker(guild_id: int):
    task = speech_workers.get(guild_id)

    if task is None or task.done():
        speech_workers[guild_id] = asyncio.create_task(
            speech_worker(guild_id)
        )


# =====================================================
# SLASH COMMANDS
# =====================================================

@bot.tree.command(
    name="join",
    description="Bot vào phòng Voice bạn đang tham gia"
)
@app_commands.guild_only()
async def join(interaction: discord.Interaction):

    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ sử dụng được trong server.",
            ephemeral=True
        )
        return

    member = interaction.user

    if not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "Không xác định được thành viên.",
            ephemeral=True
        )
        return

    if member.voice is None or member.voice.channel is None:
        await interaction.response.send_message(
            "Bạn cần vào phòng Voice trước!",
            ephemeral=True
        )
        return

    channel = member.voice.channel

    await interaction.response.defer(ephemeral=True)

    try:
        voice_client = get_voice_client(guild)

        if voice_client is not None:

            if voice_client.channel.id == channel.id:
                await interaction.followup.send(
                    f"Bot đang ở phòng **{channel.name}** rồi.",
                    ephemeral=True
                )
                return

            await voice_client.move_to(channel)

        else:
            await channel.connect(
                self_deaf=True,
                reconnect=True
            )

        await interaction.followup.send(
            f"Đã vào phòng Voice **{channel.name}**!\n"
            "Dùng `/on` để bật chế độ đọc tin nhắn.",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "Bot thiếu quyền xem hoặc kết nối phòng Voice.",
            ephemeral=True
        )

    except Exception:
        logger.exception("Lỗi lệnh /join")

        await interaction.followup.send(
            "Không thể vào Voice. Hãy kiểm tra quyền bot "
            "và cấu hình FFmpeg/PyNaCl.",
            ephemeral=True
        )


@bot.tree.command(
    name="leave",
    description="Bot rời khỏi phòng Voice"
)
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):

    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ sử dụng được trong server.",
            ephemeral=True
        )
        return

    # Tắt đọc trong server này
    speech_enabled[guild.id] = False

    # Dừng âm thanh đang phát
    voice_client = get_voice_client(guild)

    if voice_client is not None:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        await voice_client.disconnect(force=True)

    # Xóa các tin nhắn đang chờ đọc
    clear_speech_queue(guild.id)

    await interaction.response.send_message(
        "Bot đã rời phòng Voice.",
        ephemeral=True
    )


@bot.tree.command(
    name="on",
    description="Bật chế độ đọc tin nhắn thành giọng nói"
)
@app_commands.guild_only()
async def turn_on(interaction: discord.Interaction):

    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ sử dụng được trong server.",
            ephemeral=True
        )
        return

    voice_client = get_voice_client(guild)

    if voice_client is None:
        await interaction.response.send_message(
            "Bot chưa ở trong Voice. Hãy dùng `/join` trước.",
            ephemeral=True
        )
        return

    speech_enabled[guild.id] = True

    ensure_worker(guild.id)

    await interaction.response.send_message(
        "Đã bật chế độ đọc tin nhắn! 🔊",
        ephemeral=True
    )


@bot.tree.command(
    name="off",
    description="Tắt chế độ đọc giọng nói nhưng vẫn ở Voice"
)
@app_commands.guild_only()
async def turn_off(interaction: discord.Interaction):

    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "Lệnh này chỉ sử dụng được trong server.",
            ephemeral=True
        )
        return

    speech_enabled[guild.id] = False

    # Xóa nội dung đang chờ
    clear_speech_queue(guild.id)

    # Dừng âm thanh đang phát
    voice_client = get_voice_client(guild)

    if voice_client is not None:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

    await interaction.response.send_message(
        "Đã tắt chế độ đọc. Bot vẫn ở trong Voice. 🔇",
        ephemeral=True
    )


# =====================================================
# ĐỌC TIN NHẮN TRONG CHAT CỦA PHÒNG VOICE
# =====================================================

@bot.event
async def on_message(message: discord.Message):

    # Bỏ tin nhắn do bot gửi
    if message.author.bot:
        return

    # Bỏ tin nhắn ngoài server
    if message.guild is None:
        return

    guild = message.guild
    guild_id = guild.id

    # Chỉ đọc khi đã bật chế độ nói
    if not is_speech_enabled(guild_id):
        return

    voice_client = get_voice_client(guild)

    if voice_client is None:
        return

    # Chỉ đọc chat của chính phòng Voice bot đang ở
    if message.channel.id != voice_client.channel.id:
        return

    # Làm sạch nội dung
    text = clean_message(message)

    if not text:
        return

    # Đưa tin nhắn vào hàng đợi
    queue = get_queue(guild_id)

    # Giới hạn hàng đợi để tránh spam quá nhiều
    if queue.qsize() >= 30:
        logger.info(
            "Bỏ qua tin nhắn vì hàng đợi đã đầy."
        )
        return

    await queue.put(text)

    ensure_worker(guild_id)


# =====================================================
# ĐỒNG BỘ LỆNH
# =====================================================

@bot.event
async def setup_hook():
    try:
        if TEST_GUILD_ID:
            guild_object = discord.Object(
                id=int(TEST_GUILD_ID)
            )

            bot.tree.copy_global_to(
                guild=guild_object
            )

            synced = await bot.tree.sync(
                guild=guild_object
            )

            logger.info(
                "Đã đồng bộ %s lệnh vào server thử nghiệm.",
                len(synced)
            )

        else:
            synced = await bot.tree.sync()

            logger.info(
                "Đã đồng bộ %s lệnh toàn cục.",
                len(synced)
            )

    except Exception:
        logger.exception("Không đồng bộ được slash commands.")


# =====================================================
# READY
# =====================================================

@bot.event
async def on_ready():

    logger.info(
        "Bot đăng nhập thành công: %s (%s)",
        bot.user,
        bot.user.id
    )


# =====================================================
# KHỞI CHẠY
# =====================================================

if not TOKEN:
    raise RuntimeError(
        "Thiếu biến môi trường DISCORD_TOKEN."
    )

bot.run(TOKEN)