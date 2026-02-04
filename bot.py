import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3
import os
import base64
import time
import json
import secrets
import string
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from aiohttp import web

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# CONFIGURATION
SECRET_KEY = "PXHB_SECRET_KEY_8829" # MUST MATCH LUA SCRIPT
WEBHOOK_PORT = int(os.getenv('PORT', 8080))  # Dynamic port for Render/Railway/Fly
CUSTOMER_ROLE_ID = 1456538123629494335 # Customer Role ID
OWNER_ROLE_ID = 1456538170869944414 # Owner Role ID

# VERSION INFO
SCRIPT_VERSION = "2.2.0"
LAST_UPDATE = "February 04, 2026 5:30 PM EST"
CHANGELOG = [
    "Reverted to SQLite Database",
    "Ready for Railway Persistent Volume",
    "Fixed command timeouts"
]

# ==============================================================================
# DATABASE SETUP (SQLite)
# ==============================================================================
DB_NAME = "licenses.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Licenses table
    c.execute('''CREATE TABLE IF NOT EXISTS licenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id TEXT,
        discord_name TEXT,
        key TEXT UNIQUE,
        hwid TEXT,
        status TEXT,
        created_at INTEGER,
        expires_at INTEGER,
        activated_at INTEGER,
        last_hwid_reset INTEGER DEFAULT 0
    )''')
    # Sellers table
    c.execute('''CREATE TABLE IF NOT EXISTS sellers (
        discord_id TEXT PRIMARY KEY,
        discord_name TEXT,
        added_at INTEGER
    )''')
    conn.commit()
    conn.close()
    print(f"[System] Database {DB_NAME} initialized.")

# Initialize DB on start
init_db()

def get_db():
    return sqlite3.connect(DB_NAME)

# ==============================================================================
# ROLE HELPERS
# ==============================================================================
async def check_and_update_role(guild, user_id):
    """Checks if a user has any valid licenses and updates their role accordingly."""
    try:
        current_time = int(time.time())
        conn = get_db()
        c = conn.cursor()
        
        c.execute('''SELECT 1 FROM licenses 
                     WHERE discord_id = ? AND status != 'revoked' AND expires_at > ?''', 
                  (str(user_id), current_time))
        
        has_active = c.fetchone() is not None
        conn.close()
        
        member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
        if not member:
            return
            
        role = guild.get_role(CUSTOMER_ROLE_ID)
        if not role:
            print(f"[Role Error] Role ID {CUSTOMER_ROLE_ID} not found in guild.")
            return

        if has_active:
            if role not in member.roles:
                await member.add_roles(role)
                print(f"[Role] Assigned role to {member.name}")
        else:
            if role in member.roles:
                await member.remove_roles(role)
                print(f"[Role] Removed role from {member.name} (No active licenses)")
                
    except Exception as e:
        print(f"[Role Error] Failed to update role for {user_id}: {e}")

# ==============================================================================
# CRYPTO LOGIC (Custom XOR Cipher)
# ==============================================================================
def encrypt_string(text, key):
    result = []
    key_len = len(key)
    for i, char in enumerate(text):
        key_char = key[i % key_len]
        encrypted_char = chr(ord(char) ^ ord(key_char))
        result.append(encrypted_char)
    return "".join(result)

def generate_license(hwid, days):
    if days >= 999:
        expiry_timestamp = 9999999999
    else:
        expiry_timestamp = int(time.time()) + (days * 86400)
    
    # Add random salt to ensure uniqueness
    salt = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
    payload = f"{hwid}|{expiry_timestamp}|{salt}"
    
    encrypted_payload = encrypt_string(payload, SECRET_KEY)
    b64_bytes = base64.b64encode(encrypted_payload.encode('utf-8'))
    license_key = b64_bytes.decode('utf-8')
    
    return license_key, expiry_timestamp

# ==============================================================================
# WEBHOOK SERVER (Receives HWID from Lua)
# ==============================================================================
async def handle_activation(request):
    try:
        data = await request.json()
        key = data.get('key')
        hwid = data.get('hwid')
        
        if not key or not hwid:
            return web.json_response({'success': False, 'error': 'Missing key or hwid'}, status=400)
        
        conn = get_db()
        c = conn.cursor()
        
        # Check if key exists and is pending
        c.execute("SELECT status FROM licenses WHERE key = ?", (key,))
        row = c.fetchone()
        
        if not row:
            conn.close()
            return web.json_response({'success': False, 'error': 'Invalid Key'}, status=404)
            
        if row[0] != 'pending':
            conn.close()
            return web.json_response({'success': False, 'error': 'Key alread activated or revoked'}, status=400)
            
        # Activate
        timestamp = int(time.time())
        c.execute("UPDATE licenses SET hwid = ?, status = 'active', activated_at = ? WHERE key = ?", 
                  (hwid, timestamp, key))
        conn.commit()
        conn.close()
        
        return web.json_response({'success': True, 'message': 'Key activated successfully'})
    
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)}, status=500)

async def start_webhook_server():
    app = web.Application()
    app.router.add_post('/activate', handle_activation)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT)
    await site.start()
    print(f'Webhook server running on port {WEBHOOK_PORT}')

# ==============================================================================
# BOT SETUP
# ==============================================================================
intents = discord.Intents.default()
intents.members = True 
bot = commands.Bot(command_prefix="!", intents=intents)

# Periodically check for expired licenses
@tasks.loop(minutes=30)
async def check_expirations():
    print("[Task] Checking for license expirations...")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT DISTINCT discord_id FROM licenses")
        users = [row[0] for row in c.fetchall()]
        conn.close()
            
        for guild in bot.guilds:
            for user_id in users:
                await check_and_update_role(guild, user_id)
    except Exception as e:
        print(f"[Task Error] Error in check_expirations: {e}")

# ==============================================================================
# PERMISSION CHECKS
# ==============================================================================
def is_owner(interaction: discord.Interaction):
    role = interaction.guild.get_role(OWNER_ROLE_ID)
    return (role in interaction.user.roles if role else False) or interaction.user.guild_permissions.administrator

def is_seller(interaction: discord.Interaction):
    if is_owner(interaction):
        return True
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM sellers WHERE discord_id = ?", (str(interaction.user.id),))
    result = c.fetchone()
    conn.close()
    return result is not None

# ==============================================================================
# SELLERS MANAGEMENT
# ==============================================================================

@bot.tree.command(name="addseller", description="Add a new authorized seller (Owner Only)")
async def addseller(interaction: discord.Interaction, user: discord.Member):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owners only.", ephemeral=True)
        return
        
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO sellers (discord_id, discord_name, added_at) VALUES (?, ?, ?)",
                  (str(user.id), str(user), int(time.time())))
        conn.commit()
        conn.close()
        await interaction.response.send_message(f"✅ {user.mention} is now a Seller.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)

@bot.tree.command(name="removeseller", description="Remove an authorized seller (Owner Only)")
async def removeseller(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owners only.", ephemeral=True)
        return
        
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM sellers WHERE discord_id = ?", (str(user.id),))
        rows = c.rowcount
        conn.commit()
        conn.close()
        
        if rows > 0:
            await interaction.response.send_message(f"✅ Removed {user.mention} from sellers.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {user.mention} was not a seller.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)

# ==============================================================================
# SLASH COMMANDS
# ==============================================================================

@bot.tree.command(name="genkey", description="Generate a license key")
async def genkey(interaction: discord.Interaction, user: discord.User, days: int = 30):
    if not is_seller(interaction):
        await interaction.response.send_message("❌ Sellers only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    
    try:
        key, expiry = generate_license("UNBOUND", days)
        
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO licenses 
                     (discord_id, discord_name, key, hwid, status, created_at, expires_at) 
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (str(user.id), str(user), key, "UNBOUND", "pending", int(time.time()), expiry))
        conn.commit()
        conn.close()
        
        await check_and_update_role(interaction.guild, user.id)
        
        expiry_str = "Lifetime" if days >= 999 else f"<t:{expiry}:R>"
            
        embed = discord.Embed(title="License Generated", color=0x00ff00)
        embed.add_field(name="User", value=user.mention, inline=False)
        embed.add_field(name="Duration", value=f"{days} Days ({expiry_str})", inline=True)
        embed.add_field(name="Key", value=f"```{key}```", inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        try:
            await user.send(f"Here is your key: `{key}`")
        except:
            pass
        
    except Exception as e:
        await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="userinfo", description="View user licenses")
async def userinfo(interaction: discord.Interaction, user: discord.User):
    if not is_seller(interaction):
        await interaction.response.send_message("❌ Sellers only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT key, hwid, status, expires_at FROM licenses WHERE discord_id = ? ORDER BY created_at DESC", (str(user.id),))
    licenses = c.fetchall()
    conn.close()
    
    if not licenses:
        await interaction.followup.send("No licenses found.", ephemeral=True)
        return
    
    embed = discord.Embed(title=f"Licenses for {user.name}", color=0x5865F2)
    for row in licenses:
        key, hwid, status, expires = row
        exp_display = "Lifetime" if expires > 9999999990 else datetime.fromtimestamp(expires).strftime("%Y-%m-%d")
        embed.add_field(
            name=f"Status: {status}",
            value=f"Key: `{key}`\nHWID: `{hwid}`\nExpires: {exp_display}",
            inline=False
        )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="revoke", description="Revoke licenses")
async def revoke(interaction: discord.Interaction, user: discord.User):
    if not is_seller(interaction):
        await interaction.response.send_message("❌ Sellers only.", ephemeral=True)
        return
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE licenses SET status = 'revoked' WHERE discord_id = ? AND status != 'revoked'", (str(user.id),))
    count = c.rowcount
    conn.commit()
    conn.close()
    
    await check_and_update_role(interaction.guild, user.id)
    await interaction.response.send_message(f"Revoked {count} licenses for {user.mention}.", ephemeral=True)

@bot.tree.command(name="stats", description="License statistics")
async def stats(interaction: discord.Interaction):
    if not is_seller(interaction):
        await interaction.response.send_message("❌ Sellers only.", ephemeral=True)
        return
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM licenses GROUP BY status")
    rows = c.fetchall()
    total = sum(r[1] for r in rows)
    conn.close()
    
    stats_dict = {r[0]: r[1] for r in rows}
    
    embed = discord.Embed(title="Statistics", color=0x5865F2)
    embed.add_field(name="Total", value=str(total))
    embed.add_field(name="Active", value=str(stats_dict.get('active', 0)))
    embed.add_field(name="Pending", value=str(stats_dict.get('pending', 0)))
    embed.add_field(name="Revoked", value=str(stats_dict.get('revoked', 0)))
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==============================================================================
# PANEL
# ==============================================================================
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="Login", emoji="🔑", style=discord.ButtonStyle.primary, custom_id="panel_login")
    async def login_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT status, expires_at, hwid FROM licenses WHERE discord_id = ? ORDER BY created_at DESC", (str(interaction.user.id),))
        licenses = c.fetchall()
        conn.close()
        
        if not licenses:
            await interaction.response.send_message("No licenses found.", ephemeral=True)
            return
            
        embed = discord.Embed(title="Your Licenses", color=0x00ff00)
        for status, expires, hwid in licenses:
            exp_str = "Lifetime" if expires > 9999999990 else datetime.fromtimestamp(expires).strftime("%Y-%m-%d")
            embed.add_field(name=status.upper(), value=f"Expires: {exp_str}\nHWID: {hwid}", inline=False)
            
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Get Script", emoji="📜", style=discord.ButtonStyle.primary, custom_id="panel_getscript")
    async def getscript_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_time = int(time.time())
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT key FROM licenses WHERE discord_id = ? AND status != 'revoked' AND expires_at > ? ORDER BY created_at DESC LIMIT 1", 
                  (str(interaction.user.id), current_time))
        row = c.fetchone()
        conn.close()
        
        if not row:
            await interaction.response.send_message("❌ No active license found.", ephemeral=True)
            return
            
        key = row[0]
        script_url = "https://raw.githubusercontent.com/Americanbreathing/Keypsal/main/PXHV_Scripts/PXHB_FF2_Obfuscated.lua"
        loader = f'_G.LicenseKey = "{key}"\nloadstring(game:HttpGet("{script_url}"))()'
        
        await interaction.response.send_message(f"```lua\n{loader}\n```\n⚠️ **Do not share this key!**", ephemeral=True)

    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.primary, custom_id="panel_stats")
    async def stats_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM licenses WHERE discord_id = ?", (str(interaction.user.id),))
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM licenses WHERE discord_id = ? AND status = 'active'", (str(interaction.user.id),))
        active = c.fetchone()[0]
        conn.close()
        
        embed = discord.Embed(title="Your Stats", description=f"Total Licenses: {total}\nActive: {active}", color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="HWID Reset", emoji="🖥️", style=discord.ButtonStyle.primary, custom_id="panel_hwidreset")
    async def hwidreset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_time = int(time.time())
        cooldown = 7 * 86400
        
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, last_hwid_reset FROM licenses WHERE discord_id = ? AND status != 'revoked' AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
                  (str(interaction.user.id), current_time))
        row = c.fetchone()
        
        if not row:
            conn.close()
            await interaction.response.send_message("❌ No active license.", ephemeral=True)
            return
            
        lic_id, last_reset = row
        
        if (current_time - last_reset) < cooldown:
            days_left = (cooldown - (current_time - last_reset)) // 86400
            conn.close()
            await interaction.response.send_message(f"❌ Cooldown active. Try again in {days_left} days.", ephemeral=True)
            return
            
        c.execute("UPDATE licenses SET hwid = 'UNBOUND', last_hwid_reset = ? WHERE id = ?", (current_time, lic_id))
        conn.commit()
        conn.close()
        
        await interaction.response.send_message("✅ HWID Reset successful!", ephemeral=True)

@bot.tree.command(name="panel", description="Send Panel")
async def panel(interaction: discord.Interaction):
    if is_owner(interaction):
        await interaction.response.send_message("PX HB Control Panel", view=PanelView())
    else:
        await interaction.response.send_message("Owners only.", ephemeral=True)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    bot.add_view(PanelView())
    await bot.tree.sync()
    check_expirations.start()
    asyncio.create_task(start_webhook_server())

if __name__ == "__main__":
    bot.run(TOKEN)
