import os
import asyncio
import logging
from collections import Counter
import discord
from discord import app_commands
from discord.ext import commands
# =====================================================
# CẤU HÌNH
# =====================================================
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = 1356498767788118099
# Số tin nhắn xử lý trước khi nhường quyền chạy cho tác vụ khác
BATCH_SIZE = 500
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.members = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents
)
# =====================================================
# KIỂM TRA OWNER
# =====================================================
def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID
# =====================================================
# CHỌN CÁC KÊNH CÓ THỂ QUÉT
# =====================================================
def get_readable_channels(guild: discord.Guild):
    channels = []
    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)
        if permissions.view_channel and permissions.read_message_history:
            channels.append(channel)
    return channels
# =====================================================
# QUÉT LỊCH SỬ MỘT KÊNH
# =====================================================
async def scan_channel(
    channel: discord.TextChannel,
    counter: Counter,
    target_user_id: int = None
):
    total = 0
    try:
        async for message in channel.history(
            limit=None,
            oldest_first=False
        ):
            # Chỉ tính tin nhắn người dùng, không tính bot/webhook
            if message.author.bot or message.webhook_id is not None:
                continue
            if target_user_id is not None:
                if message.author.id != target_user_id:
                    continue
            counter[message.author.id] += 1
            total += 1
            # Nhường quyền chạy định kỳ để tránh chặn event loop
            if total % BATCH_SIZE == 0:
                await asyncio.sleep(0)
    except discord.Forbidden:
        logging.warning(
            "Không có quyền đọc lịch sử kênh: %s",
            channel.name
        )
    except discord.HTTPException as exc:
        logging.warning(
            "Lỗi khi quét kênh %s: %s",
            channel.name,
            exc
        )
    return total
# =====================================================
# QUÉT NHIỀU KÊNH
# =====================================================
async def scan_channels(
    channels,
    target_user_id: int = None,
    progress_message=None
):
    counter = Counter()
    scanned_channels = 0
    total_channels = len(channels)
    for channel in channels:
        await scan_channel(
            channel,
            counter,
            target_user_id
        )
        scanned_channels += 1
        if progress_message is not None:
            try:
                await progress_message.edit(
                    content=(
                        f"🔎 Đang quét lịch sử tin nhắn...\n"
                        f"📚 Tiến độ: {scanned_channels}/{total_channels} kênh"
                    )
                )
            except discord.HTTPException:
                pass
    return counter, scanned_channels
# =====================================================
# TẠO EMBED TOP 10
# =====================================================
def make_top_embed(
    guild: discord.Guild,
    counter: Counter,
    scope_name: str,
    scanned_channels: int
):
    embed = discord.Embed(
        title="🏆 TOP 10 NGƯỜI NHẮN TIN NHIỀU NHẤT",
        description=(
            f"📍 Phạm vi: **{scope_name}**\n"
            f"🗂️ Số kênh đã quét: **{scanned_channels}**"
        ),
        color=discord.Color.blurple()
    )
    ranking = counter.most_common(10)
    if not ranking:
        embed.add_field(
            name="Không có dữ liệu",
            value="Không tìm thấy tin nhắn phù hợp.",
            inline=False
        )
        return embed
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for index, (user_id, count) in enumerate(ranking):
        member = guild.get_member(user_id)
        if member is not None:
            username = member.display_name
        else:
            user = bot.get_user(user_id)
            username = user.name if user else f"User {user_id}"
        medal = medals[index] if index < 3 else f"**{index + 1}.**"
        lines.append(
            f"{medal} **{discord.utils.escape_markdown(username)}**"
            f" — **{count:,}** tin nhắn"
        )
    embed.add_field(
        name="Bảng xếp hạng",
        value="\n".join(lines),
        inline=False
    )
    embed.set_footer(
        text="Chỉ tính tin nhắn người dùng còn trong lịch sử."
    )
    return embed
# =====================================================
# LỆNH /QUET
# =====================================================
@bot.tree.command(
    name="quet",
    description="Quét TOP 10 người nhắn tin nhiều nhất"
)
@app_commands.describe(
    kenh="Chọn kênh cần quét. Bỏ trống để quét toàn server."
)
async def quet(
    interaction: discord.Interaction,
    kenh: discord.TextChannel = None
):
    if not is_owner(interaction):
        await interaction.response.send_message(
            "❌ Bạn không có quyền sử dụng lệnh này.",
            ephemeral=True
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Lệnh này chỉ dùng được trong server.",
            ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=False, thinking=True)
    if kenh is not None:
        permissions = kenh.permissions_for(interaction.guild.me)
        if not (
            permissions.view_channel
            and permissions.read_message_history
        ):
            await interaction.followup.send(
                "❌ Bot không có quyền đọc lịch sử kênh này.",
                ephemeral=True
            )
            return
        channels = [kenh]
        scope_name = f"#{kenh.name}"
    else:
        channels = get_readable_channels(interaction.guild)
        scope_name = "Toàn server"
    if not channels:
        await interaction.followup.send(
            "❌ Không tìm thấy kênh nào có thể quét.",
            ephemeral=True
        )
        return
    progress = await interaction.followup.send(
        "🔎 Đang bắt đầu quét lịch sử tin nhắn...",
        wait=True
    )
    counter, scanned_channels = await scan_channels(
        channels,
        progress_message=progress
    )
    embed = make_top_embed(
        interaction.guild,
        counter,
        scope_name,
        scanned_channels
    )
    await progress.edit(
        content="✅ Đã hoàn tất quét lịch sử tin nhắn!",
        embed=embed
    )
# =====================================================
# LỆNH /SOTN
# =====================================================
class ScopeChoice(str):
    pass
@bot.tree.command(
    name="sotn",
    description="Kiểm tra số tin nhắn của một người"
)
@app_commands.describe(
    ten="Người dùng cần kiểm tra",
    pham_vi="Chọn all hoặc kenh",
    kenh="Chọn kênh nếu phạm vi là kenh"
)
@app_commands.choices(
    pham_vi=[
        app_commands.Choice(name="all - Toàn server", value="all"),
        app_commands.Choice(name="kenh - Một kênh", value="kenh")
    ]
)
async def sotn(
    interaction: discord.Interaction,
    ten: discord.Member,
    pham_vi: app_commands.Choice[str],
    kenh: discord.TextChannel = None
):
    if not is_owner(interaction):
        await interaction.response.send_message(
            "❌ Bạn không có quyền sử dụng lệnh này.",
            ephemeral=True
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Lệnh này chỉ dùng được trong server.",
            ephemeral=True
        )
        return
    if ten.bot:
        await interaction.response.send_message(
            "❌ Không thể thống kê tài khoản bot.",
            ephemeral=True
        )
        return
    if pham_vi.value == "kenh":
        if kenh is None:
            await interaction.response.send_message(
                "❌ Bạn cần chọn kênh khi phạm vi là kenh.",
                ephemeral=True
            )
            return
        permissions = kenh.permissions_for(interaction.guild.me)
        if not (
            permissions.view_channel
            and permissions.read_message_history
        ):
            await interaction.response.send_message(
                "❌ Bot không có quyền đọc lịch sử kênh này.",
                ephemeral=True
            )
            return
        channels = [kenh]
        scope_name = f"#{kenh.name}"
    else:
        channels = get_readable_channels(interaction.guild)
        scope_name = "Toàn server"
    if not channels:
        await interaction.response.send_message(
            "❌ Không tìm thấy kênh nào có thể quét.",
            ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=False, thinking=True)
    progress = await interaction.followup.send(
        f"🔎 Đang kiểm tra tin nhắn của {ten.display_name}...",
        wait=True
    )
    counter, scanned_channels = await scan_channels(
        channels,
        target_user_id=ten.id,
        progress_message=progress
    )
    message_count = counter.get(ten.id, 0)
    # Tính thứ hạng của người đó trong phạm vi đã quét
    # Chỉ lấy các tin nhắn đã thống kê được trong phạm vi này.
    if pham_vi.value == "all":
        ranking_counter, _ = await scan_channels(
            channels,
            progress_message=None
        )
    else:
        ranking_counter = Counter()
        try:
            async for message in kenh.history(
                limit=None,
                oldest_first=False
            ):
                if message.author.bot or message.webhook_id is not None:
                    continue
                ranking_counter[message.author.id] += 1
                if sum(ranking_counter.values()) % BATCH_SIZE == 0:
                    await asyncio.sleep(0)
        except (discord.Forbidden, discord.HTTPException):
            pass
    sorted_users = ranking_counter.most_common()
    rank = next(
        (
            index
            for index, (user_id, _) in enumerate(sorted_users, start=1)
            if user_id == ten.id
        ),
        None
    )
    embed = discord.Embed(
        title="📊 THỐNG KÊ TIN NHẮN",
        color=discord.Color.green()
    )
    embed.add_field(
        name="👤 Người dùng",
        value=ten.mention,
        inline=False
    )
    embed.add_field(
        name="💬 Tổng số tin nhắn",
        value=f"**{message_count:,}**",
        inline=True
    )
    embed.add_field(
        name="🏆 Xếp hạng",
        value=f"**#{rank}**" if rank else "Chưa xếp hạng",
        inline=True
    )
    embed.add_field(
        name="📍 Phạm vi",
        value=scope_name,
        inline=True
    )
    embed.add_field(
        name="🗂️ Số kênh đã quét",
        value=str(scanned_channels),
        inline=True
    )
    embed.set_footer(
        text="Thống kê lịch sử tin nhắn mà bot có thể truy cập."
    )
    await progress.edit(
        content="✅ Đã hoàn tất thống kê!",
        embed=embed
    )
# =====================================================
# ĐỒNG BỘ SLASH COMMAND
# =====================================================
@bot.event
async def on_ready():
    logging.info("Bot đã đăng nhập: %s", bot.user)
    try:
        synced = await bot.tree.sync()
        logging.info(
            "Đã đồng bộ %s slash command.",
            len(synced)
        )
    except Exception:
        logging.exception("Không thể đồng bộ slash command.")
# =====================================================
# KHỞI ĐỘNG
# =====================================================
if not TOKEN:
    raise RuntimeError(
        "Thiếu DISCORD_TOKEN trong Environment Variables."
    )
bot.run(TOKEN)