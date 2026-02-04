import discord
from discord import app_commands
from discord.ext import commands, tasks
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
from pymongo import MongoClient

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')

# CONFIGURATION
SECRET_KEY = "PXHB_SECRET_KEY_8829" # MUST MATCH LUA SCRIPT
WEBHOOK_PORT = int(os.getenv('PORT', 8080))  # Dynamic port for Render/Railway/Fly
CUSTOMER_ROLE_ID = 1456538123629494335 # Customer Role ID
OWNER_ROLE_ID = 1456538170869944414 # Owner Role ID

# VERSION INFO (Update these when pushing new script)
SCRIPT_VERSION = "2.2.0"  # Bumped version for MongoDB update
LAST_UPDATE = "February 04, 2026 4:45 PM EST"
CHANGELOG = [
    "Migrated database to MongoDB (Cloud Persistent)",
    "Fixed persistent storage issues on free hosting",
    "Optimized license lookup speed"
]

# ==============================================================================
# MONGODB SETUP
# ==============================================================================
if not MONGODB_URI:
    print("Warning: MONGODB_URI not found in .env. Bot will fail to connect to DB.")

# Initialize MongoDB Client
try:
    # Use tls=true without certifi to avoid SSL issues on some platforms
    mongo_client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = mongo_client['pxhb_bot']
    licenses_col = db['licenses']
    sellers_col = db['sellers']
    print("[System] Connected to MongoDB Atlas successfully!")
except Exception as e:
    print(f"[System] Failed to connect to MongoDB: {e}")

# ==============================================================================
# ROLE HELPERS
# ==============================================================================
async def check_and_update_role(guild, user_id):
    """Checks if a user has any valid licenses and updates their role accordingly."""
    try:
        current_time = int(time.time())
        user_id_str = str(user_id)
        
        # Check for any active, non-expired license
        active_license = licenses_col.find_one({
            "discord_id": user_id_str,
            "status": {"$ne": "revoked"},
            "expires_at": {"$gt": current_time}
        })
        
        has_active = (active_license is not None)
        
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
        
        # Update database
        result = licenses_col.update_one(
            {"key": key, "status": "pending"},
            {"$set": {"hwid": hwid, "status": "active", "activated_at": int(time.time())}}
        )
            
        if result.modified_count == 0:
            return web.json_response({'success': False, 'error': 'Key not found or already activated'}, status=404)
        
        return web.json_response({'success': True, 'message': 'Key activated successfully'})
    
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)}, status=500)

async def start_webhook_server():
    app = web.Application()
    app.router.add_post('/activate', handle_activation)
    runner = web.AppRunner(app)
    await runner.setup()
    # Listen on all interfaces
    site = web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT)
    await site.start()
    print(f'Webhook server running on port {WEBHOOK_PORT}')

# ==============================================================================
# BOT SETUP
# ==============================================================================
intents = discord.Intents.default()
intents.members = True 
bot = commands.Bot(command_prefix="!", intents=intents)

# Periodically check for expired licenses and remove roles
@tasks.loop(minutes=30)
async def check_expirations():
    print("[Task] Checking for license expirations...")
    try:
        # Get all unique discord IDs in the database
        users = licenses_col.distinct("discord_id")
            
        for guild in bot.guilds:
            for user_id in users:
                await check_and_update_role(guild, user_id)
    except Exception as e:
        print(f"[Task Error] Error in check_expirations: {e}")

# ==============================================================================
# PERMISSION CHECKS
# ==============================================================================
def is_owner(interaction: discord.Interaction):
    """Checks if the user has the Owner role."""
    role = interaction.guild.get_role(OWNER_ROLE_ID)
    return (role in interaction.user.roles if role else False) or interaction.user.guild_permissions.administrator

async def is_seller(interaction: discord.Interaction):
    """Checks if the user is a registered seller or an admin/owner."""
    if is_owner(interaction):
        return True
    
    seller = sellers_col.find_one({"discord_id": str(interaction.user.id)})
    return seller is not None

# ==============================================================================
# SELLERS MANAGEMENT (OWNER ONLY)
# ==============================================================================

@bot.tree.command(name="addseller", description="Add a new authorized seller (Owner Only)")
@app_commands.describe(user="The Discord user to authorize as a seller")
async def addseller(interaction: discord.Interaction, user: discord.Member):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ This command is restricted to **Owners**.", ephemeral=True)
        return
        
    try:
        sellers_col.update_one(
            {"discord_id": str(user.id)},
            {"$set": {"discord_name": str(user), "added_at": int(time.time())}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ {user.mention} is now an authorized **Seller**.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)

@bot.tree.command(name="removeseller", description="Remove an authorized seller (Owner Only)")
@app_commands.describe(user="The Discord user to remove from sellers")
async def removeseller(interaction: discord.Interaction, user: discord.User):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ This command is restricted to **Owners**.", ephemeral=True)
        return
        
    try:
        result = sellers_col.delete_one({"discord_id": str(user.id)})
            
        if result.deleted_count > 0:
            await interaction.response.send_message(f"✅ Removed {user.mention} from authorized sellers.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {user.mention} was not a registered seller.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)

# ==============================================================================
# SLASH COMMANDS
# ==============================================================================

@bot.tree.command(name="genkey", description="Generate a license key for a user")
@app_commands.describe(user="The Discord user to generate a key for", days="Duration in days (999 for lifetime)")
async def genkey(interaction: discord.Interaction, user: discord.User, days: int = 30):
    if not await is_seller(interaction):
        await interaction.response.send_message("❌ Access Denied: You are not an authorized **Seller**.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Generate UNBOUND key
        key, expiry = generate_license("UNBOUND", days)
        
        # Store in database
        doc = {
            "discord_id": str(user.id),
            "discord_name": str(user),
            "key": key,
            "hwid": "UNBOUND",
            "status": "pending",
            "created_at": int(time.time()),
            "expires_at": expiry,
            "last_hwid_reset": 0
        }
        licenses_col.insert_one(doc)
        
        # Add Customer Role immediately
        await check_and_update_role(interaction.guild, user.id)
        
        # Format response
        if days >= 999:
            expiry_str = "Lifetime"
        else:
            expiry_str = f"<t:{expiry}:R>"
            
        embed = discord.Embed(title="License Generated", color=0x00ff00)
        embed.add_field(name="User", value=user.mention, inline=False)
        embed.add_field(name="Duration", value=f"{days} Days ({expiry_str})", inline=True)
        embed.add_field(name="Status", value="⏳ Pending Activation", inline=True)
        embed.add_field(name="License Key", value=f"```{key}```", inline=False)
        embed.set_footer(text="User has been assigned the Customer role.")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        # Optionally DM the user
        try:
            dm_embed = discord.Embed(title="Your PXHB License Key", color=0x00ff00)
            dm_embed.add_field(name="Key", value=f"```{key}```", inline=False)
            dm_embed.add_field(name="Expires", value=expiry_str, inline=True)
            dm_embed.set_footer(text="Paste this key into the script to activate.")
            await user.send(embed=dm_embed)
        except:
            pass  # User has DMs disabled
        
    except Exception as e:
        await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="userinfo", description="View a user's license information")
@app_commands.describe(user="The Discord user to check")
async def userinfo(interaction: discord.Interaction, user: discord.User):
    if not await is_seller(interaction):
        await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Find all licenses for user
        cursor = licenses_col.find({"discord_id": str(user.id)}).sort("created_at", -1)
        licenses = list(cursor)
        
        if not licenses:
            await interaction.followup.send(f"{user.mention} has no licenses.", ephemeral=True)
            return
        
        embed = discord.Embed(title=f"Licenses for {user.name}", color=0x5865F2)
        
        for lic in licenses:
            status = lic.get('status', 'pending')
            hwid = lic.get('hwid', '')
            key = lic.get('key', 'Unknown')
            expires_at = lic.get('expires_at', 0)
            
            status_emoji = {"pending": "⏳", "active": "✅", "revoked": "❌"}.get(status, "❓")
            hwid_display = hwid[:16] + "..." if hwid else "Not activated"
            expires = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at < 9999999999 else "Lifetime"
            
            embed.add_field(
                name=f"{status_emoji} {status.upper()}",
                value=f"**Key:** `{key[:20]}...`\n**HWID:** `{hwid_display}`\n**Expires:** {expires}",
                inline=False
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="revoke", description="Revoke a user's license")
@app_commands.describe(user="The Discord user whose license to revoke")
async def revoke(interaction: discord.Interaction, user: discord.User):
    if not await is_seller(interaction):
        await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    
    try:
        result = licenses_col.update_many(
            {"discord_id": str(user.id), "status": {"$ne": "revoked"}},
            {"$set": {"status": "revoked"}}
        )
            
        count = result.modified_count
            
        # Re-check and potentially remove role
        await check_and_update_role(interaction.guild, user.id)
        
        if count > 0:
            await interaction.followup.send(f"✅ Revoked {count} license(s) for {user.mention}. Role updated.", ephemeral=True)
        else:
            await interaction.followup.send(f"{user.mention} has no active licenses to revoke.", ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="stats", description="View license statistics")
async def stats(interaction: discord.Interaction):
    if not await is_seller(interaction):
        await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    
    try:
        total = licenses_col.count_documents({})
        active = licenses_col.count_documents({"status": "active"})
        pending = licenses_col.count_documents({"status": "pending"})
        revoked = licenses_col.count_documents({"status": "revoked"})
        
        embed = discord.Embed(title="License Statistics", color=0x5865F2)
        embed.add_field(name="Total Keys", value=str(total), inline=True)
        embed.add_field(name="✅ Active", value=str(active), inline=True)
        embed.add_field(name="⏳ Pending", value=str(pending), inline=True)
        embed.add_field(name="❌ Revoked", value=str(revoked), inline=True)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="help", description="Show bot commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="PXHB Bot Commands", color=0x5865F2)
    embed.add_field(name="/genkey @user <days>", value="Generate a license (Sellers Only)", inline=False)
    embed.add_field(name="/userinfo @user", value="View user's licenses", inline=False)
    embed.add_field(name="/revoke @user", value="Revoke licenses", inline=False)
    embed.add_field(name="/stats", value="View license statistics", inline=False)
    
    if is_owner(interaction):
        embed.add_field(name="--- OWNER ONLY ---", value="\u200b", inline=False)
        embed.add_field(name="/addseller @user", value="Authorize a user to sell keys", inline=False)
        embed.add_field(name="/removeseller @user", value="Revoke seller permissions", inline=False)
        
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Note: Backup command removed as downloading a .db file is no longer relevant for MongoDB
# You can use MongoDB Compass or Atlas UI to export data if needed.

# ==============================================================================
# INTERACTIVE PANEL (Replaces Web Portal)
# ==============================================================================
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Never timeout
    
    @discord.ui.button(label="Login", emoji="🔑", style=discord.ButtonStyle.primary, custom_id="panel_login")
    async def login_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = str(interaction.user.id)
            current_time = int(time.time())
            
            cursor = licenses_col.find({"discord_id": user_id}).sort("created_at", -1)
            licenses = list(cursor)
            
            if not licenses:
                embed = discord.Embed(
                    title="❌ No License Found",
                    description="You don't have any licenses.\nContact an admin to get one!",
                    color=0xFF5555
                )
            else:
                embed = discord.Embed(title="🔑 Your Licenses", color=5814783)
                for lic in licenses:
                    status = lic.get('status', 'unknown')
                    expires_at = lic.get('expires_at', 0)
                    hwid = lic.get('hwid', '')
                    
                    expires_str = datetime.fromtimestamp(expires_at).strftime('%Y-%m-%d') if expires_at else "N/A"
                    is_expired = expires_at < current_time
                    status_display = "⏰ EXPIRED" if is_expired else f"✅ {status.upper()}"
                    hwid_display = hwid if hwid and hwid != "UNBOUND" else "Not activated"
                    
                    embed.add_field(
                        name=f"{status_display}",
                        value=f"**Expires:** {expires_str}\n**HWID:** `{hwid_display[:20]}...`" if len(str(hwid)) > 20 else f"**Expires:** {expires_str}\n**HWID:** `{hwid_display}`",
                        inline=False
                    )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label="Get Script", emoji="📜", style=discord.ButtonStyle.primary, custom_id="panel_getscript")
    async def getscript_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = str(interaction.user.id)
            current_time = int(time.time())
            
            # Find active license
            license_doc = licenses_col.find_one({
                "discord_id": user_id,
                "status": {"$ne": "revoked"},
                "expires_at": {"$gt": current_time}
            }, sort=[("created_at", -1)])
            
            if not license_doc:
                embed = discord.Embed(
                    title="❌ Access Denied",
                    description="You must have an **Active License** to get the script.\n\n1. Purchase a key\n2. Use `/genkey` (Admin)\n3. Click 'Login' to check status",
                    color=0xFF5555
                )
            else:
                key = license_doc['key']
                # TODO: UPDATE THIS URL TO YOUR RAW LUA SCRIPT URL
                script_url = "https://raw.githubusercontent.com/Americanbreathing/Keypsal/main/PXHV_Scripts/PXHB_FF2_Obfuscated.lua"
                
                script_loader = f'_G.LicenseKey = "{key}"\nloadstring(game:HttpGet("{script_url}"))()'
                
                embed = discord.Embed(
                    title="📜 Your PXHB Script",
                    description="Copy the code below and paste it into your executor.",
                    color=0x55FF55
                )
                embed.add_field(name="Script Loader", value=f"```lua\n{script_loader}\n```", inline=False)
                embed.add_field(name="⚠️ Warning", value="Do NOT share this! The key is linked to your hardware.", inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.primary, custom_id="panel_stats")
    async def stats_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = str(interaction.user.id)
            current_time = int(time.time())
            
            total = licenses_col.count_documents({"discord_id": user_id})
            active = licenses_col.count_documents({
                "discord_id": user_id, 
                "status": "active", 
                "expires_at": {"$gt": current_time}
            })
            
            latest = licenses_col.find_one(
                {"discord_id": user_id, "expires_at": {"$gt": current_time}},
                sort=[("expires_at", -1)]
            )
            
            if total == 0:
                embed = discord.Embed(
                    title="📊 Your Stats",
                    description="You have no licenses yet!",
                    color=5814783
                )
            else:
                embed = discord.Embed(title="📊 Your License Stats", color=5814783)
                embed.add_field(name="Total Licenses", value=str(total), inline=True)
                embed.add_field(name="Active", value=str(active), inline=True)
                
                if latest:
                    days_left = (latest['expires_at'] - current_time) // 86400
                    embed.add_field(name="Days Remaining", value=str(max(0, days_left)), inline=True)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label="HWID Reset", emoji="🖥️", style=discord.ButtonStyle.primary, custom_id="panel_hwidreset")
    async def hwidreset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            user_id = str(interaction.user.id)
            current_time = int(time.time())
            cooldown_days = 7
            cooldown_seconds = cooldown_days * 24 * 60 * 60
            
            # Find most recent active license
            license_doc = licenses_col.find_one({
                "discord_id": user_id,
                "status": {"$ne": "revoked"},
                "expires_at": {"$gt": current_time}
            }, sort=[("created_at", -1)])
            
            if not license_doc:
                embed = discord.Embed(
                    title="❌ No Active License",
                    description="You need an active license to reset HWID.",
                    color=0xFF5555
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            last_reset = license_doc.get('last_hwid_reset', 0)
            
            # Check cooldown
            if last_reset and (current_time - last_reset) < cooldown_seconds:
                remaining = cooldown_seconds - (current_time - last_reset)
                days = remaining // 86400
                hours = (remaining % 86400) // 3600
                embed = discord.Embed(
                    title="⏰ HWID Reset Cooldown",
                    description=f"You can reset your HWID in **{days}d {hours}h**.\n\nCooldown: {cooldown_days} days between resets.",
                    color=0xFFAA00
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Perform reset
            licenses_col.update_one(
                {"_id": license_doc["_id"]},
                {"$set": {"hwid": "UNBOUND", "last_hwid_reset": current_time}}
            )
            
            embed = discord.Embed(
                title="✅ HWID Reset Successful",
                description="Your HWID has been reset!\nLaunch the script on your new PC to bind it.",
                color=0x55FF55
            )
            embed.add_field(name="Next Reset Available", value=f"In {cooldown_days} days", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label="Version", emoji="ℹ️", style=discord.ButtonStyle.secondary, custom_id="panel_version")
    async def version_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            embed = discord.Embed(
                title="ℹ️ PXHB Script Version",
                color=0x5865F2
            )
            embed.add_field(name="Current Version", value=f"**v{SCRIPT_VERSION}**", inline=True)
            embed.add_field(name="Last Updated", value=LAST_UPDATE, inline=True)
            embed.add_field(name="Changelog", value="\n".join([f"• {item}" for item in CHANGELOG]), inline=False)
            embed.set_footer(text="If your script is outdated, click 'Get Script' again.")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="panel", description="Send the PXHB control panel embed (Owner Only)")
async def panel(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ This command is restricted to **Owners**.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="PX HB",
        description="\nAll licenses are HWID-bound, so don't try to share your script or YOU WILL BE BLACKLISTED.",
        color=5814783
    )
    
    view = PanelView()
    await interaction.response.send_message(embed=embed, view=view)

# Register persistent view on bot startup
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    
    # Register persistent view for panel buttons
    bot.add_view(PanelView())
    
    try:
        synced = await bot.tree.sync()
        print(f'Synced {len(synced)} commands')
    except Exception as e:
        print(f'Sync error: {e}')
    
    # Start background tasks
    check_expirations.start()
    asyncio.create_task(start_webhook_server())

# Run Bot
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Error: DISCORD_TOKEN not found in .env")
