import os
import threading
import datetime
import collections
from flask import Flask
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# --- 1. Render 웹 서비스 인식용 가짜 Web Server (Flask) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    # Render가 지정해주는 PORT 환경 변수를 사용합니다.
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# 백그라운드 스레드로 Flask 서버 실행
threading.Thread(target=run_flask, daemon=True).start()

# --- 2. 디스코드 봇 설정 ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("오류: DISCORD_TOKEN 환경 변수가 설정되지 않았습니다.")
    exit(1)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.moderation = True  
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

whitelist = {}
security_enabled = {}
action_tracker = collections.defaultdict(lambda: collections.defaultdict(list))

def is_whitelisted(guild_id: int, user_id: int, owner_id: int) -> bool:
    if user_id == owner_id:
        return True
    return user_id in whitelist.get(guild_id, set())

def is_suspicious_activity(guild_id: int, user_id: int, action_type: str, threshold: int = 3, timeframe: int = 5) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    user_actions = action_tracker[guild_id][(user_id, action_type)]
    
    user_actions = [t for t in user_actions if now - t < timeframe]
    user_actions.append(now)
    action_tracker[guild_id][(user_id, action_type)] = user_actions
    
    return len(user_actions) >= threshold

def get_default_log_channel(guild: discord.Guild) -> discord.TextChannel:
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            return channel
            
    return None

async def punish_and_log(guild: discord.Guild, user: discord.Member, reason: str, details: str):
    try:
        duration = datetime.timedelta(hours=10)
        await user.timeout(duration, reason=f"[보안봇 자동제재] {reason}")
        
        log_channel = get_default_log_channel(guild)
        if log_channel:
            embed = discord.Embed(
                title="🚨 보안 위협 감지 및 자동 제재",
                description=f"비동기적 테러 행위가 감지되어 **10시간 타임아웃** 조치되었습니다.",
                color=discord.Color.red(),
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            embed.add_field(name="👤 제재 대상", value=f"{user.mention} (`{user}`)", inline=False)
            embed.add_field(name="📝 감지 사유", value=reason, inline=False)
            embed.add_field(name="📋 상세 정보", value=details, inline=False)
            embed.set_footer(text=f"Server ID: {guild.id}")
            
            await log_channel.send(embed=embed)
                
    except discord.Forbidden:
        print(f"[{guild.name}] 권한 부족")
    except Exception as e:
        print(f"오류: {e}")

@bot.event
async def on_ready():
    print(f"로그인 완료: {bot.user.name} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 {len(synced)}개 동기화 완료.")
    except Exception as e:
        print(f"동기화 실패: {e}")

@bot.tree.command(name="설정", description="서버 보안 감지 기능을 활성화합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_security(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    security_enabled[guild_id] = True
    
    if guild_id not in whitelist:
        whitelist[guild_id] = set()
        
    embed = discord.Embed(
        title="🛡️ 서버 보안 설정 완료",
        description="서버 자동 보안 모드가 **활성화**되었습니다.",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

whitelist_group = app_commands.Group(name="화이트리스트", description="화이트리스트 관리 명령어")

@whitelist_group.command(name="추가", description="보안 감지 예외 유저 추가")
@app_commands.checks.has_permissions(administrator=True)
async def whitelist_add(interaction: discord.Interaction, member: discord.Member):
    guild_id = interaction.guild_id
    if guild_id not in whitelist:
        whitelist[guild_id] = set()
    whitelist[guild_id].add(member.id)
    await interaction.response.send_message(f"✅ {member.mention} 님이 화이트리스트에 추가되었습니다.", ephemeral=True)

@whitelist_group.command(name="제거", description="보안 감지 예외 유저 제거")
@app_commands.checks.has_permissions(administrator=True)
async def whitelist_remove(interaction: discord.Interaction, member: discord.Member):
    guild_id = interaction.guild_id
    if guild_id in whitelist and member.id in whitelist[guild_id]:
        whitelist[guild_id].remove(member.id)
        await interaction.response.send_message(f"❌ {member.mention} 님이 화이트리스트에서 제거되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ 화이트리스트에 존재하지 않습니다.", ephemeral=True)

bot.tree.add_command(whitelist_group)

@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild
    if not security_enabled.get(guild.id, False):
        return

    async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_delete, limit=1):
        user = entry.user
        if not user or user.bot or is_whitelisted(guild.id, user.id, guild.owner_id):
            return

        if is_suspicious_activity(guild.id, user.id, "channel_delete", threshold=3, timeframe=5):
            member = guild.get_member(user.id)
            if member:
                deleted_channel_info = f"• 삭제된 채널: **#{channel.name}**"
                await punish_and_log(guild, member, "단시간 연속 채널 삭제 감지", deleted_channel_info)

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    guild = channel.guild
    if not security_enabled.get(guild.id, False):
        return

    async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_create, limit=1):
        user = entry.user
        if not user or user.bot or is_whitelisted(guild.id, user.id, guild.owner_id):
            return

        if is_suspicious_activity(guild.id, user.id, "channel_create", threshold=3, timeframe=5):
            member = guild.get_member(user.id)
            if member:
                created_channel_info = f"• 생성된 채널: **#{channel.name}**"
                await punish_and_log(guild, member, "단시간 연속 채널 생성 감지", created_channel_info)
            try:
                await channel.delete(reason="보안봇 자동 원상복구")
            except:
                pass

bot.run(TOKEN)