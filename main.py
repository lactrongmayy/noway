import os
import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

# =========================================================
# CẤU HÌNH
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = 1356498767788118099
DAYS_TO_SCAN = 60
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
scan_lock = asyncio.Lock()


def fmt_num(number: int) -> str:
    return f"{number:,}".replace(",", ".")


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        logging.info("Đăng nhập: %s (%s)", bot.user, bot.user.id)
        logging.info("Đã đồng bộ %s lệnh slash.", len(synced))
    except Exception:
        logging.exception("Không thể đồng bộ slash command.")


@bot.tree.command(
    name="quet",
    description="Quét tin nhắn trong kênh hiện tại của 60 ngày gần nhất",
)
@app_commands.guild_only()
async def quet(interaction: discord.Interaction):
    # Chỉ chủ bot được sử dụng lệnh.
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(
            "❌ Bạn không có quyền sử dụng lệnh này!",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ Lệnh này chỉ dùng được trong kênh văn bản thông thường.",
            ephemeral=True,
        )
        return

    channel = interaction.channel
    guild = interaction.guild

    if scan_lock.locked():
        await interaction.response.send_message(
            "⏳ Bot đang quét một kênh khác. Vui lòng đợi lượt đó hoàn tất.",
            ephemeral=True,
        )
        return

    # Trả lời interaction ngay để tránh Discord báo hết thời gian chờ.
    await interaction.response.send_message(
        f"✅ Đã bắt đầu quét tin nhắn trong {channel.mention}.",
        ephemeral=True,
    )

    async with scan_lock:
        progress = await channel.send(
            f"🔎 **ĐANG QUÉT TIN NHẮN**\n"
            f"📍 Kênh: {channel.mention}\n"
            f"📅 Tiến trình: **0/{DAYS_TO_SCAN}**"
        )

        # Quét 60 ngày lịch theo múi giờ Việt Nam, tính cả ngày hôm nay.
        now = datetime.now(LOCAL_TZ)
        today = now.date()
        first_day = today - timedelta(days=DAYS_TO_SCAN - 1)

        # user_id -> tổng số tin nhắn
        totals = Counter()
        # (ngày, user_id) -> số tin nhắn trong ngày
        daily_counts = defaultdict(int)
        display_names = {}

        scanned_messages = 0

        try:
            for day_index in range(DAYS_TO_SCAN):
                current_date = first_day + timedelta(days=day_index)
                day_start = datetime.combine(
                    current_date, time.min, tzinfo=LOCAL_TZ
                )
                next_day_start = day_start + timedelta(days=1)
                day_end = min(next_day_start, now)

                # Đếm tin nhắn trong từng ngày; không cần đọc nội dung tin nhắn.
                async for message in channel.history(
                    after=day_start,
                    before=day_end,
                    oldest_first=True,
                    limit=None,
                ):
                    if message.author.bot:
                        continue

                    user_id = message.author.id
                    daily_counts[(current_date, user_id)] += 1
                    display_names[user_id] = message.author.display_name
                    scanned_messages += 1

                # Cập nhật một tin nhắn tiến trình duy nhất.
                await progress.edit(
                    content=(
                        f"🔎 **ĐANG QUÉT TIN NHẮN**\n"
                        f"📍 Kênh: {channel.mention}\n"
                        f"📅 Ngày đang xử lý: **{current_date.strftime('%d/%m/%Y')}**\n"
                        f"⏳ Tiến trình: **{day_index + 1}/{DAYS_TO_SCAN}**"
                    )
                )

            # Tổng hợp sau khi quét xong.
            await progress.edit(
                content=(
                    f"✅ **ĐÃ QUÉT XONG {DAYS_TO_SCAN} NGÀY**\n"
                    f"📍 Kênh: {channel.mention}\n"
                    f"🧮 **Đang cộng số tin nhắn và xếp hạng...**"
                )
            )

            # Chỉ bắt đầu cộng tổng sau khi đã quét xong đủ 60 ngày.
            totals = Counter()
            for (date_key, user_id), message_count in daily_counts.items():
                totals[user_id] += message_count

            top_10 = totals.most_common(10)
            total_messages = sum(totals.values())
            people_count = len(totals)

            lines = [
                f"🏆 **TOP 10 NGƯỜI CHAT NHIỀU NHẤT — {DAYS_TO_SCAN} NGÀY**",
                f"📍 Kênh: {channel.mention}",
                f"📅 Khoảng ngày: **{first_day.strftime('%d/%m/%Y')} – {today.strftime('%d/%m/%Y')}**",
                f"👥 Số người đã chat: **{fmt_num(people_count)}**",
                f"💬 Tổng tin nhắn: **{fmt_num(total_messages)}**",
                "",
            ]

            medals = ["🥇", "🥈", "🥉"]
            if not top_10:
                lines.append("Không tìm thấy tin nhắn nào của thành viên trong khoảng thời gian này.")
            else:
                for rank, (user_id, count) in enumerate(top_10, start=1):
                    medal = medals[rank - 1] if rank <= 3 else f"**{rank}.**"
                    name = display_names.get(user_id, f"Người dùng {user_id}")
                    lines.append(
                        f"{medal} <@{user_id}> — **{fmt_num(count)}** tin nhắn"
                    )

            result = "\n".join(lines)

            # Giữ dưới giới hạn 2000 ký tự của Discord.
            if len(result) <= 2000:
                await progress.edit(content=result)
            else:
                await progress.edit(
                    content=(
                        f"✅ **ĐÃ QUÉT XONG {DAYS_TO_SCAN} NGÀY**\n"
                        f"📍 Kênh: {channel.mention}\n"
                        f"👥 Số người đã chat: **{fmt_num(people_count)}**\n"
                        f"💬 Tổng tin nhắn: **{fmt_num(total_messages)}**\n\n"
                        + "\n".join(lines[-10:])
                    )[:2000]
                )

            # DM chủ bot chỉ một lời nhắc ngắn, không gửi báo cáo dài.
            try:
                owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
                await owner.send(f"<@{OWNER_ID}> ✅ Bot đã quét xong!")
            except (discord.Forbidden, discord.HTTPException):
                logging.warning("Không gửi được DM cho chủ bot.")

            logging.info(
                "Quét xong guild=%s channel=%s messages=%s users=%s",
                guild.id if guild else "unknown",
                channel.id,
                scanned_messages,
                people_count,
            )

        except discord.Forbidden:
            await progress.edit(
                content=(
                    "❌ Bot không có quyền đọc lịch sử tin nhắn của kênh này.\n"
                    "Hãy cấp quyền **View Channel** và **Read Message History**."
                )
            )
        except discord.HTTPException:
            logging.exception("Lỗi Discord API khi quét kênh.")
            try:
                await progress.edit(
                    content="❌ Có lỗi từ Discord khi quét. Hãy kiểm tra quyền bot và console Railway."
                )
            except discord.HTTPException:
                pass
        except Exception:
            logging.exception("Lỗi không mong muốn khi quét.")
            try:
                await progress.edit(
                    content="❌ Quét thất bại do lỗi không mong muốn. Hãy kiểm tra console Railway."
                )
            except discord.HTTPException:
                pass


if not TOKEN:
    raise RuntimeError(
        "Thiếu biến môi trường DISCORD_TOKEN. Hãy thêm token trong Railway Variables."
    )

bot.run(TOKEN)
