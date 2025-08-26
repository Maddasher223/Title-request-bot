# main.py
# TitleRequest Discord Bot with Flask Web Server for Render hosting

import discord
from discord.ext import commands, tasks
import os
import json
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from flask import Flask
from threading import Thread
import typing # Used for type hinting

# --- Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')

# --- Constants & Configuration ---
STATE_FILE = 'titles_state.json'
LOG_FILE = 'log.json'
DEFAULT_TITLES = ['Governor', 'Architect', 'Prefect', 'General']
MAX_LOG_ENTRIES = 2000 # Max global history entries to keep

# --- Bot Intents ---
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or('!'), intents=intents)

# --- Helper Functions ---

def load_state():
    """Loads state from STATE_FILE. Returns default if file not found."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    else:
        # Default state structure
        return {
            "titles": {},
            "config": {
                "min_hold_minutes": 60,
                "announce_channel_id": None,
                "guardians": [],
                "remind_every_minutes": 15,
                "max_reminders": 3
            }
        }

def save_state(state):
    """Safely saves state to STATE_FILE via a temporary file."""
    # Note: Render has an ephemeral filesystem. For true persistence, a database would be needed.
    # This implementation will lose state on service restarts.
    temp_file = f"{STATE_FILE}.tmp"
    with open(temp_file, 'w') as f:
        json.dump(state, f, indent=4)
    os.replace(temp_file, STATE_FILE)

def log_event(title_name, action, previous_holder_id=None, new_holder_id=None, queue_snapshot=None, notes=None):
    """Appends an event to the LOG_FILE and handles log rotation."""
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w') as f:
            json.dump([], f)

    with open(LOG_FILE, 'r+') as f:
        log_data = json.load(f)
        
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "title": title_name,
            "action": action,
            "previous_holder_id": previous_holder_id,
            "new_holder_id": new_holder_id,
            "queue_snapshot": queue_snapshot or [],
            "notes": notes
        }
        log_data.append(event)
        
        # Trim old log entries if log is too large
        if len(log_data) > MAX_LOG_ENTRIES:
            log_data = log_data[-MAX_LOG_ENTRIES:]
            
        f.seek(0)
        json.dump(log_data, f, indent=4)
        f.truncate()

def format_timedelta(td: timedelta):
    """Formats a timedelta into a human-readable string like '1h 23m' or '45m'."""
    if not td:
        return "0m"
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

# --- Bot Cog ---

class TitleCog(commands.Cog, name="TitleRequest"):
    def __init__(self, bot):
        self.bot = bot
        self.state = load_state()
        self.check_due_changes.start()

    def cog_unload(self):
        self.check_due_changes.cancel()

    async def cog_command_error(self, ctx, error):
        """Global command error handler."""
        if isinstance(error, commands.CommandNotFound):
            return
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Missing argument. Usage: `{ctx.prefix}{ctx.command.signature}`")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("🚫 You don't have permission to use this command.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"⚠️ Invalid argument provided. Please check the command usage.")
        else:
            logging.error(f"An unhandled error occurred in command '{ctx.command}': {error}")
            await ctx.send("An unexpected error occurred. Please contact an administrator.")

    # --- Checks ---
    async def is_guardian_or_admin(self, ctx):
        """Check if the user is a guardian or has admin-level permissions."""
        if ctx.author.guild_permissions.manage_roles:
            return True
        guardian_ids = self.state['config'].get('guardians', [])
        if ctx.author.id in guardian_ids:
            return True
        if any(role.id in guardian_ids for role in ctx.author.roles):
            return True
        return False
        
    # --- Background Task ---
    @tasks.loop(seconds=60)
    async def check_due_changes(self):
        await self.bot.wait_until_ready()
        announce_channel_id = self.state['config']['announce_channel_id']
        if not announce_channel_id:
            return

        channel = self.bot.get_channel(announce_channel_id)
        if not channel:
            logging.warning(f"Announce channel with ID {announce_channel_id} not found.")
            return

        now = datetime.now(timezone.utc)
        
        for title_name, title_data in self.state['titles'].items():
            # Condition 1: Check for newly due changes
            if not title_data.get('change_due') and title_data['holder_id'] and title_data['queue']:
                claimed_at = datetime.fromisoformat(title_data['claimed_at'])
                min_hold = timedelta(minutes=self.state['config']['min_hold_minutes'])
                snoozed_until_str = title_data.get('snoozed_until')
                snoozed_until = datetime.fromisoformat(snoozed_until_str) if snoozed_until_str else None

                if now >= claimed_at + min_hold and (not snoozed_until or now >= snoozed_until):
                    title_data['change_due'] = True
                    title_data['reminders_sent'] = 0
                    title_data['last_notified_at'] = now.isoformat()
                    save_state(self.state)

                    holder = channel.guild.get_member(title_data['holder_id'])
                    next_in_queue = channel.guild.get_member(title_data['queue'][0])
                    await channel.send(
                        f"**Title Change Due: {title_name}**\n"
                        f"Current: {holder.mention if holder else 'Unknown User'} → Next: {next_in_queue.mention if next_in_queue else 'Unknown User'}.\n"
                        "Guardians, please make the change in-game and confirm with `!ack " f"{title_name}`."
                    )
                    log_event(title_name, 'due', title_data['holder_id'], title_data['queue'][0], title_data['queue'])
            
            # Condition 2: Check for reminders
            if title_data.get('change_due'):
                last_notified_at = datetime.fromisoformat(title_data.get('last_notified_at', now.isoformat()))
                remind_interval = timedelta(minutes=self.state['config']['remind_every_minutes'])
                max_reminders = self.state['config']['max_reminders']
                
                if now >= last_notified_at + remind_interval and title_data.get('reminders_sent', 0) < max_reminders:
                    title_data['reminders_sent'] += 1
                    title_data['last_notified_at'] = now.isoformat()
                    save_state(self.state)

                    holder = channel.guild.get_member(title_data['holder_id'])
                    next_in_queue = channel.guild.get_member(title_data['queue'][0])
                    await channel.send(
                        f"**REMINDER: Title Change Pending for {title_name}**\n"
                        f"Current: {holder.mention if holder else 'Unknown User'} is still awaiting replacement by {next_in_queue.mention if next_in_queue else 'Unknown User'}.\n"
                        "Guardians, please use `!ack " f"{title_name}` after updating in-game."
                    )
                    log_event(title_name, 'remind', title_data['holder_id'], title_data['queue'][0], title_data['queue'])

    # --- Helper to manage roles ---
    async def _update_discord_roles(self, guild, old_holder_id, new_holder_id, title_name):
        role = discord.utils.get(guild.roles, name=title_name)
        if not role:
            logging.warning(f"Role '{title_name}' not found for role management.")
            return

        try:
            if old_holder_id:
                old_holder = guild.get_member(old_holder_id)
                if old_holder and role in old_holder.roles:
                    await old_holder.remove_roles(role, reason="Title released/transferred")
            
            if new_holder_id:
                new_holder = guild.get_member(new_holder_id)
                if new_holder and role not in new_holder.roles:
                    await new_holder.add_roles(role, reason="Title claimed")
        except discord.Forbidden:
            logging.error(f"Bot lacks permissions to manage the '{title_name}' role. Ensure it's higher in the role hierarchy.")
        except discord.HTTPException as e:
            logging.error(f"Failed to update roles for '{title_name}': {e}")


    # --- Member Commands ---

    @commands.command(name='claim', help='Claim a title or join its queue.')
    async def claim(self, ctx, *, title_name: str):
        """Claims a title if available, or adds the user to the queue."""
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")
        
        # Check if user is already a holder or in any queue
        for t_name, t_data in self.state['titles'].items():
            if t_data['holder_id'] == ctx.author.id:
                return await ctx.send(f"You already hold the **{t_name}** title.")
            if ctx.author.id in t_data['queue']:
                return await ctx.send(f"You are already in the queue for the **{t_name}** title.")

        title_data = self.state['titles'][title_name]

        if not title_data['holder_id']: # Title is unheld
            title_data['holder_id'] = ctx.author.id
            title_data['claimed_at'] = datetime.now(timezone.utc).isoformat()
            await self._update_discord_roles(ctx.guild, None, ctx.author.id, title_name)
            log_event(title_name, 'claim', new_holder_id=ctx.author.id, queue_snapshot=title_data['queue'])
            save_state(self.state)
            await ctx.send(f"🎉 Congratulations! You have claimed the **{title_name}** title.")
        else: # Title is held, join queue
            if ctx.author.id in title_data['queue']:
                 return await ctx.send("You are already in this queue.") # Redundant but safe
            title_data['queue'].append(ctx.author.id)
            position = len(title_data['queue'])
            log_event(title_name, 'queued', previous_holder_id=title_data['holder_id'], new_holder_id=ctx.author.id, queue_snapshot=title_data['queue'])
            save_state(self.state)
            
            claimed_at = datetime.fromisoformat(title_data['claimed_at'])
            min_hold = timedelta(minutes=self.state['config']['min_hold_minutes'])
            eta = claimed_at + min_hold
            time_left = eta - datetime.now(timezone.utc)

            eta_msg = f"eligible for rotation after {format_timedelta(time_left)}." if time_left.total_seconds() > 0 else "eligible for rotation now."
            
            await ctx.send(f"👍 You have been added to the queue for **{title_name}** at position **#{position}**. The title is {eta_msg}")

    @commands.command(name='release', help='Release the title you currently hold.')
    async def release(self, ctx):
        """Releases the title held by the user."""
        held_title_name = None
        for t_name, t_data in self.state['titles'].items():
            if t_data['holder_id'] == ctx.author.id:
                held_title_name = t_name
                break
        
        if not held_title_name:
            return await ctx.send("You do not currently hold any title.")

        title_data = self.state['titles'][held_title_name]
        old_holder_id = title_data['holder_id']
        await self._update_discord_roles(ctx.guild, old_holder_id, None, held_title_name)
        
        if not title_data['queue']:
            title_data['holder_id'] = None
            title_data['claimed_at'] = None
            log_event(held_title_name, 'release', previous_holder_id=old_holder_id, queue_snapshot=title_data['queue'])
            await ctx.send(f"✅ You have released the **{held_title_name}** title. It is now available.")
        else:
            # Trigger a due change immediately
            title_data['change_due'] = True
            title_data['reminders_sent'] = 0
            title_data['last_notified_at'] = datetime.now(timezone.utc).isoformat()
            
            log_event(held_title_name, 'release', previous_holder_id=old_holder_id, new_holder_id=title_data['queue'][0], queue_snapshot=title_data['queue'], notes="Triggered guardian flow.")
            
            announce_channel = self.bot.get_channel(self.state['config']['announce_channel_id'])
            if announce_channel:
                holder = ctx.guild.get_member(old_holder_id)
                next_in_queue = ctx.guild.get_member(title_data['queue'][0])
                await announce_channel.send(
                    f"**Title Change Due: {held_title_name}** (Holder Released)\n"
                    f"Current: {holder.mention if holder else 'Unknown User'} → Next: {next_in_queue.mention if next_in_queue else 'Unknown User'}.\n"
                    "Guardians, please make the change in-game and confirm with `!ack " f"{held_title_name}`."
                )
            await ctx.send(f"✅ You have released **{held_title_name}**. Guardians have been notified to assign it to the next person in the queue.")

        save_state(self.state)

    @commands.command(name='queue', help='Show the queue for a specific title.')
    async def queue(self, ctx, *, title_name: str):
        """Displays the current holder and queue for a title."""
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")

        data = self.state['titles'][title_name]
        embed = discord.Embed(title=f"Status for {title_name}", color=discord.Color.blue())

        if data['holder_id']:
            holder = ctx.guild.get_member(data['holder_id'])
            claimed_at = datetime.fromisoformat(data['claimed_at'])
            held_for = format_timedelta(datetime.now(timezone.utc) - claimed_at)
            embed.add_field(name="Current Holder", value=f"{holder.mention if holder else 'Unknown User'} (held for {held_for})", inline=False)
        else:
            embed.add_field(name="Current Holder", value="None (Available)", inline=False)
            
        if data.get('snoozed_until'):
            snoozed_until = datetime.fromisoformat(data['snoozed_until'])
            if snoozed_until > datetime.now(timezone.utc):
                time_left = format_timedelta(snoozed_until - datetime.now(timezone.utc))
                embed.set_footer(text=f"Snoozed for another {time_left}.")

        if data['queue']:
            queue_text = []
            for i, user_id in enumerate(data['queue']):
                user = ctx.guild.get_member(user_id)
                queue_text.append(f"{i+1}. {user.mention if user else f'Unknown User (ID: {user_id})'}")
            embed.add_field(name="Queue", value="\n".join(queue_text), inline=False)
        else:
            embed.add_field(name="Queue", value="Empty", inline=False)
            
        await ctx.send(embed=embed)

    @commands.command(name='titles', help='Show a summary of all titles.')
    async def titles(self, ctx):
        """Displays a summary of all available titles and their status."""
        embed = discord.Embed(title="Server Title Summary", color=discord.Color.gold())
        
        if not self.state['titles']:
            embed.description = "No titles have been configured yet."
            return await ctx.send(embed=embed)
            
        for name, data in self.state['titles'].items():
            status = ""
            if data.get('change_due'):
                status = "🔴 Change Due"
            elif data.get('snoozed_until') and datetime.fromisoformat(data['snoozed_until']) > datetime.now(timezone.utc):
                status = "Snoozed"
            elif data['holder_id']:
                status = "Held"
            else:
                status = "🟢 Available"

            value = f"**Holder**: "
            if data['holder_id']:
                holder = ctx.guild.get_member(data['holder_id'])
                claimed_at = datetime.fromisoformat(data['claimed_at'])
                held_for = format_timedelta(datetime.now(timezone.utc) - claimed_at)
                value += f"{holder.mention if holder else 'Unknown'} ({held_for})\n"
            else:
                value += "None\n"
            
            value += f"**Queue**: {len(data['queue'])} waiting\n"
            value += f"**Status**: {status}"
            embed.add_field(name=name, value=value, inline=True)
            
        await ctx.send(embed=embed)
        
    @commands.command(name='mytitle', help='Show which title you hold.')
    async def mytitle(self, ctx):
        """Shows the title held by the user and for how long."""
        for t_name, t_data in self.state['titles'].items():
            if t_data['holder_id'] == ctx.author.id:
                claimed_at = datetime.fromisoformat(t_data['claimed_at'])
                held_for = format_timedelta(datetime.now(timezone.utc) - claimed_at)
                await ctx.send(f"You currently hold the **{t_name}** title. You have held it for **{held_for}**.")
                return
        await ctx.send("You do not currently hold any title.")

    # --- Guardian & Admin Commands ---

    @commands.command(name='ack', help='Acknowledge a title change (Guardians only).')
    @commands.check(is_guardian_or_admin)
    async def ack(self, ctx, *, title_name: str):
        """Acknowledges a title change, passing it to the next in queue."""
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")

        title_data = self.state['titles'][title_name]
        if not title_data.get('change_due'):
            return await ctx.send(f"There is no pending change for **{title_name}** to acknowledge.")
        if not title_data['queue']:
            return await ctx.send(f"Cannot acknowledge: the queue for **{title_name}** is empty.")

        old_holder_id = title_data['holder_id']
        new_holder_id = title_data['queue'].pop(0)

        title_data['holder_id'] = new_holder_id
        title_data['claimed_at'] = datetime.now(timezone.utc).isoformat()
        title_data['change_due'] = False
        title_data['snoozed_until'] = None
        title_data['reminders_sent'] = 0
        
        await self._update_discord_roles(ctx.guild, old_holder_id, new_holder_id, title_name)
        
        log_event(title_name, 'ack', old_holder_id, new_holder_id, title_data['queue'], f"Acknowledged by {ctx.author.id}")
        save_state(self.state)

        new_holder = ctx.guild.get_member(new_holder_id)
        confirmation_msg = f"✅ Confirmed: The **{title_name}** title has been passed to {new_holder.mention if new_holder else 'Unknown User'}!"
        
        await ctx.send(confirmation_msg)
        announce_channel = self.bot.get_channel(self.state['config']['announce_channel_id'])
        if announce_channel and announce_channel.id != ctx.channel.id:
            await announce_channel.send(confirmation_msg)

    @commands.command(name='snooze', help='Delay notifications for a title (Guardians only).')
    @commands.check(is_guardian_or_admin)
    async def snooze(self, ctx, title_name: str, minutes: int):
        """Snoozes notifications for a due title change."""
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")
        if minutes <= 0:
            return await ctx.send("Please provide a positive number of minutes.")

        title_data = self.state['titles'][title_name]
        snooze_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        title_data['snoozed_until'] = snooze_until.isoformat()
        
        log_event(title_name, 'snooze', notes=f"Snoozed for {minutes} minutes by {ctx.author.id}")
        save_state(self.state)
        
        await ctx.send(f" snoozed for **{minutes}** minutes.")

    @commands.command(name='force_release', help='Forcefully remove a holder (Admins only).')
    @commands.has_permissions(manage_roles=True)
    async def force_release(self, ctx, *, title_name: str):
        """Forcefully releases a title from its current holder."""
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")

        title_data = self.state['titles'][title_name]
        if not title_data['holder_id']:
            return await ctx.send(f"The **{title_name}** title is already unheld.")

        old_holder_id = title_data['holder_id']
        await self._update_discord_roles(ctx.guild, old_holder_id, None, title_name)
        
        # This logic mirrors the user !release command
        if not title_data['queue']:
            title_data['holder_id'] = None
            title_data['claimed_at'] = None
            log_event(title_name, 'force_release', previous_holder_id=old_holder_id, notes=f"Forced by {ctx.author.id}")
            await ctx.send(f"✅ The **{title_name}** title has been forcefully released and is now available.")
        else:
            title_data['change_due'] = True
            title_data['reminders_sent'] = 0
            title_data['last_notified_at'] = datetime.now(timezone.utc).isoformat()
            
            log_event(title_name, 'force_release', old_holder_id, title_data['queue'][0], title_data['queue'], f"Forced by {ctx.author.id}, triggered guardian flow.")
            
            announce_channel = self.bot.get_channel(self.state['config']['announce_channel_id'])
            if announce_channel:
                holder = ctx.guild.get_member(old_holder_id)
                next_in_queue = ctx.guild.get_member(title_data['queue'][0])
                await announce_channel.send(
                    f"**Title Change Due: {title_name}** (Forced Release)\n"
                    f"Current: {holder.mention if holder else 'Unknown User'} → Next: {next_in_queue.mention if next_in_queue else 'Unknown User'}.\n"
                    "Guardians, please make the change in-game and confirm with `!ack " f"{title_name}`."
                )
            await ctx.send(f"✅ The **{title_name}** holder has been removed. Guardians have been notified to assign it to the next in queue.")

        save_state(self.state)

    @commands.command(name='set_min_hold', help='Set the minimum hold time in minutes.')
    @commands.has_permissions(manage_roles=True)
    async def set_min_hold(self, ctx, minutes: int):
        if minutes < 0:
            return await ctx.send("Minimum hold time cannot be negative.")
        self.state['config']['min_hold_minutes'] = minutes
        log_event(None, 'config_change', notes=f"min_hold_minutes set to {minutes} by {ctx.author.id}")
        save_state(self.state)
        await ctx.send(f"✅ Minimum title hold time set to **{minutes}** minutes.")

    @commands.command(name='set_announce', help='Set the channel for guardian notifications.')
    @commands.has_permissions(manage_roles=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        self.state['config']['announce_channel_id'] = channel.id
        log_event(None, 'config_change', notes=f"announce_channel_id set to {channel.id} by {ctx.author.id}")
        save_state(self.state)
        await ctx.send(f"✅ Guardian announcements will now be sent to {channel.mention}.")

    @commands.command(name='set_guardians', help='Set guardian users or roles.')
    @commands.has_permissions(manage_roles=True)
    async def set_guardians(self, ctx, *mentions: commands.Greedy[typing.Union[discord.Member, discord.Role]]):
        if not mentions:
            return await ctx.send("Please mention at least one user or role.")
        guardian_ids = [m.id for m in mentions]
        self.state['config']['guardians'] = guardian_ids
        log_event(None, 'config_change', notes=f"Guardians set to {guardian_ids} by {ctx.author.id}")
        save_state(self.state)
        mention_str = ' '.join([m.mention for m in mentions])
        await ctx.send(f"✅ Guardians set to: {mention_str}")
    
    @commands.command(name='set_reminders', help='Set reminder frequency and count.')
    @commands.has_permissions(manage_roles=True)
    async def set_reminders(self, ctx, interval_minutes: int, max_count: int):
        if interval_minutes <= 0 or max_count < 0:
            return await ctx.send("Please provide positive values.")
        self.state['config']['remind_every_minutes'] = interval_minutes
        self.state['config']['max_reminders'] = max_count
        log_event(None, 'config_change', notes=f"Reminders set to every {interval_minutes}m, max {max_count} by {ctx.author.id}")
        save_state(self.state)
        await ctx.send(f"✅ Due change reminders will be sent every **{interval_minutes}** minutes, up to **{max_count}** times.")

    @commands.command(name='import_titles', help='Create or update the list of managed titles.')
    @commands.has_permissions(manage_roles=True)
    async def import_titles(self, ctx, *, title_list: str):
        """Seeds the bot with titles. Preserves existing state for matching names."""
        new_titles = [t.strip().title() for t in title_list.split(',')]
        if not new_titles:
            return await ctx.send("Please provide a comma-separated list of titles.")
        
        created_count = 0
        for title_name in new_titles:
            if title_name not in self.state['titles']:
                self.state['titles'][title_name] = {
                    "holder_id": None,
                    "claimed_at": None,
                    "queue": [],
                    "change_due": False,
                    "snoozed_until": None,
                    "reminders_sent": 0,
                    "last_notified_at": None,
                }
                created_count += 1
            
            # Optional: Ensure Discord role exists
            if not discord.utils.get(ctx.guild.roles, name=title_name):
                try:
                    await ctx.guild.create_role(name=title_name, reason="TitleRequest bot setup")
                    await ctx.send(f"ℹ️ Created Discord role: **{title_name}**")
                except discord.Forbidden:
                    await ctx.send(f"⚠️ Could not create role **{title_name}**. Please check my permissions and role hierarchy.")

        log_event(None, 'config_change', notes=f"Titles imported by {ctx.author.id}: {new_titles}")
        save_state(self.state)
        await ctx.send(f"✅ Titles processed. **{created_count}** new titles were added. Existing titles were preserved.")

    @commands.command(name='config', help='Display the current bot configuration.')
    @commands.has_permissions(manage_roles=True)
    async def config(self, ctx):
        """Shows the current configuration."""
        cfg = self.state['config']
        embed = discord.Embed(title="TitleRequest Bot Configuration", color=discord.Color.dark_grey())
        
        embed.add_field(name="Min Hold Time", value=f"{cfg['min_hold_minutes']} minutes", inline=True)
        
        channel = self.bot.get_channel(cfg['announce_channel_id']) if cfg['announce_channel_id'] else "Not set"
        embed.add_field(name="Announce Channel", value=channel.mention if hasattr(channel, 'mention') else channel, inline=True)

        embed.add_field(name="Reminders", value=f"Every {cfg['remind_every_minutes']}m, max {cfg['max_reminders']} times", inline=True)
        
        guardians_list = []
        for gid in cfg['guardians']:
            g_obj = ctx.guild.get_member(gid) or ctx.guild.get_role(gid)
            if g_obj:
                guardians_list.append(g_obj.mention)
        embed.add_field(name="Guardians", value=' '.join(guardians_list) if guardians_list else "Not set", inline=False)
        
        titles_list = ", ".join(self.state['titles'].keys()) or "None"
        embed.add_field(name="Managed Titles", value=titles_list, inline=False)
        
        await ctx.send(embed=embed)

    # --- History Commands ---

    @commands.command(name='history', help='Show the last N events for a title.')
    async def history(self, ctx, title_name: str, count: int = 10):
        """Shows recent history for a specific title."""
        title_name = title_name.title()
        if not os.path.exists(LOG_FILE):
            return await ctx.send("No history log found.")
        
        with open(LOG_FILE, 'r') as f:
            logs = json.load(f)

        title_logs = [log for log in logs if log['title'] and log['title'].lower() == title_name.lower()]
        if not title_logs:
            return await ctx.send(f"No history found for **{title_name}**.")
            
        description = []
        for log in reversed(title_logs[-count:]):
            ts = datetime.fromisoformat(log['timestamp']).strftime('%Y-%m-%d %H:%M UTC')
            description.append(f"`{ts}` - **{log['action'].upper()}** - {log.get('notes') or '...'}")

        embed = discord.Embed(title=f"Recent History for {title_name}", description="\n".join(description), color=discord.Color.purple())
        await ctx.send(embed=embed)

    @commands.command(name='fullhistory', help='Show the last N events across all titles.')
    async def fullhistory(self, ctx, count: int = 10):
        """Shows recent history for all titles."""
        if not os.path.exists(LOG_FILE):
            return await ctx.send("No history log found.")
        
        with open(LOG_FILE, 'r') as f:
            logs = json.load(f)

        if not logs:
            return await ctx.send("History is empty.")
            
        description = []
        for log in reversed(logs[-count:]):
            ts = datetime.fromisoformat(log['timestamp']).strftime('%Y-%m-%d %H:%M UTC')
            title_str = f"**{log['title']}**: " if log['title'] else ""
            description.append(f"`{ts}` - {title_str}**{log['action'].upper()}** - {log.get('notes') or '...'}")
        
        embed = discord.Embed(title="Recent Global History", description="\n".join(description), color=discord.Color.purple())
        await ctx.send(embed=embed)


# --- Bot Startup ---

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logging.info('------')
    # Load the cog
    await bot.add_cog(TitleCog(bot))
    # Seed default titles if state is fresh
    current_state = load_state()
    if not current_state['titles']:
        logging.info("No titles found in state. Seeding with defaults.")
        for title_name in DEFAULT_TITLES:
            current_state['titles'][title_name] = {
                "holder_id": None, "claimed_at": None, "queue": [], "change_due": False,
                "snoozed_until": None, "reminders_sent": 0, "last_notified_at": None,
            }
        save_state(current_state)
        log_event(None, 'config_change', notes="Initial seeding of default titles.")

# --- Web Server & Live Dashboard ---
app = Flask('')

@app.route('/')
def home():
    """Generates the HTML for the live dashboard."""
    if not bot.is_ready():
        return "<h1>Bot is still starting up, please refresh in a moment.</h1>", 503

    # Safely get the cog and state
    cog = bot.get_cog('TitleRequest')
    if not cog:
        return "<h1>Bot cog not loaded.</h1>", 500
    
    state = cog.state
    
    # Start building the HTML page
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>TitleRequest Bot Dashboard</title>
        <meta http-equiv="refresh" content="60">
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 2em; }
            .container { max-width: 1200px; margin: auto; }
            h1 { color: #ffffff; border-bottom: 2px solid #333; padding-bottom: 10px; }
            .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }
            .card { background-color: #1e1e1e; border: 1px solid #333; border-radius: 8px; padding: 20px; box-shadow: 0 4px 8px rgba(0,0,0,0.2); }
            .card h2 { margin-top: 0; color: #bb86fc; }
            .card p { margin: 5px 0; }
            .card .label { font-weight: bold; color: #a0a0a0; }
            .card .status-held { color: #03dac6; }
            .card .status-available { color: #4caf50; }
            .card .status-due { color: #cf6679; font-weight: bold; }
            ol { padding-left: 20px; }
            li { margin-bottom: 5px; }
            footer { text-align: center; margin-top: 30px; color: #777; font-size: 0.9em; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>TitleRequest Bot Status</h1>
            <p><i>Last updated: """ + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') + """ (Page auto-refreshes every 60 seconds)</i></p>
            <div class="grid">
    """

    # Generate a card for each title
    guild = bot.guilds[0] if bot.guilds else None # Assume the bot is in one server

    for title_name, data in sorted(state['titles'].items()):
        html += '<div class="card">'
        html += f'<h2>{title_name}</h2>'

        # Holder Info
        if data['holder_id']:
            holder = guild.get_member(data['holder_id']) if guild else None
            holder_name = holder.display_name if holder else f"User ID: {data['holder_id']}"
            claimed_at = datetime.fromisoformat(data['claimed_at'])
            held_for = format_timedelta(datetime.now(timezone.utc) - claimed_at)
            
            status_class = "status-due" if data.get('change_due') else "status-held"
            html += f'<p><span class="label">Holder:</span> <span class="{status_class}">{holder_name}</span></p>'
            html += f'<p><span class="label">Held For:</span> {held_for}</p>'
        else:
            html += '<p><span class="label">Holder:</span> <span class="status-available">Available</span></p>'

        # Queue Info
        html += '<p><span class="label">Queue:</span></p>'
        if data['queue']:
            html += '<ol>'
            for i, user_id in enumerate(data['queue']):
                user = guild.get_member(user_id) if guild else None
                user_name = user.display_name if user else f"User ID: {user_id}"
                html += f'<li>{user_name}</li>'
            html += '</ol>'
        else:
            html += '<p>The queue is empty.</p>'

        html += '</div>' # End Card

    html += """
            </div> <!-- End Grid -->
            <footer>Powered by TitleRequest Bot</footer>
        </div>
    </body>
    </html>
    """
    return html

def run():
  app.run(host='0.0.0.0',port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- Final Bot Execution ---
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable not set.")

keep_alive()
bot.run(TOKEN)
