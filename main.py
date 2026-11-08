import asyncio
import logging
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands
import edge_tts
import emoji

# ==================== CẤU HÌNH ====================
TOKEN = "YOUR_BOT_TOKEN_HERE"  # Thay bằng token của bạn
PREFIX = "none"
TTS_VOICE = "vi-VN-HoaiMyNeural"
TTS_RATE = "+0%"
TTS_PITCH = "+0Hz"

# ==================== DATABASE ====================
DB_PATH = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS voice_lock (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            locked_at REAL
        )
    """)
    conn.commit()
    conn.close()

def get_locked_channel(guild_id: int) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT channel_id FROM voice_lock WHERE guild_id = ?", (guild_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_locked_channel(guild_id: int, channel_id: Optional[int]):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if channel_id is None:
        c.execute("DELETE FROM voice_lock WHERE guild_id = ?", (guild_id,))
    else:
        c.execute(
            "INSERT OR REPLACE INTO voice_lock (guild_id, channel_id, locked_at) VALUES (?, ?, ?)",
            (guild_id, channel_id, time.time()),
        )
    conn.commit()
    conn.close()

# ==================== TTS QUEUE ====================
speech_queues: dict[int, asyncio.Queue] = {}
active_audio_files: dict[int, str] = {}
speech_locks: dict[int, asyncio.Lock] = {}

def get_speech_queue(guild_id: int) -> asyncio.Queue:
    if guild_id not in speech_queues:
        speech_queues[guild_id] = asyncio.Queue()
    return speech_queues[guild_id]

def get_speech_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in speech_locks:
        speech_locks[guild_id] = asyncio.Lock()
    return speech_locks[guild_id]

def delete_audio_file(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning("Không xóa được file %s: %s", path, e)

# ==================== TTS ====================
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

        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        if size < 1000:
            raise RuntimeError(f"TTS tạo file quá nhỏ ({size} bytes)")

        logger.info("TTS file created: %s (%s bytes)", file_path, size)
        return file_path
    except Exception:
        delete_audio_file(file_path)
        raise

# ==================== TEXT CLEANING ====================
def clean_message(content: str, guild: discord.Guild) -> str:
    # Xóa mention
    content = re.sub(r"<@!?\d+>", "", content)
    content = re.sub(r"<@&\d+>", "", content)
    content = re.sub(r"<#\d+>", "", content)

    # Xóa URL
    content = re.sub(r"https?://\S+", "", content)

    # Xóa custom emoji
    content = re.sub(r"<:\w+:\d+>", "", content)
    content = re.sub(r"<a:\w+:\d+>", "", content)

    # Xóa emoji thường
    content = emoji.replace_emoji(content, replace="")

    # Dọn khoảng trắng
    content = re.sub(r"\s+", " ", content).strip()
    return content

# ==================== SPEECH WORKER ====================
async def speech_worker(guild_id: int):
    queue = get_speech_queue(guild_id)
    lock = get_speech_lock(guild_id)

    while True:
        text = await queue.get()
        try:
            async with lock:
                guild = bot.get_guild(guild_id)
                if not guild:
                    continue

                voice_client = guild.voice_client
                if not voice_client or not voice_client.is_connected():
                    locked_ch_id = get_locked_channel(guild_id)
                    if locked_ch_id:
                        channel = guild.get_channel(locked_ch_id)
                        if channel and isinstance(channel, discord.VoiceChannel):
                            try:
                                voice_client = await channel.connect()
                            except Exception as e:
                                logger.error("Không connect được voice: %s", e)
                                continue
                        else:
                            continue
                    else:
                        continue

                if not voice_client or not voice_client.is_connected():
                    continue

                # Tạo file TTS
                try:
                    file_path = await create_tts_file(text)
                except Exception as e:
                    logger.error("Không tạo được âm thanh TTS: %s", e)
                    continue

                # Phát
                logger.info(
                    "Playing TTS | guild=%s | size=%s bytes",
                    guild_id,
                    os.path.getsize(file_path),
                )

                source = discord.FFmpegOpusAudio(file_path)
                active_audio_files[guild_id] = file_path

                try:
                    voice_client.play(source)
                    while voice_client.is_playing():
                        await asyncio.sleep(0.1)
                except Exception as e:
                    logger.error("Lỗi khi play: %s", e)
                finally:
                    delete_audio_file(file_path)
                    active_audio_files.pop(guild_id, None)

        except Exception as e:
            logger.error("Lỗi speech_worker: %s", e)
        finally:
            queue.task_done()

# ==================== BOT ====================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)
logger = logging.getLogger("discord")
logger.setLevel(logging.INFO)

@bot.event
async def on_ready():
    init_db()
    logger.info("Bot đã sẵn sàng: %s", bot.user)
    for guild in bot.guilds:
        asyncio.create_task(speech_worker(guild.id))

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await bot.process_commands(message)

    content = clean_message(message.content, message.guild)
    if not content:
        return

    # Chỉ đọc khi bot đang ở trong voice channel
    if not message.guild.voice_client or not message.guild.voice_client.is_connected():
        return

    # Kiểm tra channel lock
    locked_id = get_locked_channel(message.guild.id)
    if locked_id and message.channel.id != locked_id:
        return

    queue = get_speech_queue(message.guild.id)
    await queue.put(content)

@bot.command(name="join")
async def join(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Bạn phải ở trong voice channel.")
        return

    channel = ctx.author.voice.channel
    if ctx.voice_client:
        if ctx.voice_client.channel == channel:
            await ctx.send("Đã ở trong channel này rồi.")
            return
        await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()

    set_locked_channel(ctx.guild.id, channel.id)
    await ctx.send(f"Đã join và khóa vào {channel.name}")

@bot.command(name="leave")
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
    set_locked_channel(ctx.guild.id, None)
    await ctx.send("Đã rời và mở khóa.")

@bot.command(name="lock")
async def lock(ctx):
    if not ctx.voice_client:
        await ctx.send("Bot chưa ở trong voice.")
        return
    set_locked_channel(ctx.guild.id, ctx.voice_client.channel.id)
    await ctx.send(f"Đã khóa vào {ctx.voice_client.channel.name}")

@bot.command(name="unlock")
async def unlock(ctx):
    set_locked_channel(ctx.guild.id, None)
    await ctx.send("Đã mở khóa.")

# ==================== RUN ====================
if __name__ == "__main__":
    bot.run(TOKEN)