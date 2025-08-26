# TitleRequest Discord Bot

TitleRequest is a specialized Discord bot designed to manage in-game titles for a server community. It implements a fair, first-come-first-serve queue system with a mandatory guardian approval workflow to ensure title changes are correctly performed in-game before being updated on Discord.

The bot is persistent, meaning its state (queues, holders, configuration) survives restarts. It's fully configurable and provides a complete history log of all title-related events.

---

## ✨ Features

* **Fair Queuing**: A first-come-first-serve system ensures everyone gets a turn.
* **Minimum Hold Time**: Enforces a configurable holding period (`60 minutes` by default) before a title can be passed to the next person in the queue.
* **Guardian Workflow**: Title handoffs are not automatic. They become "due," and a designated guardian must confirm the change in-game with `!ack` before the bot updates the state.
* **Automated Notifications**: The bot automatically notifies guardians in a dedicated channel when a title change is due and sends periodic reminders if a change is pending.
* **Persistent State**: All title holders, queues, and settings are saved in a `titles_state.json` file, so the bot never loses track.
* **Historical Logging**: Every action is recorded in `log.json`, providing a complete, auditable history of title changes.
* **Discord Role Mirroring**: Can automatically assign and remove Discord roles corresponding to the titles, keeping them in sync with the bot's official state.
* **Fully Configurable**: Admins can set the titles, minimum hold time, guardians, notification channel, and reminder frequency.

---

## 🚀 Setup and Installation

Follow these steps to get the TitleRequest bot running on your server.

### Prerequisites

* Python 3.8 or newer.
* A Discord Bot account with a token. You can create one on the [Discord Developer Portal](https://discord.com/developers/applications).

### Installation Steps

1. **Download the Bot**
    Save the `main.py` script to a directory on your computer or server.

2. **Install Dependencies**
    The bot requires the `discord.py` library. Install it using pip:

    ```bash
    pip install discord.py
    ```

3. **Invite the Bot to Your Server**
    When inviting your bot, you must grant it the following permissions for it to function correctly:
    * Manage Roles (Crucial for role mirroring)
    * Send Messages
    * Embed Links
    * Read Message History
    * Mention @everyone, @here, and All Roles (For notifications)

4. **Set the Bot Token**
    The bot's token is loaded from an environment variable for security. **Never hardcode your token in the script.**

    * **On Linux/macOS:**

        ```bash
        export DISCORD_TOKEN="YOUR_BOT_TOKEN_HERE"
        ```

    * **On Windows (Command Prompt):**

        ```bash
        set DISCORD_TOKEN="YOUR_BOT_TOKEN_HERE"
        ```

5. **Run the Bot**
    Execute the script from your terminal in the same session where you set the token:

    ```bash
    python main.py
    ```

    The bot will start, and two files, `titles_state.json` and `log.json`, will be created in the same directory.

---

## ⚙️ Initial Configuration

Once the bot is in your server and running, an admin (with "Manage Roles" permission) must perform these initial setup commands.

1. **Import Titles**
    Define the titles you want the bot to manage. The bot will also attempt to create corresponding Discord roles if they don't exist.
    > `!import_titles Governor, Architect, Prefect, General`

2. **Set the Announcement Channel**
    This is the channel where all guardian notifications and confirmations will be posted. The bot's core workflow depends on this.
    > `!set_announce #title-updates`

3. **Set the Guardians**
    Specify who can acknowledge title changes. You can mention one or more users and/or roles.
    > `!set_guardians @GuardianRole @GameAdmin1`

After these steps, the bot is fully operational!

---

## 📋 Commands

### Member Commands (For Everyone)

| Command                  | Description                                            |
| ------------------------ | ------------------------------------------------------ |
| `!claim <title_name>`    | Claim an available title or join its queue.            |
| `!release`               | Release the title you currently hold.                  |
| `!queue <title_name>`    | Show the current holder and queue for a specific title.|
| `!titles`                | Display a summary of all titles and their status.      |
| `!mytitle`               | Show which title you hold and for how long.            |

### Guardian & Admin Commands

| Command                           | Description                                            |
| --------------------------------- | ------------------------------------------------------ |
| `!ack <title_name>`               | Acknowledge and confirm a title handoff (Guardians only).|
| `!snooze <title_name> <minutes>`  | Delay notifications for a pending change (Guardians only).|
| `!force_release <title_name>`     | Forcibly remove a holder from a title (Admins only).  |

### Admin-Only Commands (Requires "Manage Roles")

| Command                                          | Description                                                        |
| ------------------------------------------------ | ------------------------------------------------------------------ |
| `!import_titles <list>`                          | Seed or update the list of managed titles (e.g., `Title A, Title B`).|
| `!set_min_hold <minutes>`                        | Set the minimum time a user must hold a title.                     |
| `!set_announce <#channel>`                       | Set the channel for guardian notifications.                        |
| `!set_guardians <@user/@role ...>`               | Set the users or roles who can use guardian commands.              |
| `!set_reminders <interval_minutes> <max_count>`  | Configure reminder frequency and count for pending changes.        |
| `!config`                                        | Display the current bot configuration.                             |

### History Commands

| Command                         | Description                                            |
| ------------------------------- | ------------------------------------------------------ |
| `!history <title_name> [count]` | Show the last N events for a single title (default 10).|
| `!fullhistory [count]`          | Show the last N events across all titles (default 10). |

---

## 🔄 Workflow Example

1. **`PlayerA` claims an open title:** `!claim Governor`
    > *Bot gives PlayerA the title and starts a 60-minute timer.*
2. **`PlayerB` wants the same title:** `!claim Governor`
    > *Bot adds PlayerB to the queue for Governor at position #1.*
3. **60 minutes pass.**
    > *The bot's automatic check runs and sees the hold time is met and the queue is not empty.*
    > **Bot posts in `#title-updates`**: "Title Change Due: Governor. Current: @PlayerA → Next: @PlayerB. Guardians please confirm with `!ack Governor`."
4. **A `Guardian` sees the notice.**
    > *The guardian goes into the game and transfers the "Governor" title from PlayerA to PlayerB.*
5. **The `Guardian` confirms the action on Discord:** `!ack Governor`
    > *Bot updates its state: PlayerB is now the holder, the timer is reset, and PlayerB is removed from the queue.*
    > **Bot posts a confirmation**: "✅ Confirmed: The **Governor** title has been passed to @PlayerB!"

---

## 💾 Data Persistence and Backups

The bot uses two files to store its data:

* `titles_state.json`: **This is the master state file.** It contains all current title holders, queues, timestamps, and configuration.
* `log.json`: A historical record of every action the bot has processed.

To **back up** your bot, simply make a copy of `titles_state.json`. To **restore** from a backup, stop the bot, replace the existing `titles_state.json` with your backup file, and restart the bot.

---

## ⚠️ Permissions and Role Hierarchy

For the bot to function correctly, especially for the role mirroring feature, please ensure two things:

1. **Invite Permissions**: The bot must be invited with the `Manage Roles` permission.
2. **Role Hierarchy**: The bot's own role (e.g., "TitleRequest") **must be positioned higher** in your server's role list than the title roles it needs to manage (e.g., "Governor", "Architect"). If the bot's role is lower, it will not have permission to assign or remove the title roles from users.
