# main.py - Part 1/3

import discord
from discord.ext import commands, tasks
import json
import os
import logging
from datetime import datetime, timedelta, time
import asyncio
from threading import Thread
from flask import Flask, render_template, request, redirect, url_for, jsonify
import calendar

# --- Initial Setup ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- State and Logging Configuration ---
STATE_FILE = 'titles_state.json'
LOG_FILE = 'log.json'
state = {}
state_lock = asyncio.Lock()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

# --- Helper Functions ---
async def load_state():
    """Loads the bot's state from a JSON file."""
    global state
    async with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    # Convert string keys back to int for reminders
                    if 'config' in state and 'reminders' in state['config']:
                        state['config']['reminders'] = {int(k): v for k, v in state['config']['reminders'].items()}
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading state file: {e}")
                initialize_state()
        else:
            initialize_state()

def initialize_state():
    """Initializes a default state structure."""
    global state
    state = {
        'titles': {},
        'users': {},
        'config': {
            'min_hold_duration_hours': 24,
            'announcement_channel': None,
            'guardian_roles': [],
            'guardian_titles': {}, # Maps guardian role ID to list of title names they manage
            'reminders': { # Default reminders in hours before expiry
                24: "24 hours remaining on your title.",
                1: "1 hour remaining on your title."
            }
        },
        'schedules': {} # For !schedule command
    }

async def save_state():
    """Saves the bot's state to a JSON file."""
    async with state_lock:
        try:
            # Convert int keys back to string for JSON compatibility
            temp_state = state.copy()
            if 'config' in temp_state and 'reminders' in temp_state['config']:
                 temp_state['config']['reminders'] = {str(k): v for k, v in temp_state['config']['reminders'].items()}

            with open(STATE_FILE, 'w') as f:
                json.dump(temp_state, f, indent=4)
        except IOError as e:
            logger.error(f"Error saving state file: {e}")

def log_action(action, user_id, details):
    """Logs an action to the log file."""
    log_entry = {
        'timestamp': datetime.utcnow().isoformat(),
        'action': action,
        'user_id': user_id,
        'details': details
    }
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r+') as f:
                try:
                    logs = json.load(f)
                except json.JSONDecodeError:
                    logs = []
                logs.append(log_entry)
                f.seek(0)
                json.dump(logs, f, indent=4)
        else:
            with open(LOG_FILE, 'w') as f:
                json.dump([log_entry], f, indent=4)
    except IOError as e:
        logger.error(f"Error writing to log file: {e}")

def is_guardian_or_admin(ctx):
    """Check if the user is a Guardian or an Admin."""
    if ctx.author.guild_permissions.administrator:
        return True
    guardian_role_ids = state.get('config', {}).get('guardian_roles', [])
    user_role_ids = {role.id for role in ctx.author.roles}
    return any(role_id in user_role_ids for role_id in guardian_role_ids)

def is_title_guardian(user, title_name):
    """Check if a user is a designated guardian for a specific title."""
    if user.guild_permissions.administrator:
        return True
    guardian_titles = state.get('config', {}).get('guardian_titles', {})
    for role in user.roles:
        if str(role.id) in guardian_titles and title_name in guardian_titles[str(role.id)]:
            return True
    return False

# --- Main Bot Cog ---
class TitleCog(commands.Cog, name="TitleRequest"):
    def __init__(self, bot):
        self.bot = bot
        self.title_check_loop.start()

    def cog_unload(self):
        self.title_check_loop.cancel()

    @tasks.loop(minutes=1)
    async def title_check_loop(self):
        """Periodically checks for expired titles and sends reminders."""
        await self.bot.wait_until_ready()
        now = datetime.utcnow()
        
        # Check for expired titles
        titles_to_release = []
        for title_name, data in state.get('titles', {}).items():
            if data['holder'] and data.get('expiry_date'):
                expiry = datetime.fromisoformat(data['expiry_date'])
                if now >= expiry:
                    titles_to_release.append((title_name, data['holder']))
        
        for title_name, user_id in titles_to_release:
            guild = self.bot.guilds[0] # Assuming the bot is in one server
            member = guild.get_member(user_id)
            user_display = member.display_name if member else f"User ID {user_id}"
            logger.info(f"Title '{title_name}' expired for {user_display}. Forcing release.")
            await self.force_release_logic(title_name, self.bot.user.id, "Title expired.")

        # Check for reminders
        for title_name, data in state.get('titles', {}).items():
            if data['holder'] and data.get('expiry_date'):
                expiry = datetime.fromisoformat(data['expiry_date'])
                user_id = data['holder']
                reminders_sent = state['users'].setdefault(str(user_id), {}).setdefault('reminders_sent', {})
                
                for hours, message in state.get('config', {}).get('reminders', {}).items():
                    reminder_time = expiry - timedelta(hours=hours)
                    # Check if it's time to send and if this specific reminder for this title hasn't been sent
                    if now >= reminder_time and reminders_sent.get(title_name) != hours:
                        try:
                            user = await self.bot.fetch_user(user_id)
                            await user.send(f"**Title Reminder:** {message} (Title: {title_name})")
                            reminders_sent[title_name] = hours
                            logger.info(f"Sent {hours}-hour reminder for '{title_name}' to User ID {user_id}.")
                        except discord.Forbidden:
                            logger.warning(f"Cannot send reminder DM to User ID {user_id}.")
                        except discord.NotFound:
                            logger.warning(f"User ID {user_id} not found for reminder.")

        await save_state()

    # --- Member Commands ---

    @commands.command(help="Claim a title or see its queue. Usage: !claim <Title Name>")
    async def claim(self, ctx, *, title_name: str):
        title_name = title_name.strip()
        user_id = str(ctx.author.id)

        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return

        # Check if user already holds a title
        if any(t['holder'] == ctx.author.id for t in state['titles'].values()):
            await ctx.send("You already hold a title. Please release it before claiming another.")
            return

        title = state['titles'][title_name]
        
        if not title['holder']:
            # Title is free, but requires guardian approval
            title['pending_claimant'] = ctx.author.id
            await save_state()
            log_action('claim_request', ctx.author.id, {'title': title_name})
            
            # Notify guardians
            guardian_message = (f"👑 **Title Request:** {ctx.author.mention} has requested to claim the title **'{title_name}'**. "
                                f"A designated guardian must approve this by using `!assign {title_name} @{ctx.author.display_name}`.")
            
            await self.notify_guardians(ctx.guild, title_name, guardian_message)
            await ctx.send(f"Your request for '{title_name}' has been submitted. A guardian must approve it.")

        else:
            # Title is held, join the queue
            if ctx.author.id in title['queue']:
                await ctx.send(f"You are already in the queue for '{title_name}'.")
            else:
                title['queue'].append(ctx.author.id)
                await save_state()
                log_action('queue_join', ctx.author.id, {'title': title_name})
                await ctx.send(f"You have been added to the queue for '{title_name}'. Position: {len(title['queue'])}")

    @commands.command(help="Release a title you currently hold. Usage: !release <Title Name>")
    async def release(self, ctx, *, title_name: str):
        title_name = title_name.strip()
        user_id = ctx.author.id

        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return

        title = state['titles'][title_name]

        if title['holder'] != user_id:
            await ctx.send("You do not hold this title.")
            return

        await self.release_logic(ctx, title_name, user_id, "User released.")
        await ctx.send(f"You have released the title '{title_name}'.")

    @commands.command(help="Join the queue for a held title. Usage: !queue <Title Name>")
    async def queue(self, ctx, *, title_name: str):
        """Alias for !claim when title is held."""
        await self.claim(ctx, title_name=title_name)

    @commands.command(help="List all available titles and their status.")
    async def titles(self, ctx):
        if not state['titles']:
            await ctx.send("No titles have been configured.")
            return

        embed = discord.Embed(title="📜 Title Status", color=discord.Color.blue())
        
        sorted_titles = sorted(state['titles'].items())

        for title_name, data in sorted_titles:
            status = ""
            if data['holder']:
                holder = ctx.guild.get_member(data['holder'])
                holder_name = holder.display_name if holder else f"Unknown User (ID: {data['holder']})"
                expiry = datetime.fromisoformat(data['expiry_date'])
                remaining = expiry - datetime.utcnow()
                status = f"**Held by:** {holder_name}\n*Expires in: {str(timedelta(seconds=int(remaining.total_seconds())))}*"
                if data['queue']:
                    status += f"\n*Queue: {len(data['queue'])}*"
            elif data.get('pending_claimant'):
                claimant = ctx.guild.get_member(data['pending_claimant'])
                claimant_name = claimant.display_name if claimant else "Unknown User"
                status = f"**Pending Approval for:** {claimant_name}"
            else:
                status = "**Status:** Available"
            
            details = []
            if data.get('icon'): details.append(f"Icon: {data['icon']}")
            if data.get('buffs'): details.append(f"Buffs: {data['buffs']}")
            if details:
                status += "\n" + " | ".join(details)

            embed.add_field(name=f"👑 {title_name}", value=status, inline=False)

        await ctx.send(embed=embed)

    @commands.command(help="Show the title you currently hold.")
    async def mytitle(self, ctx):
        user_id = ctx.author.id
        held_title = None
        for title_name, data in state['titles'].items():
            if data['holder'] == user_id:
                held_title = (title_name, data)
                break
        
        if not held_title:
            await ctx.send("You do not currently hold any titles.")
            return

        title_name, data = held_title
        expiry = datetime.fromisoformat(data['expiry_date'])
        remaining = expiry - datetime.utcnow()
        
        embed = discord.Embed(title=f"Your Title: {title_name}", color=discord.Color.green())
        embed.add_field(name="Expires In", value=str(timedelta(seconds=int(remaining.total_seconds()))))
        
        if data.get('icon'):
            embed.set_thumbnail(url=data['icon'])
        if data.get('buffs'):
            embed.add_field(name="Buffs", value=data['buffs'], inline=False)
            
        await ctx.send(embed=embed)

    # --- Helper methods for logic ---

    async def release_logic(self, ctx, title_name, user_id, reason):
        """Core logic for releasing a title and processing the queue."""
        log_action('release', user_id, {'title': title_name, 'reason': reason})
        
        state['titles'][title_name]['holder'] = None
        state['titles'][title_name]['claim_date'] = None
        state['titles'][title_name]['expiry_date'] = None
        
        # Clear any reminders sent for this title for the user
        if 'reminders_sent' in state['users'].get(str(user_id), {}):
            state['users'][str(user_id)]['reminders_sent'].pop(title_name, None)

        await self.process_queue(ctx, title_name)
        await save_state()

    async def process_queue(self, ctx, title_name):
        """Processes the queue for a title that has just become free."""
        queue = state['titles'][title_name]['queue']
        if not queue:
            # Announce title is free if no one is in queue
            await self.announce(f"👑 The title **'{title_name}'** is now available!")
            return

        next_in_line_id = None
        original_queue = list(queue) # Copy for iteration
        
        for user_id in original_queue:
            # Check if the user already holds another title
            if any(t['holder'] == user_id for t in state['titles'].values()):
                try:
                    user = await self.bot.fetch_user(user_id)
                    await user.send(f"It's your turn for the title '{title_name}', but you already hold another title. Your turn has been skipped.")
                    logger.info(f"Skipped {user.display_name} in queue for '{title_name}' as they hold another title.")
                except discord.Forbidden:
                    logger.warning(f"Could not DM user {user_id} about being skipped in queue.")
                queue.pop(0) # Remove from the front
            else:
                next_in_line_id = queue.pop(0)
                break # Found a valid user

        if next_in_line_id:
            state['titles'][title_name]['pending_claimant'] = next_in_line_id
            user = ctx.guild.get_member(next_in_line_id)
            user_mention = user.mention if user else f"<@{next_in_line_id}>"
            
            guardian_message = (f"👑 **Next in Queue:** {user_mention}, it's your turn for the title **'{title_name}'**! "
                                f"A designated guardian must use `!assign {title_name} {user_mention}` to grant it to you.")
            
            await self.notify_guardians(ctx.guild, title_name, guardian_message)
            
            try: # Also DM the user
                if user:
                    await user.send(f"It's your turn for the title '{title_name}'! A guardian has been notified to assign it to you.")
            except discord.Forbidden:
                pass
        else:
            # Everyone in the queue already had a title
            await self.announce(f"👑 The title **'{title_name}'** is now available!")

    async def force_release_logic(self, title_name, actor_id, reason):
        """Logic for forcing a release, callable by code (e.g., expiry)."""
        if title_name not in state['titles'] or not state['titles'][title_name]['holder']:
            return

        user_id = state['titles'][title_name]['holder']
        log_action('force_release', actor_id, {'title': title_name, 'released_user': user_id, 'reason': reason})

        state['titles'][title_name]['holder'] = None
        state['titles'][title_name]['claim_date'] = None
        state['titles'][title_name]['expiry_date'] = None
        
        if 'reminders_sent' in state['users'].get(str(user_id), {}):
            state['users'][str(user_id)]['reminders_sent'].pop(title_name, None)

        # Need a context-like object to process queue. We can get the guild.
        guild = self.bot.guilds[0]
        # We can't send a message to a channel without a context, but we can announce.
        # The process_queue function needs a ctx, let's adapt it or fake it.
        class FakeContext:
            def __init__(self, guild):
                self.guild = guild
        
        await self.process_queue(FakeContext(guild), title_name)
        await save_state()
        await self.announce(f"👑 The title **'{title_name}'** held by <@{user_id}> has been automatically released. Reason: {reason}")
    
    async def announce(self, message):
        """Sends a message to the configured announcement channel."""
        channel_id = state.get('config', {}).get('announcement_channel')
        if channel_id:
            try:
                channel = await self.bot.fetch_channel(channel_id)
                await channel.send(message)
            except (discord.NotFound, discord.Forbidden) as e:
                logger.error(f"Could not send to announcement channel {channel_id}: {e}")

    async def notify_guardians(self, guild, title_name, message):
        """Notifies designated guardians for a title, or all guardians if none are designated."""
        notified_roles = set()
        guardian_titles = state.get('config', {}).get('guardian_titles', {})
        
        # Find roles specifically for this title
        for role_id_str, titles in guardian_titles.items():
            if title_name in titles:
                notified_roles.add(int(role_id_str))

        # If no specific guardians, notify all guardian roles
        if not notified_roles:
            notified_roles.update(state.get('config', {}).get('guardian_roles', []))

        if not notified_roles:
            await self.announce(f"⚠️ **Guardian Alert:** No guardian roles configured to handle the request for **'{title_name}'**. An admin should set this up.")
            return

        full_message = ""
        for role_id in notified_roles:
            role = guild.get_role(role_id)
            if role:
                full_message += f"{role.mention} "
        
        full_message += f"\n{message}"
        await self.announce(full_message)
# main.py - Part 2/3

    # --- Guardian and Admin Commands ---

    @commands.command(help="Assign a title to a user. Usage: !assign <Title Name> <@User>")
    @commands.check(is_guardian_or_admin)
    async def assign(self, ctx, title_name: str, member: discord.Member):
        title_name = title_name.strip()
        
        if not is_title_guardian(ctx.author, title_name):
            await ctx.send(f"You are not a designated guardian for the title '{title_name}'.")
            return

        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return

        title = state['titles'][title_name]
        pending_claimant = title.get('pending_claimant')

        if pending_claimant != member.id:
            await ctx.send(f"{member.display_name} is not the pending claimant for this title. "
                           f"The current pending claimant is <@{pending_claimant}>.")
            return
            
        # Check if user already holds a title
        if any(t['holder'] == member.id for t in state['titles'].values()):
            await ctx.send(f"{member.display_name} already holds a title. They must release it first.")
            return

        min_hold_hours = state['config']['min_hold_duration_hours']
        now = datetime.utcnow()
        expiry_date = now + timedelta(hours=min_hold_hours)

        title['holder'] = member.id
        title['claim_date'] = now.isoformat()
        title['expiry_date'] = expiry_date.isoformat()
        title['pending_claimant'] = None

        log_action('assign', ctx.author.id, {'title': title_name, 'user': member.id})
        await save_state()

        await self.announce(f"🎉 Congratulations {member.mention}! You have been granted the title **'{title_name}'**.")
        await ctx.send(f"Successfully assigned '{title_name}' to {member.display_name}.")

    @commands.command(help="Extend a user's hold on a title. Usage: !snooze <Title Name> <hours>")
    @commands.check(is_guardian_or_admin)
    async def snooze(self, ctx, title_name: str, hours: int):
        title_name = title_name.strip()
        
        if not is_title_guardian(ctx.author, title_name):
            await ctx.send(f"You are not a designated guardian for the title '{title_name}'.")
            return

        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return
            
        title = state['titles'][title_name]
        if not title['holder']:
            await ctx.send(f"The title '{title_name}' is not currently held.")
            return

        expiry = datetime.fromisoformat(title['expiry_date'])
        new_expiry = expiry + timedelta(hours=hours)
        title['expiry_date'] = new_expiry.isoformat()

        log_action('snooze', ctx.author.id, {'title': title_name, 'user': title['holder'], 'hours': hours})
        await save_state()
        
        holder = ctx.guild.get_member(title['holder'])
        await ctx.send(f"Extended hold for {holder.display_name} on '{title_name}' by {hours} hours.")
        await self.announce(f"⏰ The hold on **'{title_name}'** for {holder.mention} has been extended by {hours} hours.")

    @commands.command(help="Forcibly remove a title from a user. Usage: !force_release <Title Name>")
    @commands.check(is_guardian_or_admin)
    async def force_release(self, ctx, *, title_name: str):
        title_name = title_name.strip()

        if not is_title_guardian(ctx.author, title_name):
            await ctx.send(f"You are not a designated guardian for the title '{title_name}'.")
            return

        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return

        title = state['titles'][title_name]
        if not title['holder']:
            await ctx.send(f"The title '{title_name}' is not currently held.")
            return
            
        released_user_id = title['holder']
        reason = f"Forced by {ctx.author.display_name}"
        await self.release_logic(ctx, title_name, released_user_id, reason)
        
        await ctx.send(f"Forcibly released '{title_name}' from <@{released_user_id}>.")
        await self.announce(f"️️️️⚠️ The title **'{title_name}'** has been forcibly released by an admin/guardian.")

    # --- Admin-Only Commands ---

    @commands.command(help="Import titles from a comma-separated list. Usage: !import_titles <Title1, Title2, ...>")
    @commands.has_permissions(administrator=True)
    async def import_titles(self, ctx, *, titles_csv: str):
        titles = [t.strip() for t in titles_csv.split(',')]
        added_count = 0
        for title in titles:
            if title and title not in state['titles']:
                state['titles'][title] = {
                    'holder': None,
                    'claim_date': None,
                    'expiry_date': None,
                    'queue': [],
                    'icon': None,
                    'buffs': None,
                    'pending_claimant': None
                }
                added_count += 1
        
        if added_count > 0:
            log_action('import_titles', ctx.author.id, {'count': added_count, 'titles': titles})
            await save_state()
            await ctx.send(f"Successfully added {added_count} new titles.")
        else:
            await ctx.send("No new titles were added. They may already exist.")

    @commands.command(help="Delete a title permanently. Usage: !delete_title <Title Name>")
    @commands.has_permissions(administrator=True)
    async def delete_title(self, ctx, *, title_name: str):
        title_name = title_name.strip()
        if title_name in state['titles']:
            del state['titles'][title_name]
            # Also remove from guardian assignments
            for role_id in state['config']['guardian_titles']:
                if title_name in state['config']['guardian_titles'][role_id]:
                    state['config']['guardian_titles'][role_id].remove(title_name)
            
            log_action('delete_title', ctx.author.id, {'title': title_name})
            await save_state()
            await ctx.send(f"Title '{title_name}' has been permanently deleted.")
        else:
            await ctx.send("Title not found.")

    @commands.command(help="Set the minimum hold duration for titles. Usage: !set_min_hold <hours>")
    @commands.has_permissions(administrator=True)
    async def set_min_hold(self, ctx, hours: int):
        state['config']['min_hold_duration_hours'] = hours
        log_action('set_config', ctx.author.id, {'key': 'min_hold_duration_hours', 'value': hours})
        await save_state()
        await ctx.send(f"Minimum title hold duration set to {hours} hours.")

    @commands.command(help="Set the channel for bot announcements. Usage: !set_announce <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        state['config']['announcement_channel'] = channel.id
        log_action('set_config', ctx.author.id, {'key': 'announcement_channel', 'value': channel.id})
        await save_state()
        await ctx.send(f"Announcement channel set to {channel.mention}.")

    @commands.command(name="set_guardians", help="Set roles that can manage titles. Usage: !set_guardians <@Role1> <@Role2> ...")
    @commands.has_permissions(administrator=True)
    async def set_guardians(self, ctx, roles: commands.Greedy[discord.Role]):
        if not roles:
            state['config']['guardian_roles'] = []
            await ctx.send("Guardian roles cleared.")
        else:
            role_ids = [role.id for role in roles]
            state['config']['guardian_roles'] = role_ids
            log_action('set_config', ctx.author.id, {'key': 'guardian_roles', 'value': role_ids})
            await ctx.send(f"Guardian roles set to: {', '.join(r.mention for r in roles)}")
        await save_state()

    @commands.command(name="set_guardian_titles", help="Assign specific titles to a guardian role. Usage: !set_guardian_titles <@Role> <Title1, Title2, ...>")
    @commands.has_permissions(administrator=True)
    async def set_guardian_titles(self, ctx, role: discord.Role, *, titles_csv: str):
        titles = [t.strip() for t in titles_csv.split(',')]
        valid_titles = []
        invalid_titles = []
        for title in titles:
            if title in state['titles']:
                valid_titles.append(title)
            else:
                invalid_titles.append(title)

        state['config']['guardian_titles'][str(role.id)] = valid_titles
        log_action('set_config', ctx.author.id, {'key': 'guardian_titles', 'role': role.id, 'titles': valid_titles})
        await save_state()
        
        response = f"Role {role.mention} is now a guardian for: {', '.join(valid_titles)}."
        if invalid_titles:
            response += f"\nCould not find the following titles: {', '.join(invalid_titles)}."
        await ctx.send(response)

    @commands.command(help="Set details for a title. Usage: !set_title_details <Title Name> <icon|buffs> <value>")
    @commands.has_permissions(administrator=True)
    async def set_title_details(self, ctx, title_name: str, detail_type: str, *, value: str):
        title_name = title_name.strip()
        detail_type = detail_type.lower()
        if title_name not in state['titles']:
            await ctx.send("Title not found.")
            return
        if detail_type not in ['icon', 'buffs']:
            await ctx.send("Invalid detail type. Use 'icon' or 'buffs'.")
            return
            
        state['titles'][title_name][detail_type] = value
        log_action('set_title_details', ctx.author.id, {'title': title_name, 'detail': detail_type, 'value': value})
        await save_state()
        await ctx.send(f"Set {detail_type} for '{title_name}'.")

    @commands.command(help="Add/update a reminder. Usage: !set_reminders <hours> <message>")
    @commands.has_permissions(administrator=True)
    async def set_reminders(self, ctx, hours: int, *, message: str):
        if hours <= 0:
            await ctx.send("Hours must be a positive number.")
            return
        state['config']['reminders'][hours] = message
        log_action('set_config', ctx.author.id, {'key': 'reminders', 'hours': hours, 'message': message})
        await save_state()
        await ctx.send(f"Set reminder for {hours} hours before expiry.")

    @commands.command(help="Show current bot configuration.")
    @commands.has_permissions(administrator=True)
    async def config(self, ctx):
        conf = state['config']
        min_hold = conf['min_hold_duration_hours']
        
        channel_id = conf['announcement_channel']
        channel = f"<#{channel_id}>" if channel_id else "Not set"
        
        guardian_roles = [f"<@&{rid}>" for rid in conf['guardian_roles']] or ["Not set"]
        
        embed = discord.Embed(title="Bot Configuration", color=discord.Color.orange())
        embed.add_field(name="Min Hold Duration", value=f"{min_hold} hours", inline=False)
        embed.add_field(name="Announcement Channel", value=channel, inline=False)
        embed.add_field(name="Guardian Roles", value=', '.join(guardian_roles), inline=False)
        
        reminders = "\n".join([f"{h}h: {msg}" for h, msg in conf['reminders'].items()]) or "None"
        embed.add_field(name="Reminders", value=reminders, inline=False)
        
        guardian_titles = []
        for role_id, titles in conf['guardian_titles'].items():
            guardian_titles.append(f"<@&{role_id}>: {', '.join(titles)}")
        embed.add_field(name="Title-Specific Guardians", value='\n'.join(guardian_titles) or "None", inline=False)
        
        await ctx.send(embed=embed)

    # --- History Commands ---

    @commands.command(help="Show the last 10 log entries for a user or title. Usage: !history <@User or Title Name>")
    async def history(self, ctx, *, query: str):
        try:
            member = await commands.MemberConverter().convert(ctx, query)
            is_user_query = True
        except commands.MemberNotFound:
            member = None
            is_user_query = False

        if not os.path.exists(LOG_FILE):
            await ctx.send("Log file not found.")
            return
        
        with open(LOG_FILE, 'r') as f:
            try:
                logs = json.load(f)
            except json.JSONDecodeError:
                logs = []

        filtered_logs = []
        if is_user_query:
            for log in logs:
                if log.get('user_id') == member.id or log.get('details', {}).get('user') == member.id or log.get('details', {}).get('released_user') == member.id:
                    filtered_logs.append(log)
            title_text = f"History for {member.display_name}"
        else: # Title query
            for log in logs:
                if log.get('details', {}).get('title') == query:
                    filtered_logs.append(log)
            title_text = f"History for Title '{query}'"

        if not filtered_logs:
            await ctx.send("No history found for that query.")
            return

        embed = discord.Embed(title=title_text, color=discord.Color.purple())
        for log in reversed(filtered_logs[-10:]):
            ts = datetime.fromisoformat(log['timestamp']).strftime('%Y-%m-%d %H:%M')
            user = f"<@{log['user_id']}>"
            action = log['action'].replace('_', ' ').title()
            details = ', '.join([f"{k}: {v}" for k, v in log['details'].items()])
            embed.add_field(name=f"[{ts}] {action} by {user}", value=f"```{details}```", inline=False)
        
        await ctx.send(embed=embed)

    @commands.command(help="Get the full history file.")
    @commands.has_permissions(administrator=True)
    async def fullhistory(self, ctx):
        if os.path.exists(LOG_FILE):
            await ctx.send(file=discord.File(LOG_FILE))
        else:
            await ctx.send("Log file does not exist.")

    @commands.command(help="Book a 1-hour time slot for a title. Usage: !schedule <Title Name> <YYYY-MM-DD> <HH:00>")
    async def schedule(self, ctx, title_name: str, date_str: str, time_str: str):
        title_name = title_name.strip()
        if title_name not in state['titles']:
            await ctx.send("Title not found.")
            return

        try:
            schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            if schedule_time.minute != 0:
                raise ValueError
        except ValueError:
            await ctx.send("Invalid date/time format. Use YYYY-MM-DD and HH:00 (24-hour format).")
            return

        if schedule_time < datetime.now():
            await ctx.send("Cannot schedule a time in the past.")
            return

        title_schedules = state['schedules'].setdefault(title_name, {})
        schedule_key = schedule_time.isoformat()

        if schedule_key in title_schedules:
            await ctx.send(f"This time slot is already booked by <@{title_schedules[schedule_key]}>.")
            return

        # Check for user conflicts
        for schedules in state['schedules'].values():
            if schedule_key in schedules and schedules[schedule_key] == ctx.author.id:
                await ctx.send("You have already booked another title for this exact time slot.")
                return

        title_schedules[schedule_key] = ctx.author.id
        log_action('schedule_book', ctx.author.id, {'title': title_name, 'time': schedule_key})
        await save_state()
        await ctx.send(f"Successfully booked '{title_name}' for {date_str} at {time_str} UTC.")
# main.py - Part 3/3

from waitress import serve

# --- Bot Startup and Web Server ---

@bot.event
async def on_ready():
    """Event that runs when the bot is ready."""
    await load_state()
    await bot.add_cog(TitleCog(bot))
    logger.info(f'{bot.user.name} has connected to Discord!')
    logger.info(f'State loaded. Found {len(state.get("titles", {}))} titles.')
    # Start the Flask server in a separate thread using a production-grade server
    flask_thread = Thread(target=run_flask_app, daemon=True)
    flask_thread.start()
    logger.info("Flask web server started with waitress.")

# --- Flask Web Server ---
app = Flask(__name__)

def get_bot_state():
    """Safely gets a copy of the bot's state for Flask."""
    # This is a simplified approach. For high-concurrency, a more robust
    # IPC mechanism like Redis or a dedicated API might be better.
    # For this project, we assume the global state is sufficient.
    return state

def get_guild_and_members():
    """Gets the guild and member list from the running bot."""
    if not bot.is_ready() or not bot.guilds:
        return None, {}
    guild = bot.guilds[0]
    members = {str(m.id): m.display_name for m in guild.members}
    return guild, members

@app.route("/")
def dashboard():
    """Renders the main dashboard."""
    bot_state = get_bot_state()
    guild, members = get_guild_and_members()
    
    titles_data = []
    for name, data in sorted(bot_state.get('titles', {}).items()):
        holder_name = members.get(str(data['holder'])) if data['holder'] else "None"
        pending_name = members.get(str(data['pending_claimant'])) if data.get('pending_claimant') else "None"
        
        remaining = "N/A"
        if data.get('expiry_date'):
            expiry = datetime.fromisoformat(data['expiry_date'])
            delta = expiry - datetime.utcnow()
            if delta.total_seconds() > 0:
                remaining = str(timedelta(seconds=int(delta.total_seconds())))
            else:
                remaining = "Expired"

        queue_names = [members.get(str(uid), f"ID: {uid}") for uid in data.get('queue', [])]
        
        titles_data.append({
            'name': name,
            'holder': holder_name,
            'pending': pending_name,
            'expires_in': remaining,
            'queue': queue_names,
            'icon': data.get('icon'),
            'buffs': data.get('buffs')
        })
        
    return render_template('dashboard.html', titles=titles_data)

@app.route("/scheduler")
def scheduler():
    """Renders the interactive scheduler page."""
    bot_state = get_bot_state()
    guild, members = get_guild_and_members()
    
    # Generate calendar data for the next 7 days
    today = datetime.utcnow().date()
    days = [(today + timedelta(days=i)) for i in range(7)]
    hours = [f"{h:02d}:00" for h in range(24)]
    
    # Get all title names
    title_names = sorted(bot_state.get('titles', {}).keys())

    # Get existing schedules
    schedules = bot_state.get('schedules', {})
    
    return render_template('scheduler.html', days=days, hours=hours, titles=title_names, schedules=schedules, members=members)

@app.route("/request-title", methods=['POST'])
def request_title():
    """Handles the web form for requesting a title."""
    title_name = request.form.get('title_name')
    user_id_str = request.form.get('user_id')
    
    if not title_name or not user_id_str:
        return "Missing form data.", 400

    try:
        user_id = int(user_id_str)
    except ValueError:
        return "Invalid User ID.", 400

    # This is a simplified simulation. A real implementation would need
    # to find the user in the guild and trigger the !claim command logic.
    # For now, we'll just log it.
    logger.info(f"Web request received for title '{title_name}' by user ID '{user_id}'.")
    log_action('web_claim_request', user_id, {'title': title_name, 'source': 'web_form'})
    
    # A more advanced version would use bot.loop.call_soon_threadsafe
    # to interact with the bot's async logic.
    
    return redirect(url_for('dashboard'))

@app.route("/book-slot", methods=['POST'])
def book_slot():
    """Handles booking a time slot from the web scheduler."""
    title_name = request.form.get('title')
    date_str = request.form.get('date')
    time_str = request.form.get('time')
    user_id_str = request.form.get('user_id') # This needs to be securely obtained in a real app

    if not all([title_name, date_str, time_str, user_id_str]):
        return "Missing data for booking.", 400
        
    # This is a simplified example. A real app needs authentication to get the user ID.
    # We'll assume it's passed for this demonstration.
    try:
        user_id = int(user_id_str)
        schedule_time = datetime.fromisoformat(f"{date_str}T{time_str}")
    except (ValueError, TypeError):
        return "Invalid data format.", 400

    # This function would need to be thread-safe to modify the bot's state.
    # We use call_soon_threadsafe to schedule the async function from this thread.
    async def do_booking():
        async with state_lock:
            title_schedules = state['schedules'].setdefault(title_name, {})
            schedule_key = schedule_time.isoformat()
            if schedule_key not in title_schedules:
                title_schedules[schedule_key] = user_id
                log_action('schedule_book_web', user_id, {'title': title_name, 'time': schedule_key})
                await save_state() # save_state is async
    
    bot.loop.call_soon_threadsafe(asyncio.create_task, do_booking())
    
    return redirect(url_for('scheduler'))

def run_flask_app():
    """Runs the Flask app using a production-grade WSGI server."""
    serve(app, host='0.0.0.0', port=8080)

# --- Create Flask Templates (in-memory) ---
# In a real project, these would be in a 'templates' folder.
if not os.path.exists('templates'):
    os.makedirs('templates')

dashboard_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TitleRequest Dashboard</title>
    <style>
        body { font-family: sans-serif; background-color: #36393f; color: #dcddde; margin: 2em; }
        h1, h2 { color: #ffffff; }
        .container { max-width: 1200px; margin: auto; }
        .title-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }
        .title-card { background-color: #2f3136; border-radius: 8px; padding: 20px; border-left: 5px solid #7289da; }
        .title-card h3 { margin-top: 0; display: flex; align-items: center; }
        .title-card img { width: 24px; height: 24px; margin-right: 10px; border-radius: 50%; }
        .title-card p { margin: 5px 0; }
        .title-card strong { color: #ffffff; }
        .queue { list-style: none; padding-left: 0; }
        .queue li { background-color: #40444b; padding: 5px 10px; border-radius: 4px; margin-top: 5px; }
        .form-card { background-color: #2f3136; padding: 20px; border-radius: 8px; margin-top: 2em; }
        input, select, button { width: 100%; padding: 10px; margin-top: 10px; border-radius: 4px; border: 1px solid #202225; background-color: #40444b; color: #dcddde; }
        button { background-color: #7289da; cursor: pointer; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>👑 TitleRequest Dashboard</h1>
        <p>Live status of all server titles. <a href="/scheduler">View Scheduler</a></p>
        <div class="title-grid">
            {% for title in titles %}
            <div class="title-card">
                <h3>
                    {% if title.icon %}<img src="{{ title.icon }}" alt="icon">{% endif %}
                    {{ title.name }}
                </h3>
                <p><strong>Status:</strong> 
                    {% if title.holder != 'None' %} Held
                    {% elif title.pending != 'None' %} Pending Approval
                    {% else %} Available
                    {% endif %}
                </p>
                <p><strong>Holder:</strong> {{ title.holder }}</p>
                <p><strong>Expires In:</strong> {{ title.expires_in }}</p>
                {% if title.buffs %}<p><strong>Buffs:</strong> {{ title.buffs }}</p>{% endif %}
                {% if title.queue %}
                <h4>Queue:</h4>
                <ul class="queue">
                    {% for user in title.queue %}
                    <li>{{ user }}</li>
                    {% endfor %}
                </ul>
                {% endif %}
            </div>
            {% endfor %}
        </div>

        <div class="form-card">
            <h2>Request a Title</h2>
            <form action="/request-title" method="POST">
                <p>Note: This is a simplified form for demonstration. In a real app, you would be authenticated via Discord OAuth2.</p>
                <label for="title_name">Title Name:</label>
                <select id="title_name" name="title_name" required>
                    {% for title in titles %}<option value="{{ title.name }}">{{ title.name }}</option>{% endfor %}
                </select>
                <label for="user_id">Your Discord User ID:</label>
                <input type="text" id="user_id" name="user_id" placeholder="Enter your Discord User ID" required>
                <button type="submit">Submit Request</button>
            </form>
        </div>
    </div>
</body>
</html>
"""
with open('templates/dashboard.html', 'w') as f:
    f.write(dashboard_template)

scheduler_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Title Scheduler</title>
    <style>
        body { font-family: sans-serif; background-color: #36393f; color: #dcddde; margin: 2em; }
        h1 { color: #ffffff; }
        .container { max-width: 1400px; margin: auto; }
        .calendar-view { overflow-x: auto; }
        table { border-collapse: collapse; width: 100%; margin-top: 1em; }
        th, td { border: 1px solid #40444b; padding: 8px; text-align: center; min-width: 120px; }
        th { background-color: #2f3136; }
        .time-header { min-width: 80px; }
        .booked { background-color: #f04747; color: white; font-size: 0.8em; }
        .available { background-color: #43b581; cursor: pointer; }
        .form-card { background-color: #2f3136; padding: 20px; border-radius: 8px; margin-top: 2em; }
        input, select, button { width: 100%; padding: 10px; margin-top: 10px; border-radius: 4px; border: 1px solid #202225; background-color: #40444b; color: #dcddde; }
        button { background-color: #7289da; cursor: pointer; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🗓️ Title Scheduler</h1>
        <p>Book a 1-hour time slot for a title. All times are in UTC. <a href="/">Back to Dashboard</a></p>
        
        <div class="form-card">
            <h2>Book a Slot</h2>
            <form action="/book-slot" method="POST">
                <p>Note: This is a simplified form. In a real app, you would be authenticated.</p>
                <label for="title">Title:</label>
                <select id="title" name="title" required>
                    {% for title in titles %}<option value="{{ title }}">{{ title }}</option>{% endfor %}
                </select>
                <label for="date">Date:</label>
                <input type="date" id="date" name="date" required>
                <label for="time">Time (UTC, hour):</label>
                <select id="time" name="time" required>
                    {% for hour in hours %}<option value="{{ hour }}">{{ hour }}</option>{% endfor %}
                </select>
                <label for="user_id">Your Discord User ID:</label>
                <input type="text" id="user_id" name="user_id" placeholder="Enter your Discord User ID" required>
                <button type="submit">Book Slot</button>
            </form>
        </div>

        <h2>Upcoming Week Schedule</h2>
        <div class="calendar-view">
            <table>
                <thead>
                    <tr>
                        <th class="time-header">Time (UTC)</th>
                        {% for day in days %}
                        <th>{{ day.strftime('%A') }}<br>{{ day.strftime('%Y-%m-%d') }}</th>
                        {% endfor %}
                    </tr>
                </thead>
                <tbody>
                    {% for hour in hours %}
                    <tr>
                        <td class="time-header">{{ hour }}</td>
                        {% for day in days %}
                        <td>
                            {% for title_name, schedule_data in schedules.items() %}
                                {% set slot_time = day.strftime('%Y-%m-%d') + 'T' + hour + ':00' %}
                                {% if schedule_data[slot_time] %}
                                    <div class="booked">
                                        <strong>{{ title_name }}</strong><br>
                                        {{ members.get(schedule_data[slot_time]|string, 'Unknown') }}
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </td>
                        {% endfor %}
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""
with open('templates/scheduler.html', 'w') as f:
    f.write(scheduler_template)

# --- Final Bot Execution ---
if __name__ == "__main__":
    # It's recommended to load the token from an environment variable for security
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token:
        print("Error: DISCORD_BOT_TOKEN environment variable not set.")
    else:
        bot.run(bot_token)

# --- Final Deliverables ---
#
# ## Pip Install Commands
#
# pip install discord.py flask waitress
#
# ## How to Run the Bot
#
# 1.  **Set Environment Variable:** Before running, you must set an environment
#     variable to hold your Discord bot token.
#
#     -   On Linux/macOS: `export DISCORD_BOT_TOKEN="YOUR_TOKEN_HERE"`
#     -   On Windows (Command Prompt): `set DISCORD_BOT_TOKEN="YOUR_TOKEN_HERE"`
#     -   On Windows (PowerShell): `$env:DISCORD_BOT_TOKEN="YOUR_TOKEN_HERE"`
#
# 2.  **Run the Python script:**
#
#     `python main.py`
#
# 3.  **Web Dashboard:** Once the bot is running, the web dashboard will be
#     accessible at `http://<your_server_ip>:8080`.
#
# ## State and Log Files
#
# The bot will automatically create two files in the same directory where it is run:
#
# -   `titles_state.json`: This file stores the current state of all titles,
#     queues, and configurations. Do not edit this file manually while the bot
#     is running.
# -   `log.json`: This file contains a persistent history of all major actions
#     performed by the bot and its users, such as claiming, releasing, and
#     assigning titles.

# main.py - Part 3/3
