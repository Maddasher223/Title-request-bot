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
import dateutil.parser

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
                "guardian_titles": [],
                "remind_every_minutes": 15,
                "max_reminders": 3
            }
        }

def save_state(state):
    """Safely saves state to STATE_FILE via a temporary file."""
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
            # --- Check 1: Scheduled Events ---
            if title_data.get('schedule'):
                next_slot = title_data['schedule'][0]
                start_time = datetime.fromisoformat(next_slot['start_time'])
                if now >= start_time:
                    # A scheduled slot is starting now
                    scheduled_user_id = next_slot['user_id']
                    
                    # Remove from schedule
                    title_data['schedule'].pop(0)

                    # Insert at the front of the queue
                    if scheduled_user_id in title_data['queue']:
                        title_data['queue'].remove(scheduled_user_id)
                    title_data['queue'].insert(0, scheduled_user_id)

                    # Trigger change due, even if there was a holder
                    title_data['change_due'] = True
                    title_data['reminders_sent'] = 0
                    title_data['last_notified_at'] = now.isoformat()
                    
                    log_event(title_name, 'schedule_start', new_holder_id=scheduled_user_id, notes="Scheduled slot started.")
                    save_state(self.state)
                    
                    # Announce it
                    holder_id = title_data.get('holder_id')
                    holder = channel.guild.get_member(holder_id) if holder_id else None
                    next_in_queue = channel.guild.get_member(scheduled_user_id)
                    
                    if holder:
                        await channel.send(
                            f"**Scheduled Title Change: {title_name}**\n"
                            f"Current: {holder.mention}'s time is up. → Next (Scheduled): {next_in_queue.mention}.\n"
                            "Guardians, please make the change in-game and confirm with `!assign " f"{title_name}`."
                        )
                    else:
                         await channel.send(
                            f"**Scheduled Title Assignment: {title_name}**\n"
                            f"Assign to: {next_in_queue.mention}.\n"
                            "Guardians, please make the change in-game and confirm with `!assign " f"{title_name}`."
                        )
                    continue # Move to next title

            # --- Check 2: Standard Queue Rotation ---
            if not title_data.get('change_due') and title_data['holder_id'] and title_data['queue']:
                claimed_at = datetime.fromisoformat(title_data['claimed_at'])
                min_hold = timedelta(minutes=self.state['config']['min_hold_minutes'])
                snoozed_until_str = title_data.get('snoozed_until')
                snoozed_until = datetime.fromisoformat(snoozed_until_str) if snoozed_until_str else None

                if now >= claimed_at + min_hold and (not snoozed_until or now >= snoozed_until):
                    # --- New Logic: Find next eligible user ---
                    eligible_user_found = False
                    while title_data['queue'] and not eligible_user_found:
                        next_user_id = title_data['queue'][0]
                        
                        # Check if this user already holds another title
                        is_holding_another = False
                        for t_name, t_data in self.state['titles'].items():
                            if t_data['holder_id'] == next_user_id:
                                is_holding_another = True
                                break
                        
                        if is_holding_another:
                            # Skip this user
                            title_data['queue'].pop(0)
                            skipped_user = self.bot.get_user(next_user_id)
                            if skipped_user:
                                try:
                                    await skipped_user.send(f"Your turn for the **{title_name}** title was skipped because you currently hold another title.")
                                except discord.Forbidden:
                                    pass # Can't DM user
                            log_event(title_name, 'queue_skip', new_holder_id=next_user_id, notes="User holds another title.")
                        else:
                            eligible_user_found = True

                    if eligible_user_found:
                        title_data['change_due'] = True
                        title_data['reminders_sent'] = 0
                        title_data['last_notified_at'] = now.isoformat()
                        save_state(self.state)

                        holder = channel.guild.get_member(title_data['holder_id'])
                        next_in_queue = channel.guild.get_member(title_data['queue'][0])
                        await channel.send(
                            f"**Title Change Due: {title_name}**\n"
                            f"Current: {holder.mention if holder else 'Unknown User'} → Next: {next_in_queue.mention if next_in_queue else 'Unknown User'}.\n"
                            "Guardians, please make the change in-game and confirm with `!assign " f"{title_name}`."
                        )
                        log_event(title_name, 'due', title_data['holder_id'], title_data['queue'][0], title_data['queue'])
            
            # --- Check 3: Reminders ---
            if title_data.get('change_due'):
                last_notified_at = datetime.fromisoformat(title_data.get('last_notified_at', now.isoformat()))
                remind_interval = timedelta(minutes=self.state['config']['remind_every_minutes'])
                max_reminders = self.state['config']['max_reminders']
                
                if now >= last_notified_at + remind_interval and title_data.get('reminders_sent', 0) < max_reminders:
                    title_data['reminders_sent'] += 1
                    title_data['last_notified_at'] = now.isoformat()
                    save_state(self.state)

                    holder = channel.guild.get_member(title_data.get('holder_id'))
                    next_in_queue = channel.guild.get_member(title_data['queue'][0])
                    
                    message = ""
                    if holder:
                        message = (f"**REMINDER: Title Change Pending for {title_name}**\n"
                                   f"Current: {holder.mention} is still awaiting replacement by {next_in_queue.mention}.\n")
                    else:
                        message = (f"**REMINDER: Title Assignment Pending for {title_name}**\n"
                                   f"Waiting for {next_in_queue.mention} to be assigned the title.\n")

                    await channel.send(message + "Guardians, please use `!assign " f"{title_name}` after updating in-game.")
                    log_event(title_name, 'remind', title_data.get('holder_id'), title_data['queue'][0], title_data['queue'])

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
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")
        
        title_data = self.state['titles'][title_name]

        if ctx.author.id in title_data['queue']:
            return await ctx.send(f"You are already in the queue for the **{title_name}** title.")

        # If title is unheld, add user to queue and trigger guardian flow
        if not title_data['holder_id']:
            title_data['queue'].insert(0, ctx.author.id) # Add to front of queue
            title_data['change_due'] = True
            title_data['reminders_sent'] = 0
            title_data['last_notified_at'] = datetime.now(timezone.utc).isoformat()
            
            log_event(title_name, 'queued', new_holder_id=ctx.author.id, queue_snapshot=title_data['queue'], notes="Claimed unheld title, pending assignment.")
            save_state(self.state)

            announce_channel = self.bot.get_channel(self.state['config']['announce_channel_id'])
            if announce_channel:
                next_in_queue = ctx.guild.get_member(ctx.author.id)
                await announce_channel.send(
                    f"**Title Assignment Due: {title_name}**\n"
                    f"Assign to: {next_in_queue.mention if next_in_queue else 'Unknown User'}.\n"
                    "Guardians, please make the change in-game and confirm with `!assign " f"{title_name}`."
                )
            await ctx.send(f"👍 You have claimed the unheld **{title_name}** title. Guardians have been notified to assign it to you.")

        # If title is held, add to back of queue
        else:
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
                    "Guardians, please make the change in-game and confirm with `!assign " f"{held_title_name}`."
                )
            await ctx.send(f"✅ You have released **{held_title_name}**. Guardians have been notified to assign it to the next person in the queue.")

        save_state(self.state)

    @commands.command(name='queue', help='Show the queue for a specific title.')
    async def queue(self, ctx, *, title_name: str):
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
        for t_name, t_data in self.state['titles'].items():
            if t_data['holder_id'] == ctx.author.id:
                claimed_at = datetime.fromisoformat(t_data['claimed_at'])
                held_for = format_timedelta(datetime.now(timezone.utc) - claimed_at)
                await ctx.send(f"You currently hold the **{t_name}** title. You have held it for **{held_for}**.")
                return
        await ctx.send("You do not currently hold any title.")

    # --- Guardian & Admin Commands ---

    @commands.command(name='assign', help='Assign a title to the next person in queue (Guardians only).')
    @commands.check(is_guardian_or_admin)
    async def assign(self, ctx, *, title_name: str):
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")

        title_data = self.state['titles'][title_name]
        if not title_data.get('change_due'):
            return await ctx.send(f"There is no pending assignment for **{title_name}**.")
        if not title_data['queue']:
            return await ctx.send(f"Cannot assign: the queue for **{title_name}** is empty.")

        old_holder_id = title_data['holder_id']
        new_holder_id = title_data['queue'].pop(0)

        title_data['holder_id'] = new_holder_id
        title_data['claimed_at'] = datetime.now(timezone.utc).isoformat()
        title_data['change_due'] = False
        title_data['snoozed_until'] = None
        title_data['reminders_sent'] = 0
        
        await self._update_discord_roles(ctx.guild, old_holder_id, new_holder_id, title_name)
        
        log_event(title_name, 'assign', old_holder_id, new_holder_id, title_data['queue'], f"Assigned by {ctx.author.id}")
        save_state(self.state)

        new_holder = ctx.guild.get_member(new_holder_id)
        confirmation_msg = f"✅ Confirmed: The **{title_name}** title has been assigned to {new_holder.mention if new_holder else 'Unknown User'}!"
        
        await ctx.send(confirmation_msg)
        announce_channel = self.bot.get_channel(self.state['config']['announce_channel_id'])
        if announce_channel and announce_channel.id != ctx.channel.id:
            await announce_channel.send(confirmation_msg)

    @commands.command(name='snooze', help='Delay notifications for a title (Guardians only).')
    @commands.check(is_guardian_or_admin)
    async def snooze(self, ctx, title_name: str, minutes: int):
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
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")

        title_data = self.state['titles'][title_name]
        if not title_data['holder_id']:
            return await ctx.send(f"The **{title_name}** title is already unheld.")

        old_holder_id = title_data['holder_id']
        await self._update_discord_roles(ctx.guild, old_holder_id, None, title_name)
        
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
                    "Guardians, please make the change in-game and confirm with `!assign " f"{title_name}`."
                )
            await ctx.send(f"✅ The **{title_name}** holder has been removed. Guardians have been notified to assign it to the next in queue.")

        save_state(self.state)

    @commands.command(name='delete_title', help='Deletes a title from the bot (Admins only).')
    @commands.has_permissions(manage_roles=True)
    async def delete_title(self, ctx, *, title_name: str):
        title_name = title_name.title()
        if title_name not in self.state['titles']:
            return await ctx.send(f"❌ Title '{title_name}' does not exist.")

        del self.state['titles'][title_name]

        if 'guardian_titles' in self.state['config'] and title_name in self.state['config']['guardian_titles']:
            self.state['config']['guardian_titles'].remove(title_name)

        log_event(title_name, 'delete', notes=f"Title deleted by {ctx.author.id}")
        save_state(self.state)

        await ctx.send(f"✅ The title **{title_name}** has been permanently deleted from the bot.")


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
    
    @commands.command(name='set_guardian_titles', help='Designate which titles are for guardians.')
    @commands.has_permissions(manage_roles=True)
    async def set_guardian_titles(self, ctx, *, title_list: str):
        guardian_titles = [t.strip().title() for t in title_list.split(',')]
        
        for title in guardian_titles:
            if title not in self.state['titles']:
                return await ctx.send(f"❌ Error: The title '{title}' does not exist. Please import it first.")

        self.state['config']['guardian_titles'] = guardian_titles
        log_event(None, 'config_change', notes=f"Guardian titles set to {guardian_titles} by {ctx.author.id}")
        save_state(self.state)
        await ctx.send(f"✅ Guardian titles for the dashboard have been set to: **{', '.join(guardian_titles)}**")

    @commands.command(name='set_title_details', help='Set icon and description for a title.')
    @commands.has_permissions(manage_roles=True)
    async def set_title_details(self, ctx, *, details: str):
        """Sets the icon URL and description for a title for the dashboard.
        Usage: !set_title_details Title Name | icon_url=https://... | description=Buffs here
        """
        try:
            parts = [p.strip() for p in details.split('|')]
            title_name = parts[0].title()
            
            if title_name not in self.state['titles']:
                return await ctx.send(f"❌ Title '{title_name}' does not exist.")

            details_dict = {}
            for part in parts[1:]:
                key, value = part.split('=', 1)
                details_dict[key.strip()] = value.strip()

            if 'icon_url' in details_dict:
                self.state['titles'][title_name]['icon_url'] = details_dict['icon_url']
            if 'description' in details_dict:
                self.state['titles'][title_name]['description'] = details_dict['description']

            log_event(title_name, 'config_change', notes=f"Details updated by {ctx.author.id}")
            save_state(self.state)
            await ctx.send(f"✅ Details for **{title_name}** have been updated.")

        except Exception as e:
            await ctx.send(f"❌ Invalid format. Please use: `!set_title_details Title Name | icon_url=... | description=...`")
            logging.error(f"Error parsing set_title_details: {e}")


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
        new_titles = [t.strip().title() for t in title_list.split(',') if t.strip()]
        if not new_titles:
            return await ctx.send("Please provide a comma-separated list of titles.")
        
        created_titles = []
        for title_name in new_titles:
            if title_name not in self.state['titles']:
                self.state['titles'][title_name] = {
                    "holder_id": None, "claimed_at": None, "queue": [], "schedule": [], "change_due": False,
                    "snoozed_until": None, "reminders_sent": 0, "last_notified_at": None,
                    "icon_url": None, "description": None
                }
                created_titles.append(title_name)
            
            if not discord.utils.get(ctx.guild.roles, name=title_name):
                try:
                    await ctx.guild.create_role(name=title_name, reason="TitleRequest bot setup")
                    await ctx.send(f"ℹ️ Created Discord role: **{title_name}**")
                except discord.Forbidden:
                    await ctx.send(f"⚠️ Could not create role **{title_name}**. Please check my permissions and role hierarchy.")

        log_event(None, 'config_change', notes=f"Titles imported by {ctx.author.id}: {new_titles}")
        save_state(self.state)
        if created_titles:
            await ctx.send(f"✅ **{len(created_titles)}** new titles were added: `{', '.join(created_titles)}`. Existing titles were preserved.")
        else:
            await ctx.send("✅ All specified titles already exist. No new titles were added.")

    @commands.command(name='config', help='Display the current bot configuration.')
    @commands.has_permissions(manage_roles=True)
    async def config(self, ctx):
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

        guardian_titles_list = ", ".join(cfg.get('guardian_titles', [])) or "None"
        embed.add_field(name="Guardian Titles (Dashboard)", value=guardian_titles_list, inline=False)
        
        await ctx.send(embed=embed)

    # --- History Commands ---

    @commands.command(name='history', help='Show the last N events for a title.')
    async def history(self, ctx, title_name: str, count: int = 10):
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
    await bot.add_cog(TitleCog(bot))
    current_state = load_state()
    if not current_state['titles']:
        logging.info("No titles found in state. Seeding with defaults.")
        for title_name in DEFAULT_TITLES:
            current_state['titles'][title_name] = {
                "holder_id": None, "claimed_at": None, "queue": [], "schedule": [], "change_due": False,
                "snoozed_until": None, "reminders_sent": 0, "last_notified_at": None,
                "icon_url": None, "description": None
            }
        save_state(current_state)
        log_event(None, 'config_change', notes="Initial seeding of default titles.")

# --- Web Server & Live Dashboard ---
app = Flask('')

@app.route('/')
def home():
    if not bot.is_ready() or not bot.guilds:
        return """
        <!DOCTYPE html><html><head><title>Bot Status</title><meta http-equiv="refresh" content="10">
        <style>body{background-color:#121212;color:#e0e0e0;font-family:sans-serif;text-align:center;padding-top:20%;}</style>
        </head><body><h1>Bot is starting up...</h1><p>The dashboard will be available shortly. This page will refresh automatically.</p></body></html>
        """, 503

    cog = bot.get_cog('TitleRequest')
    if not cog:
        return "<h1>Error: Bot cog not loaded. Please check the logs.</h1>", 500
    
    state = cog.state
    guild = bot.guilds[0]
    
    guardian_title_names = state['config'].get('guardian_titles', [])
    guardian_titles = {}
    other_titles = {}

    for title_name, data in sorted(state['titles'].items()):
        if title_name in guardian_title_names:
            guardian_titles[title_name] = data
        else:
            other_titles[title_name] = data

    log_history = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            logs = json.load(f)
        
        change_events = [log for log in logs if log['action'] in ('claim', 'assign', 'force_release', 'release')]
        
        for log in reversed(change_events[-15:]):
            ts = datetime.fromisoformat(log['timestamp']).strftime('%b %d, %H:%M UTC')
            
            new_holder_name = "N/A"
            if log['new_holder_id']:
                member = guild.get_member(log['new_holder_id'])
                new_holder_name = member.display_name if member else f"ID: {log['new_holder_id']}"

            prev_holder_name = "N/A"
            if log['previous_holder_id']:
                 member = guild.get_member(log['previous_holder_id'])
                 prev_holder_name = member.display_name if member else f"ID: {log['previous_holder_id']}"

            action_map = {
                'claim': ('CROWNED', '👑'), 'assign': ('PASSED', '🤝'),
                'force_release': ('REMOVED', '🛡️'), 'release': ('RELEASED', '🕊️')
            }
            action_text, action_icon = action_map.get(log['action'], (log['action'].upper(), ''))
            
            log_history.append({
                "ts": ts, "title": log['title'], "action_icon": action_icon,
                "action_text": action_text, "new_holder": new_holder_name, "prev_holder": prev_holder_name
            })

    def generate_title_cards(title_dict):
        cards_html = ""
        if not title_dict:
            return "<p class='no-titles'>No titles in this category.</p>"

        for title_name, data in title_dict.items():
            status_class, status_text = "", ""
            if data.get('change_due'):
                status_class, status_text = "status-due", "ASSIGNMENT DUE"
            elif data['holder_id']:
                status_class, status_text = "status-held", "HELD"
            else:
                status_class, status_text = "status-available", "AVAILABLE"

            icon_html = f'<img src="{data.get("icon_url")}" class="title-icon" alt="Title Icon">' if data.get("icon_url") else '<div class="title-icon-placeholder"></div>'
            
            holder_html = f'<p class="info-line"><span class="label">Holder:</span> <span class="{status_class}">{status_text}</span></p>'
            if data['holder_id']:
                holder = guild.get_member(data['holder_id'])
                holder_name = holder.display_name if holder else f"User ID: {data['holder_id']}"
                claimed_at = datetime.fromisoformat(data['claimed_at'])
                held_for = format_timedelta(datetime.now(timezone.utc) - claimed_at)
                holder_html = f"""
                    <p class="info-line"><span class="label">Holder:</span> <span class="{status_class}">{holder_name}</span></p>
                    <p class="info-line"><span class="label">Held For:</span> {held_for}</p>
                """

            description_html = f'<p class="description">{data.get("description", "")}</p>' if data.get("description") else ""

            queue_html = '<p class="queue-empty">The queue is empty.</p>'
            if data['queue']:
                queue_items = ""
                for i, user_id in enumerate(data['queue']):
                    user = guild.get_member(user_id)
                    user_name = user.display_name if user else f"User ID: {user_id}"
                    queue_items += f'<li><span class="queue-pos">{i+1}.</span> {user_name}</li>'
                queue_html = f'<ol class="queue-list">{queue_items}</ol>'

            cards_html += f"""
            <div class="card">
                <div class="card-header">
                    {icon_html}
                    <h2>{title_name}</h2>
                    <span class="status-badge {status_class}">{status_text}</span>
                </div>
                <div class="card-body">
                    {holder_html}
                    {description_html}
                    <p class="info-line label queue-label">Queue:</p>
                    {queue_html}
                </div>
            </div>
            """
        return cards_html

    guardian_cards_html = generate_title_cards(guardian_titles)
    other_cards_html = generate_title_cards(other_titles)

    history_rows_html = ""
    if log_history:
        for entry in log_history:
            history_rows_html += f"""
            <tr>
                <td>{entry['ts']}</td>
                <td>{entry['title']}</td>
                <td><span class="action-icon">{entry['action_icon']}</span> {entry['action_text']}</td>
                <td>{entry['new_holder']}</td>
                <td>{entry['prev_holder']}</td>
            </tr>
            """
    else:
        history_rows_html = '<tr><td colspan="5">No title change events found.</td></tr>'

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <title>TitleRequest Bot Dashboard</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta http-equiv="refresh" content="60">
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg-color: #0d1117; --card-bg: #161b22; --border-color: #30363d;
                --text-primary: #c9d1d9; --text-secondary: #8b949e;
                --accent-purple: #bb86fc; --accent-green: #3fb950; --accent-blue: #58a6ff; --accent-red: #f85149;
            }}
            body {{ font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--text-primary); margin: 0; padding: 2.5em; line-height: 1.6; }}
            .container {{ max-width: 1400px; margin: auto; }}
            .header {{ text-align: center; margin-bottom: 2.5em; }}
            .header h1 {{ font-size: 2.5rem; color: #fff; margin-bottom: 0.2em; }}
            .header p {{ color: var(--text-secondary); }}
            .section-title {{ font-size: 1.8rem; color: #fff; border-bottom: 1px solid var(--border-color); padding-bottom: 0.5em; margin-top: 2.5em; margin-bottom: 1em; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 25px; }}
            .card {{ background-color: var(--card-bg); border: 1px solid var(--border-color); border-radius: 8px; transition: transform 0.2s ease, box-shadow 0.2s ease; }}
            .card:hover {{ transform: translateY(-5px); box-shadow: 0 8px 20px rgba(0,0,0,0.3); }}
            .card-header {{ display: flex; align-items: center; padding: 15px 20px; border-bottom: 1px solid var(--border-color); gap: 15px; }}
            .title-icon {{ width: 40px; height: 40px; border-radius: 50%; object-fit: cover; background-color: #21262d; }}
            .title-icon-placeholder {{ width: 40px; height: 40px; border-radius: 50%; background-color: #21262d; }}
            .card-header h2 {{ margin: 0; font-size: 1.25rem; color: var(--text-primary); flex-grow: 1; }}
            .status-badge {{ font-size: 0.75rem; font-weight: 700; padding: 4px 8px; border-radius: 12px; white-space: nowrap; }}
            .status-held {{ background-color: rgba(88, 166, 255, 0.2); color: var(--accent-blue); }}
            .status-available {{ background-color: rgba(63, 185, 80, 0.2); color: var(--accent-green); }}
            .status-due {{ background-color: rgba(248, 81, 73, 0.2); color: var(--accent-red); }}
            .card-body {{ padding: 20px; }}
            .info-line {{ margin: 0 0 10px 0; color: var(--text-secondary); }}
            .label {{ font-weight: 500; color: var(--text-primary); }}
            .description {{ font-style: italic; color: var(--text-secondary); border-left: 3px solid var(--accent-purple); padding-left: 10px; margin: 15px 0; }}
            .queue-label {{ margin-top: 15px; }}
            .queue-list {{ padding-left: 20px; margin: 0; }}
            .queue-list li {{ margin-bottom: 5px; color: var(--text-secondary); }}
            .queue-pos {{ font-weight: 700; color: var(--accent-purple); margin-right: 5px; }}
            .queue-empty, .no-titles {{ color: var(--text-secondary); font-style: italic; padding: 20px 0; }}
            .table-wrapper {{ overflow-x: auto; }}
            .history-table {{ width: 100%; border-collapse: collapse; margin-top: 1em; }}
            .history-table th, .history-table td {{ padding: 12px 15px; text-align: left; border-bottom: 1px solid var(--border-color); white-space: nowrap; }}
            .history-table th {{ font-weight: 700; color: var(--text-primary); }}
            .history-table td {{ color: var(--text-secondary); }}
            .action-icon {{ margin-right: 8px; }}
            footer {{ text-align: center; margin-top: 3em; color: var(--text-secondary); font-size: 0.9em; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header"><h1>TitleRequest Bot Dashboard</h1><p>Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} (Page auto-refreshes every 60 seconds)</p></div>
            <h2 class="section-title">Guardian Titles</h2><div class="grid">{guardian_cards_html}</div>
            <h2 class="section-title">Standard Titles</h2><div class="grid">{other_cards_html}</div>
            <h2 class="section-title">Recent Events</h2><div class="table-wrapper"><table class="history-table"><thead><tr><th>Timestamp</th><th>Title</th><th>Action</th><th>New Holder</th><th>Previous Holder</th></tr></thead><tbody>{history_rows_html}</tbody></table></div>
            <footer>Powered by TitleRequest Bot for {guild.name}</footer>
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
